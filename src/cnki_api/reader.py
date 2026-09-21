"""知网阅读/取文接口（Phase A — A4 产出）。

下游 C2（HTML 阅读窗口）/C3（原版图像流）/C4（缓存层）依赖本模块契约，签名不得改。

## 实测结论（详见 docs/api/read.md）

知网现代阅读器 `https://kns.cnki.net/reader/read?invoice=<token>` 是 **Vue SPA**，
其正文数据接口为内部 API（带 `vv` 等参数，且 pdf/img 需要 `idenid` 派生值），
**本次未复现**，故本模块**不走 SPA**。

**可用且稳定的取文通道**：详情页 `https://kns.cnki.net/kcms2/article/abstract?v=<token>`
内的 `https://bar.cnki.net/bar/download/order?id=<token>` 链接族：

| 链接 | 跳转落点 | 是否可用 |
|---|---|---|
| HTML 阅读 | `kns.cnki.net/reader/read?invoice=...`（SPA 外壳） | ✗ 正文不可直取 |
| 原版阅读 | 同上 | ✗ |
| **PDF 下载** | `docdown.cnki.net/docdown/fulltext/download?q=...` → **真实 PDF 字节流** | ✅ |
| CAJ 下载 | 同上 → CAJ 字节流 | ✗ 无解析器 |

→ 本模块取 **PDF**，用 **PyMuPDF** 做两件事：
1. **文本提取** → 供「HTML 阅读」视图（期刊/硕博 PDF 均含文本层，实测 期刊 18k / 硕士 98k / 博士 161k 字符）；
2. **按页栅格化** → 供「原版阅读」图像流视图。

## 合规与成本纪律（重要）
- PDF 下载走**机构下载额度**（只有 HTML 在线阅读才不消耗额度）。本模块**磁盘强缓存**，
  同一文献只下载一次；调用方应复用缓存，**绝不批量下载**。
- 仅本人机构账号、个人学习用途；**不二次分发**。
- 未订购库在详情页不提供下载入口 → 如实返回 `KIND_UNSUBSCRIBED`，**绝不伪装可取**。
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cnki_hover.http_client import CnkiHttpClient
from cnki_hover.log import get_logger, log_event
from cnki_hover.paths import OUTPUTS_DIR

LOG = get_logger("cnki_reader")

# ---- 可取性类型（公共契约，不得改名） ----
KIND_HTML = "html"                  # 可取正文文本
KIND_ORIGINAL = "original"          # 仅原版（图像流 / 无文本层）
KIND_TRIAL = "trial"                # 仅试读
KIND_UNSUBSCRIBED = "unsubscribed"  # 该库未订购
KIND_UNKNOWN = "unknown"

# ---- 端点常量 ----
ORDER_HOST = "bar.cnki.net/bar/download/order"
DETAIL_BASE = "https://kns.cnki.net"
READER_REF = "https://kns.cnki.net/reader/read"

CACHE_DIR = Path(OUTPUTS_DIR) / "cache" / "reader"

# PDF 魔数 / CAJ 魔数
_MAGIC_PDF = b"%PDF"
_MAGIC_CAJ = b"KDH"
_MAGIC_PNG = b"\x89PNG"
_MAGIC_JPEG = b"\xff\xd8\xff"

_DEFAULT_DPI = 150
_OPEN_DOC_LRU: "dict[str, Any]" = {}
_LRU_MAX = 2

# 供 verify / 上层断言命中缓存（模块级，最近一次读的标志）
last_read_from_cache: bool = False
last_meta: dict = {}


class ReadError(Exception):
    """取文失败（网络 / 结构变化 / 无可用通道）。附可读原因。"""


@dataclass
class ArticleContent:
    title: str
    kind: str
    text: str
    html: str
    word_count: int
    pages: int
    message: str
    detail_url: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == KIND_HTML and self.word_count > 0


@dataclass
class PageImage:
    page: int
    data: bytes
    mime: str
    stable_id: str = ""     # 该页属于哪篇文献 —— 供 UI 校验，防止"串论文"

    @property
    def is_valid_image(self) -> bool:
        d = self.data or b""
        return (d.startswith(_MAGIC_PNG) or d.startswith(_MAGIC_JPEG)) and len(d) > 512


# ---------------------------------------------------------------- 工具

def _cache_key(detail_url: str) -> str:
    """缓存键（sha1 前 20 位）。

    ⚠️ **绝不允许空字符串**：`sha1("")` 是固定值，一旦有调用方在拿不到稳定标识时
    用空串兜底，所有"没有标识"的文献就会**共用同一份缓存** —— 表现为
    「点开 A 却显示 B 的内容」（用户实测反馈）。这里直接抛错，把问题暴露出来。
    """
    if not detail_url:
        raise ReadError("缓存键不能为空：缺少稳定标识（stable_id / detail_url）")
    return hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:20]


def _ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _absolutize(url: str) -> str:
    if not url:
        return ""
    url = htmlmod.unescape(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return DETAIL_BASE + url
    return url


# ---------------------------------------------------------------- 解析层（可替换）

def _parse_order_links(detail_html: str) -> dict:
    """从详情页解析 `bar.cnki.net/bar/download/order` 链接族。

    返回 {"PDF下载": url, "HTML阅读": url, ...}；键取锚点可见文本，回退 `id` 属性。
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", detail_html, re.S):
        attrs, inner = m.group(1), re.sub(r"<[^>]+>", "", m.group(2) or "").strip()
        hm = re.search(r'href="([^"]+)"', attrs)
        if not hm or ORDER_HOST not in hm.group(1):
            continue
        url = _absolutize(hm.group(1))
        key = inner
        if not key:
            im = re.search(r'id="([^"]+)"', attrs)
            key = im.group(1) if im else "order"
        out.setdefault(key, url)
    return out


def _pick_pdf_link(links: dict) -> Optional[str]:
    """优先「PDF下载」；否则退回任何 label 含 PDF 的；再否则 None。"""
    if not links:
        return None
    if "PDF下载" in links:
        return links["PDF下载"]
    for k, v in links.items():
        if "pdf" in k.lower():
            return v
    return None


def _has_reader_link(links: dict) -> bool:
    return any(("阅读" in k) or ("html" in k.lower()) for k in links)


def _extract_title(detail_html: str) -> str:
    for pat in (r"<title>(.*?)</title>", r'<h1[^>]*>(.*?)</h1>'):
        m = re.search(pat, detail_html, re.S)
        if m:
            t = re.sub(r"<[^>]+>", "", m.group(1))
            t = htmlmod.unescape(t).strip()
            t = re.sub(r"[-_|]\s*中国知网.*$", "", t).strip()
            if t:
                return t
    return ""


def normalize_stable_id(sid: Optional[str]) -> str:
    """把各种来源的标识归一到唯一形态：**只取 filename 段**。

    背景（真实踩坑）：检索网格的 raw 给出的是 `filename|dbcode`（如 `YSXT202604008|CJFQ`），
    而详情页有时只解析得到 filename（如 `YSXT202604008`）。两者不归一 → 缓存键不一致 →
    同一篇文献被判为两条 → **重复下载 PDF**。知网 filename 本身在库内唯一，故统一只用它。
    """
    s = (sid or "").strip()
    if not s:
        return ""
    return s.split("|", 1)[0].strip()


def _stable_id(detail_html: str) -> str:
    """从详情页提取**稳定文献标识**（filename）。

    为什么必须这么做：`detail_url` 形如 `kcms2/article/abstract?v=<token>`，
    其中 `?v=` 是**每次检索都会变**的一次性令牌。若拿 URL 当缓存键，
    同一篇文献每次检索都会 cache miss → 反复下载 PDF → **白烧机构下载额度**。
    实测证据：同一文献二次读取命中缓存（0.00s），但另起一次检索后 URL 变化
    → 再次触发 7.2~17.9s 的整包下载。

    详情页提供稳定标识：
        <input type="hidden" id="param-filename" value="YSXT202604008">
        ...&dbcode=CJFQ&tablename=CJFDAUTO&filename=YSXT202604008
    取不到时返回空串（调用方将退回 URL 作键）。
    """
    for pat in (r'id="param-filename"[^>]*value="([^"]+)"',
                r'name="filename"[^>]*value="([^"]+)"',
                r'[?&]filename=([A-Za-z0-9._\-]+)'):
        m = re.search(pat, detail_html)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _extract_summary(detail_html: str) -> tuple[str, str, str]:
    """返回 (摘要, 关键词, 基金)。用于补全 A3 网格拿不到的字段。"""
    def _grab(pat: str) -> str:
        m = re.search(pat, detail_html, re.S)
        if not m:
            return ""
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        return re.sub(r"\s+", " ", htmlmod.unescape(txt)).strip()

    summary = _grab(r'id="ChDivSummary"[^>]*>(.*?)</p>') or _grab(r"id=\"ChDivSummary\"[^>]*>(.*?)</div>")
    keyword = _grab(r'class="keywords"[^>]*>(.*?)</p>') or _grab(r"关键词[：:](.{0,300}?)(?:<|$)")
    fund = _grab(r"基金[：:](.{0,300}?)(?:<|$)")
    return summary, keyword, fund


_SENT_END = "。！？；：.!?;:）)】」』”\"'"
_HEADING_HINT = ("第", "一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、",
                 "参考文献", "摘要", "关键词", "引言", "结论")

# 全角标点 -> 半角（**仅在两侧都是 ASCII 字母/数字时才替换**，见 _fix_ascii_punct）
_ASCII_PUNCT_PAIRS = {
    0xFF0D: "-", 0xFF0E: ".", 0xFF0F: "/", 0xFF1A: ":", 0xFF1B: ";",
    0xFF0C: ",", 0xFF08: "(", 0xFF09: ")", 0xFF05: "%", 0xFF0B: "+",
    0xFF1D: "=", 0xFF06: "&", 0xFF20: "@", 0xFF03: "#",
}


def _normalize_fullwidth(text: str) -> str:
    """把知网 PDF 里的**全角西文**归一为半角。

    为什么必须做：知网排版把西文也做成全角（`Ｒｅｓｅａｒｃｈ`、`Ｖｏｌ．５`），
    词间分隔用 **U+3000 全角空格**。若原样输出，英文会显得"乱码"；
    更糟的是 `str.strip()` 会把 U+3000 当空白吃掉，导致**词与词粘在一起**
    （实测某篇英文摘要区间内普通空格数为 0 —— 全被 strip 掉了）。

    规则（保守，不动中文语境）：
    - 全角字母 Ａ-Ｚ ａ-ｚ、全角数字 ０-９ → 半角
    - U+3000 全角空格 → 普通空格
    - 全角标点**仅在两侧都是 ASCII 字母/数字时**转半角（如 `E－mail`→`E-mail`、`Vol．5`→`Vol.5`），
      这样中文里的 `摘要：`、`（…）`、`，` 等**保持全角不变**。
    """
    if not text:
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFF10 <= cp <= 0xFF19 or 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    t = "".join(out)

    if _ASCII_PUNCT_PAIRS:
        cls = "".join(re.escape(chr(c)) for c in _ASCII_PUNCT_PAIRS)

        def _rep(m):
            a, p, b = m.group(1), m.group(2), m.group(3)
            return a + _ASCII_PUNCT_PAIRS.get(ord(p), p) + b

        t = re.sub(r"([0-9A-Za-z])([%s])([0-9A-Za-z])" % cls, _rep, t)
    return t


def _join_lines(a: str, b: str) -> str:
    """按中英混排规则拼接两行：CJK 直接接，ASCII 词之间补空格。"""
    if not a:
        return b
    if a[-1].isascii() and a[-1].isalnum() and b[:1].isascii() and b[:1].isalnum():
        return a + " " + b
    return a + b


def _merge_paragraphs(text: str) -> list:
    """把 PDF 抽出的**硬换行**合并成真正的段落。

    为什么必须做：PyMuPDF 逐行返回可视行，直接一行一段会产出数百个块，
    既拖慢 Qt 富文本排版（实测首屏 ~770ms），阅读体验也碎。
    规则：上一行以句末标点收尾 → 断段；否则并入本段。明显是小标题的短行单独成段。
    """
    text = _normalize_fullwidth(text)
    out: list = []
    buf = ""
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            if buf:
                out.append(buf)
                buf = ""
            continue
        # 明显是小标题的行单独成段，**不要并进上一段**（否则小节标题会被吞掉）。
        # 除了「第X」「……：」这类提示词，还要认「1 YOLOv11算法原理」这种数字编号标题。
        is_num_head = (len(ln) <= 40 and _HEAD_NUM.match(ln)
                       and not (set(ln) & _HEAD_FORBID))
        if is_num_head or (len(ln) <= 24 and (ln.startswith(_HEADING_HINT)
                                              or ln.endswith(("：", ":")))):
            if buf:
                out.append(buf)
                buf = ""
            out.append(ln)
            continue
        if buf and buf[-1] in _SENT_END:
            out.append(buf)
            buf = ""
        buf = _join_lines(buf, ln)
    if buf:
        out.append(buf)
    return out


def _drop_lead_meta(paras: list, title: str) -> list:
    """丢掉正文开头重复的「标题 / 作者 / 单位」块。

    PDF 首屏会带这些行，而阅读窗口顶部**已经显示**了标题与作者信息；
    留在正文里只会变成几段莫名其妙的短句。
    """
    out = list(paras)
    dropped = 0
    while out and dropped < 4:
        s = out[0].strip()
        if not s:
            out.pop(0)
            dropped += 1
            continue
        is_title = bool(title) and (s == title or (len(s) >= 8 and title.startswith(s[:8])))
        if (is_title or _AFFIL.search(s) or _AUTHOR_LINE.match(s)
                or (dropped < 2 and len(s) <= 14)):
            out.pop(0)
            dropped += 1
            continue
        break
    return out


_AFFIL = re.compile(r"[（(][^）)]*(大学|学院|研究院|研究所|公司|实验室|中心|局|院)[^）)]*[）)]")
_AUTHOR_LINE = re.compile(r"^[\u4e00-\u9fa5]{2,4}([，,、]\s*[\u4e00-\u9fa5]{2,4}){1,6}$")


def _text_to_html(text: str, title: str = "", meta: str = "") -> str:
    """把正文文本排成**论文样式**的 HTML（而不是一坨段落）。

    排版要做四件事：
    1. **剔除每页重复的页眉页脚** —— 知网 PDF 每页都带刊头（`第49卷 第8期 … Aug. 2026 Vol. 49 No. 8`）
       与 `中国知网 https://www.cnki.net`。判据：同一行>=(3)次逐字重复 → 判为页眉页脚丢弃。
    2. **识别层级** —— 标题 / 摘要 / 关键词 / 中图分类号 / 一二三级标题 / 正文。
    3. **正文首行缩进 2 字符**（中文论文惯例）+ 两端对齐 + 段间距。
    4. 摘要与关键词用带色块的样式块，和正文区分开。
    """
    body_lines = _strip_running_lines(text)
    paras = _drop_lead_meta(_merge_paragraphs("\n".join(body_lines)), title)

    parts = []
    for p in paras:
        kind = _classify_paragraph(p)
        esc = htmlmod.escape(p)
        if kind == "abstract":
            parts.append('<p class="abs">%s</p>' % esc)
        elif kind == "keywords":
            parts.append('<p class="kw">%s</p>' % esc)
        elif kind == "clc":
            parts.append('<p class="clc">%s</p>' % esc)
        elif kind == "h2":
            parts.append("<h2>%s</h2>" % esc)
        elif kind == "h3":
            parts.append("<h3>%s</h3>" % esc)
        else:
            parts.append("<p>%s</p>" % esc)

    head = ""
    if title:
        head += '<h1 class="title">%s</h1>' % htmlmod.escape(title)
    if meta:
        head += '<div class="meta">%s</div>' % htmlmod.escape(meta)

    return (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:'Microsoft YaHei','PingFang SC',sans-serif;font-size:15px;"
        "line-height:1.9;color:#DDE2EA;margin:0;padding:26px 64px 70px;}"
        "h1.title{font-size:21px;font-weight:700;text-align:center;line-height:1.45;"
        "margin:8px 0 12px;color:#F2F4F8;}"
        "div.meta{text-align:center;color:#8A93A2;font-size:12.5px;margin:0 0 20px;}"
        "p.abs{text-indent:0;background:rgba(255,255,255,12);border-left:3px solid #4C8DFF;"
        "padding:10px 14px;margin:10px 0;color:#C6CDD8;font-size:14px;border-radius:0 6px 6px 0;}"
        "p.kw{text-indent:0;color:#9AA3B2;font-size:13.5px;margin:4px 0 14px;}"
        "p.clc{text-indent:0;color:#7F8794;font-size:12.5px;margin:2px 0;}"
        "h2{font-size:16.5px;font-weight:700;margin:22px 0 8px;color:#EDEFF3;"
        "border-left:3px solid #4C8DFF;padding-left:9px;}"
        "h3{font-size:15.5px;font-weight:600;margin:16px 0 6px;color:#E4E8EF;}"
        "p{margin:0 0 10px;text-indent:2em;text-align:justify;}"
        "</style></head><body>%s%s</body></html>" % (head, "".join(parts))
    )


def _strip_running_lines(text: str) -> list:
    """剔除在各页重复出现的页眉/页脚行（按"同一条逐字重复 >=3 次"判定）。"""
    lines = (text or "").splitlines()
    norm = [re.sub(r"\s+", "", ln) for ln in lines]
    counts: dict = {}
    for n in norm:
        if 6 <= len(n) <= 80:
            counts[n] = counts.get(n, 0) + 1
    out = []
    for ln, n in zip(lines, norm):
        if n and counts.get(n, 0) >= 3:
            continue                                   # 页眉/页脚/刊头
        s = ln.strip()
        if re.match(r"^中国知网\s*https?://", s):
            continue
        if re.match(r"^(https?://)?www\.cnki\.net\s*$", s, re.I):
            continue
        if re.match(r"^第\s*\d+\s*页\s*$", s):
            continue
        out.append(ln)
    return out


# 小节标题：编号 + （空白**或**直接跟汉字）+ 文字。
# 例：「1 YOLOv11算法原理」「2.1 数据来源」「1引言」（知网有的排版编号后无空格）。
# 早期写成 `^\d{1,2}(\.\d{1,2}){0,2}[\s、\.]?\s*\S`，因为 `\d{1,2}` 可以只吃一位、
# `\S` 又能接上紧随的数字，导致 `'90'` / `'6 2'` / `'1 - α ()'` 这类页码与公式碎片被当成标题。
_HEAD_NUM = re.compile(r"^\d{1,2}(\.\d{1,2}){0,2}([\s\u3000、]+|(?=[\u4e00-\u9fa5]))[\u4e00-\u9fa5A-Za-z]")
_HEAD_FORBID = set("=∑∫√±×÷≈≤≥，。；：！？（）()[]{}+-*/^_")
_HEAD_KEYS = ("摘要", "关键词", "中图分类号", "文献标志码", "Abstract", "Keywords",
              "引言", "前言", "结论", "结束语", "讨论", "参考文献", "致谢")


def _classify_paragraph(p: str) -> str:
    """把段落分类，用于套不同版式。

    注意：分类前先把空白压掉再比较 —— 知网排版常写成「摘 要：」（中间是全角空格）。
    """
    s = (p or "").strip()
    if not s:
        return "p"
    sn = re.sub(r"\s+", "", s)
    if sn.startswith("摘要") or sn.startswith("Abstract"):
        return "abstract"
    if sn.startswith("关键词") or sn.startswith("Keywords"):
        return "keywords"
    if sn.startswith("中图分类号") or sn.startswith("文献标志码"):
        return "clc"
    if len(sn) <= 30 and any(sn.startswith(k) for k in _HEAD_KEYS):
        return "h2"
    if (len(s) <= 40 and _HEAD_NUM.match(s)
            and not (set(s) & _HEAD_FORBID)
            and not s.endswith(("。", "，", "；", "！", "？", ".", ",", ";", ":"))):
        return "h3"
    return "p"


# ---------------------------------------------------------------- 网络层

def _fetch_detail(detail_url: str, client: Optional[CnkiHttpClient] = None) -> str:
    client = _client(client)          # 惰性解析：只有真要联网时才建立/校验会话
    url = _absolutize(detail_url)
    if not url:
        raise ReadError("detail_url 为空")
    r = client.get(url, headers={"Referer": "https://kns.cnki.net/kns8s/defaultresult/index"})
    if r.status_code != 200:
        raise ReadError("详情页请求失败：HTTP %s" % r.status_code)
    return r.content.decode("utf-8", errors="replace")


def _download_bytes(order_url: str, referer: str, client: CnkiHttpClient) -> tuple[bytes, str]:
    """跟随 order 链接取字节流。返回 (content, kind)，kind ∈ {pdf,caj,html,other}。"""
    r = client.get(order_url, headers={"Referer": referer})
    body = r.content or b""
    ct = (r.headers.get("Content-Type") or "").lower()
    if body.startswith(_MAGIC_PDF) or "application/pdf" in ct:
        return body, "pdf"
    if body.startswith(_MAGIC_CAJ) or "caj" in ct:
        return body, "caj"
    if b"<!DOCTYPE" in body[:64] or b"<html" in body[:256].lower():
        return body, "html"
    return body, "other"


# ---------------------------------------------------------------- PDF 句柄缓存

def _open_pdf(pdf_path: Path):
    key = pdf_path.name
    doc = _OPEN_DOC_LRU.get(key)
    if doc is not None:
        return doc
    try:
        import pymupdf  # noqa: PLC0415
    except Exception:  # pragma: no cover
        import fitz as pymupdf  # type: ignore # noqa: PLC0415
    if len(_OPEN_DOC_LRU) >= _LRU_MAX:
        _k, _d = next(iter(_OPEN_DOC_LRU.items()))
        try:
            _d.close()
        except Exception:
            pass
        _OPEN_DOC_LRU.pop(_k, None)
    doc = pymupdf.open(str(pdf_path))
    _OPEN_DOC_LRU[key] = doc
    return doc


def _pymupdf():
    try:
        import pymupdf  # noqa: PLC0415
        return pymupdf
    except Exception:  # pragma: no cover
        import fitz  # type: ignore # noqa: PLC0415
        return fitz


# ---------------------------------------------------------------- 公共 API

def resolve_order_links(detail_url: str, client: Optional[CnkiHttpClient] = None) -> dict:
    """暴露详情页的 order 链接族，便于排障与文档核对。"""
    client = client or _client()
    return _parse_order_links(_fetch_detail(detail_url, client))


def _client(client: Optional[CnkiHttpClient]) -> CnkiHttpClient:
    if client is not None:
        return client
    from cnki_api.auth import get_authenticated_client  # noqa: PLC0415
    return get_authenticated_client()


def ensure_pdf(detail_url: str, client: Optional[CnkiHttpClient] = None,
               stable_id: Optional[str] = None) -> Path:
    """确保本地有该文献的 PDF，返回路径。命中缓存则**只发一次详情页请求（若有需要）**。

    stable_id：稳定文献标识（如 "YSXT202604008|CJFQ"）。传入时可先命中缓存、完全不联网。
               不传时会先取一次详情页来推导（详情页请求**不消耗下载额度**）。
    无 PDF 通道时抛 ReadError（附可读原因）。
    """
    global last_read_from_cache
    cache = _ensure_cache_dir()
    detail_url = _absolutize(detail_url)

    sid = normalize_stable_id(stable_id)
    # 快路径：已知稳定标识 → 先看本地 PDF，命中则**不解析 client、不联网**
    if sid:
        _p = cache / ("%s.pdf" % _cache_key(sid))
        if _p.exists() and _p.stat().st_size > 1024:
            last_read_from_cache = True
            log_event(LOG, "pdf_cache_hit", key=_cache_key(sid), sid=sid,
                      bytes=_p.stat().st_size)
            return _p

    client = _client(client)          # 需要联网了才建立/校验会话
    detail_html: Optional[str] = None
    if not sid:
        detail_html = _fetch_detail(detail_url, client)
        sid = _stable_id(detail_html) or detail_url

    key = _cache_key(sid)
    pdf_path = cache / ("%s.pdf" % key)
    if pdf_path.exists() and pdf_path.stat().st_size > 1024:
        last_read_from_cache = True
        log_event(LOG, "pdf_cache_hit", key=key, sid=sid, bytes=pdf_path.stat().st_size)
        return pdf_path

    last_read_from_cache = False
    if detail_html is None:
        detail_html = _fetch_detail(detail_url, client)
    links = _parse_order_links(detail_html)
    pdf_link = _pick_pdf_link(links)

    if pdf_link:
        body, kind = _download_bytes(pdf_link, detail_url, client)
        if kind == "pdf":
            pdf_path.write_bytes(body)
            (cache / ("%s.json" % key)).write_text(
                json.dumps({"detail_url": detail_url, "stable_id": sid, "order_links": links,
                            "title": _extract_title(detail_html), "kind": "pdf",
                            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            log_event(LOG, "pdf_downloaded", key=key, sid=sid, bytes=len(body))
            return pdf_path
        if kind == "caj":
            raise ReadError("该文献仅提供 CAJ 格式（当前无 CAJ 解析器），无法提取正文/页图")
        if kind == "html":
            raise ReadError("PDF 下载入口被重定向回阅读器页面，未能取到文件")

    if _has_reader_link(links):
        raise ReadError("该文献仅提供在线 HTML/原版阅读（阅读器为前端 SPA，正文接口未复现），无法取正文")
    raise ReadError("详情页未提供下载入口 —— 可能该库未订阅，或文献无全文")


def _extract_pdf_text(doc) -> str:
    """按**阅读顺序**抽取整本 PDF 的文本（双栏正确）。

    为什么不能用逐"行"或 `get_text("blocks")`（实测踩坑）：
    知网期刊多为**双栏**，PDF 内容流顺序是「左栏一行、右栏一行」交错，
    逐行合并会把左右两栏拼在一起 —— 实测出现 5 个 1900+ 字符的巨型段落、
    小节标题被切碎（"0引言另一方面，需通过动态调整车距…"左右栏混在一行），
    用户看到的就是"没法阅读的一坨"。`get_text("blocks")` 在这类 PDF 上
    仍会产出跨栏行块，同样不行。

    正解：取**行级 bbox**（`get_text("dict")`），按 **x 坐标分栏** ——
    通栏行（标题/摘要）按 y 穿插，先读左栏再读右栏。行块间用双换行，
    让下游 `_merge_paragraphs` 在空行处断段，小节标题得以独立成段。
    """
    page_parts: list = []
    header_counter: dict = {}

    for page in doc:
        r = page.rect
        rows = []
        try:
            d = page.get_text("dict")
        except Exception:  # noqa: BLE001
            continue
        for blk in d.get("blocks", []) or []:
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []) or []:
                x0, y0, x1, y1 = ln.get("bbox", (0, 0, 0, 0))
                txt = "".join(sp.get("text", "") for sp in ln.get("spans", []) or [])
                txt = txt.strip()
                if txt:
                    rows.append((float(x0), float(y0), float(x1), float(y1), txt))
        if not rows:
            continue

        # —— 页眉 / 页脚：贴上下边缘（8% / 6%）
        body, edges = [], []
        for x0, y0, x1, y1, txt in rows:
            if y1 <= r.height * 0.08 or y0 >= r.height * 0.94:
                edges.append(re.sub(r"\s+", "", txt))
            else:
                body.append((x0, y0, x1, y1, txt))
        for e in edges:
            header_counter[e] = header_counter.get(e, 0) + 1
        if not body:
            body = rows

        # —— 跨页逐字重复 ≥3 次的行 = 页眉页脚，丢弃
        def _not_header(t):
            k = re.sub(r"\s+", "", t)
            return header_counter.get(k, 0) < 3
        body = [b for b in body if _not_header(b[4])]

        # —— 乱码块（字体缺字形）：连续 ≥3 个 ■/□/� 视为装饰，丢弃
        body = [b for b in body if not re.search(r"[■□]{3,}|�{3,}", b[4])]
        if not body:
            continue

        # —— 分栏
        mid = r.width * 0.5
        tol = r.width * 0.06
        full = [b for b in body if b[0] < mid - tol and b[2] > mid + tol]
        left = [b for b in body if b[2] <= mid + tol]
        right = [b for b in body if b[0] >= mid - tol]
        other = [b for b in body if b not in full and b not in left and b not in right]
        two_col = len(left) >= 3 and len(right) >= 3

        def _ys(b):
            return (round(b[1] / 10.0), b[0])

        # —— 段落起点检测：论文正文段落**首行有缩进**（约 2 字符 ≈ 9pt）。
        # 只靠"上一行是否以句末标点结尾"断段太弱，单栏论文会把整节并成一段（实测 2012 字符）。
        def _emit(group, out):
            if not group:
                return
            base = min(b[0] for b in group)
            for b in group:
                if b[0] - base > 9.0:
                    out.append("")          # 空行 = 段落边界（下游据此断段）
                out.append(b[4])

        parts: list = []
        if not two_col:
            _emit(sorted(body, key=_ys), parts)
        else:
            left.sort(key=_ys)
            right.sort(key=_ys)
            other.sort(key=_ys)
            # 通栏行若位于所有栏行之前（论文标题/作者/摘要区）→ 前置
            first_col_y = min((b[1] for b in left + right), default=1e9)
            head = sorted([b for b in full if b[1] < first_col_y - 2], key=_ys)
            rest = [b for b in full if b not in head]
            # 其余通栏行与左栏按 y 归并（例如跨栏的图注/公式）
            merged = sorted(left + rest, key=_ys)
            for g in (head, merged, right, other):
                _emit(g, parts)
                parts.append("")            # 组间也断段

        page_parts.append("\n".join(parts))

    # 行块间用双换行 → 下游 _merge_paragraphs 遇空行断段，标题块得以独立
    return "\n\n".join(page_parts)


def read_html(detail_url: str, client: Optional[CnkiHttpClient] = None,
              use_cache: bool = True, stable_id: Optional[str] = None) -> ArticleContent:
    """取正文文本（「HTML 阅读」视图的数据源）。

    **绝不抛未捕获异常给 UI**：任何失败都返回带明确 message 的 ArticleContent。
    缓存键基于**稳定文献标识**（filename|dbcode），不受 `?v=<token>` 变化影响。
    """
    global last_meta, last_read_from_cache
    detail_url = _absolutize(detail_url)
    cache = _ensure_cache_dir()
    # ⚠️ 这里**刻意不先解析 client**：解析会触发一次认证在线探针（≈350ms）。
    # 若正文已在本地缓存，读取应当**零网络、零会话校验**（离线可读）。
    # 早期版本在函数入口就 `_client()`，导致「二次打开命中缓存」仍要 ~370ms。

    def _try_cached(sid_value: str, fallback_title: str = ""):
        """命中文本缓存则直接返回 ArticleContent，否则 None。"""
        if not use_cache or not sid_value:
            return None
        k = _cache_key(sid_value)
        tp = cache / ("%s.txt" % k)
        mp = cache / ("%s.meta.json" % k)
        if not (tp.exists() and mp.exists()):
            return None
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            t = tp.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "text_cache_corrupt", key=k, error=str(e)[:120])
            return None
        globals()["last_read_from_cache"] = True
        globals()["last_meta"] = m
        log_event(LOG, "text_cache_hit", key=k, sid=sid_value, word_count=m.get("word_count"))
        return ArticleContent(
            title=m.get("title", fallback_title), kind=m.get("kind", KIND_HTML),
            text=t, html=_text_to_html(t, m.get("title", "") or fallback_title),
            word_count=int(m.get("word_count", 0)), pages=int(m.get("pages", 0)),
            message=m.get("message", "（本地缓存）"), detail_url=detail_url, meta=m,
        )

    # 快路径：调用方已给稳定标识 → 不联网即可命中（预热后点击首屏走的就是这条路）
    _sid_given = normalize_stable_id(stable_id)
    if _sid_given:
        hit = _try_cached(_sid_given)
        if hit is not None:
            return hit

    # 取详情页（不消耗下载额度）—— 缺 stable_id 时也靠它推导
    try:
        detail = _fetch_detail(detail_url, client)
    except Exception as e:
        return ArticleContent(title="", kind=KIND_UNKNOWN, text="", html="", word_count=0,
                              pages=0, message="详情页请求失败：%s" % e, detail_url=detail_url)

    title = _extract_title(detail)
    summary, keyword, fund = _extract_summary(detail)
    sid = _sid_given or _stable_id(detail) or detail_url

    if not _sid_given:
        hit = _try_cached(sid, title)
        if hit is not None:
            return hit

    key = _cache_key(sid)
    txt_path = cache / ("%s.txt" % key)
    meta_path = cache / ("%s.meta.json" % key)

    try:
        pdf_path = ensure_pdf(detail_url, client=client, stable_id=sid)
    except ReadError as e:
        links: dict = {}
        try:
            links = _parse_order_links(detail)
        except Exception:
            pass
        if _has_reader_link(links):
            # 有在线阅读入口但取不到 PDF：如实标记为「仅原版/受限」
            kind = KIND_ORIGINAL
        elif links:
            kind = KIND_TRIAL
        else:
            kind = KIND_UNSUBSCRIBED
        msg = str(e)
        out = ArticleContent(title=title, kind=kind, text="", html="", word_count=0, pages=0,
                             message=msg, detail_url=detail_url,
                             meta={"summary": summary, "keyword": keyword, "fund": fund})
        last_read_from_cache = False
        last_meta = out.meta
        log_event(LOG, "read_html_degraded", key=key, kind=kind, reason=msg[:120])
        return out

    # 从 PDF 抽文本：用**版面块 + 分栏顺序**，而不是逐"行"。
    # 为什么（用户实测反馈）：知网期刊多为**双栏**，逐行抽取会返回大量长度为 1 的行
    # （栏间碎片、公式、上下标），逐行合并会把**左右两栏拼在一起**，5 个段落长达 1900+ 字符，
    # 小节标题也被切碎 —— 用户看到的就是"没法阅读的一坨"。
    # blocks 模式返回的是版面块（含 bbox），按栏排序后再拼，段落结构才是对的。
    try:
        doc = _open_pdf(pdf_path)
        text = _extract_pdf_text(doc)
        pages = doc.page_count
    except Exception as e:
        return ArticleContent(title=title, kind=KIND_UNKNOWN, text="", html="", word_count=0,
                              pages=0, message="PDF 解析失败：%s" % e, detail_url=detail_url)

    word_count = len(re.sub(r"\s+", "", text))

    if word_count < 100:
        # 无文本层（扫描版）→ 只能看图
        out = ArticleContent(
            title=title, kind=KIND_ORIGINAL, text="", html="", word_count=0, pages=pages,
            message="该 PDF 无文本层（扫描版），仅支持原版图像阅读", detail_url=detail_url,
            meta={"summary": summary, "keyword": keyword, "fund": fund, "text_source": "pdf"})
        last_meta = out.meta
        return out

    meta = {"title": title, "kind": KIND_HTML, "word_count": word_count, "pages": pages,
            "message": "正文由机构授权 PDF 全文提取", "summary": summary,
            "keyword": keyword, "fund": fund, "text_source": "pdf",
            "stable_id": sid, "cache_key": key,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        txt_path.write_text(text, encoding="utf-8")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    last_read_from_cache = False   # 走到这里说明是回源读取（未命中文本缓存）
    last_meta = meta
    log_event(LOG, "read_html_done", key=key, sid=sid, word_count=word_count, pages=pages)
    return ArticleContent(title=title, kind=KIND_HTML, text=text, html=_text_to_html(text, title),
                          word_count=word_count, pages=pages,
                          message=meta["message"], detail_url=detail_url, meta=meta)


def read_original(detail_url: str, page: int = 1, client: Optional[CnkiHttpClient] = None,
                  use_cache: bool = True, dpi: int = _DEFAULT_DPI,
                  stable_id: Optional[str] = None) -> PageImage:
    """取原版第 page 页（1 基）的 PNG 图像。失败抛 ReadError。"""
    detail_url = _absolutize(detail_url)
    sid = normalize_stable_id(stable_id)
    cache = _ensure_cache_dir()

    # 快路径：已知稳定标识且页图已缓存 → 零网络返回（不解析 client）
    def _cached_png(sid_value: str):
        if not (use_cache and sid_value):
            return None
        p = cache / ("%s_p%04d_%d.png" % (_cache_key(sid_value), int(page), int(dpi)))
        return p if (p.exists() and p.stat().st_size > 512) else None

    hit = _cached_png(sid)
    if hit is not None:
        global last_read_from_cache
        last_read_from_cache = True
        return PageImage(page=int(page), data=hit.read_bytes(), mime="image/png", stable_id=sid)

    client = _client(client)
    if not sid:
        try:
            sid = _stable_id(_fetch_detail(detail_url, client)) or detail_url
        except Exception:  # noqa: BLE001
            sid = detail_url
        hit = _cached_png(sid)
        if hit is not None:
            last_read_from_cache = True
            return PageImage(page=int(page), data=hit.read_bytes(), mime="image/png", stable_id=sid)

    key = _cache_key(sid)
    png_path = cache / ("%s_p%04d_%d.png" % (key, int(page), int(dpi)))

    last_read_from_cache = False
    pdf_path = ensure_pdf(detail_url, client=client, stable_id=sid)
    doc = _open_pdf(pdf_path)
    idx = int(page) - 1
    if idx < 0 or idx >= doc.page_count:
        raise ReadError("页码越界：%s（共 %d 页）" % (page, doc.page_count))
    pix = doc[idx].get_pixmap(dpi=int(dpi))
    data = pix.tobytes("png")
    try:
        png_path.write_bytes(data)
    except Exception:
        pass
    log_event(LOG, "read_original_done", key=key, page=int(page), bytes=len(data))
    return PageImage(page=int(page), data=data, mime="image/png")


def render_page(detail_url: str, page: int = 1, target_width: int = 820,
                client: Optional[CnkiHttpClient] = None, stable_id: Optional[str] = None,
                use_cache: bool = True) -> PageImage:
    """按**目标像素宽度**渲染原版某页，返回 PNG。

    与 `read_original` 的区别：`read_original` 按固定 dpi 渲染（宽度不可控），
    这里用缩放矩阵把位图宽度**精确对齐到 target_width** —— 页面视图据此才不会横向溢出。
    缓存键含目标宽度，缩放变化不会互相污染。
    """
    global last_read_from_cache
    detail_url = _absolutize(detail_url)
    sid = normalize_stable_id(stable_id)
    cache = _ensure_cache_dir()
    target_width = max(120, int(target_width))

    def _cached(sid_value: str):
        if not (use_cache and sid_value):
            return None
        p = cache / ("%s_p%04d_w%04d.png" % (_cache_key(sid_value), int(page), target_width))
        return p if (p.exists() and p.stat().st_size > 512) else None

    hit = _cached(sid)
    if hit is not None:
        last_read_from_cache = True
        return PageImage(page=int(page), data=hit.read_bytes(), mime="image/png", stable_id=sid)

    client = _client(client)
    if not sid:
        try:
            sid = _stable_id(_fetch_detail(detail_url)) or detail_url
        except Exception:  # noqa: BLE001
            sid = detail_url
        hit = _cached(sid)
        if hit is not None:
            last_read_from_cache = True
            return PageImage(page=int(page), data=hit.read_bytes(), mime="image/png", stable_id=sid)

    pdf_path = ensure_pdf(detail_url, client=client, stable_id=sid)
    doc = _open_pdf(pdf_path)
    idx = int(page) - 1
    if idx < 0 or idx >= doc.page_count:
        raise ReadError("页码越界：%s（共 %d 页）" % (page, doc.page_count))
    pymupdf = _pymupdf()
    pg = doc[idx]
    scale = float(target_width) / float(pg.rect.width or 1.0)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
    data = pix.tobytes("png")
    png_path = cache / ("%s_p%04d_w%04d.png" % (_cache_key(sid), int(page), target_width))
    try:
        png_path.write_bytes(data)
    except Exception:  # noqa: BLE001
        pass
    last_read_from_cache = False
    log_event(LOG, "render_page_done", key=_cache_key(sid), page=int(page),
              target_w=target_width, bytes=len(data), px="%dx%d" % (pix.width, pix.height))
    return PageImage(page=int(page), data=data, mime="image/png", stable_id=sid)


def get_page_count(detail_url: str, client: Optional[CnkiHttpClient] = None,
                   stable_id: Optional[str] = None) -> int:
    """原版总页数；取不到返回 0。"""
    try:
        return int(_open_pdf(ensure_pdf(detail_url, client=_client(client),
                                        stable_id=stable_id)).page_count)
    except Exception:
        return 0


def page_size_pt(detail_url: str, page: int = 1, client: Optional[CnkiHttpClient] = None,
                 stable_id: Optional[str] = None) -> tuple:
    """某页的**原始尺寸（PDF 点，1pt = 1/72 inch）**。取不到返回 (0.0, 0.0)。

    C3 用它反算缩放矩阵：只有让渲染位图宽度**正好等于**逻辑页宽，
    页面才不会左右溢出被裁（早期直接按固定 dpi 渲染 → 1240px 宽的图塞进 820px 的槽位，
    横向溢出 420px，用户看到的就是"显示不完整"）。
    """
    try:
        doc = _open_pdf(ensure_pdf(detail_url, client=_client(client), stable_id=stable_id))
        idx = max(0, min(int(page) - 1, doc.page_count - 1))
        r = doc[idx].rect
        return (float(r.width), float(r.height))
    except Exception:
        return (0.0, 0.0)


def detect_availability(detail_url: str, client: Optional[CnkiHttpClient] = None,
                        stable_id: Optional[str] = None) -> str:
    """探测可取性，返回 KIND_*。不发 PDF 下载请求（省额度）。"""
    client = _client(client)
    detail_url = _absolutize(detail_url)

    try:
        detail = _fetch_detail(detail_url, client)
    except Exception:
        return KIND_UNKNOWN

    sid = normalize_stable_id(stable_id) or _stable_id(detail) or detail_url
    txt_path = _ensure_cache_dir() / ("%s.txt" % _cache_key(sid))

    links = _parse_order_links(detail)
    if _pick_pdf_link(links):
        if txt_path.exists():
            return KIND_HTML
        # 有 PDF 通道：是否含文本层需下载后才知道，先按 HTML 乐观标记
        return KIND_HTML
    if _has_reader_link(links):
        return KIND_ORIGINAL
    if links:
        return KIND_TRIAL
    return KIND_UNSUBSCRIBED


def get_summary(detail_url: str, client: Optional[CnkiHttpClient] = None) -> dict:
    """取详情页的 摘要/关键词/基金/标题（补全 A3 网格缺失字段）。"""
    try:
        detail = _fetch_detail(_absolutize(detail_url), _client(client))
    except Exception as e:
        return {"title": "", "summary": "", "keyword": "", "fund": "", "error": str(e)}
    s, k, f = _extract_summary(detail)
    return {"title": _extract_title(detail), "summary": s, "keyword": k, "fund": f}


def prefetch_pdf(detail_url: str, client: Optional[CnkiHttpClient] = None,
                 stable_id: Optional[str] = None) -> Optional[Path]:
    """供 C2 预取使用：后台把 PDF 拉到本地（幂等、命中缓存即返回）。"""
    try:
        return ensure_pdf(detail_url, client=client, stable_id=stable_id)
    except Exception as e:
        log_event(LOG, "prefetch_pdf_failed", error=str(e)[:150])
        return None


def has_cached_text(stable_id: Optional[str]) -> bool:
    """本地是否已有该文献的正文缓存（供 C2 判断「能否零网络秒开」）。"""
    sid = normalize_stable_id(stable_id)
    if not sid:
        return False
    k = _cache_key(sid)
    c = _ensure_cache_dir()
    return (c / ("%s.txt" % k)).exists() and (c / ("%s.meta.json" % k)).exists()


def has_cached_pdf(stable_id: Optional[str]) -> bool:
    """本地是否已有该文献的原版 PDF（供 C2/C3 判断预热状态）。"""
    sid = normalize_stable_id(stable_id)
    if not sid:
        return False
    p = _ensure_cache_dir() / ("%s.pdf" % _cache_key(sid))
    return p.exists() and p.stat().st_size > 1024


def get_cached_text(stable_id: Optional[str]) -> Optional[str]:
    """直接读本地正文缓存（无网络）。不存在返回 None。"""
    sid = normalize_stable_id(stable_id)
    if not sid:
        return None
    p = _ensure_cache_dir() / ("%s.txt" % _cache_key(sid))
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


def get_cached_meta(stable_id: Optional[str]) -> dict:
    sid = normalize_stable_id(stable_id)
    if not sid:
        return {}
    p = _ensure_cache_dir() / ("%s.meta.json" % _cache_key(sid))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def close() -> None:
    """释放所有打开的 PDF 句柄。"""
    for k, d in list(_OPEN_DOC_LRU.items()):
        try:
            d.close()
        except Exception:
            pass
        _OPEN_DOC_LRU.pop(k, None)
