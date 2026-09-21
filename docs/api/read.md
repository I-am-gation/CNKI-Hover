# 知网阅读 / 取文接口文档（A4）

> 阶段：Phase A — A4 任务 ｜ 依赖 A2（登录态）、A3（`search.py` 提供 `detail_url`）
> 结论：**HTML 在线阅读器的正文数据接口未复现（前端签名 SPA）**；
> 但**已打通一条稳定、可复现的取文通道**：详情页内的 `bar.cnki.net` PDF 下载入口
> → 机构授权 PDF 全文 → 本地用 PyMuPDF 做「文本提取」与「按页栅格化」。
> 验收：`tools/verify_read.py` **6/6 PASS, EXIT=0**（期刊正文 18218 字 / 硕博 157 页首页图合法）。

---

## 0. 关键事实速查

| 项 | 值 |
|---|---|
| 阅读器页 | `https://kns.cnki.net/reader/read?invoice=<token>`（**Vue SPA，正文不可直取**） |
| 详情页 | `https://kns.cnki.net/kcms2/article/abstract?v=<token>`（含 标题/摘要/关键词/基金，**不含全文**） |
| **可用取文通道** | 详情页内 `https://bar.cnki.net/bar/download/order?id=<token>` → **PDF 下载** |
| PDF 落点 | `https://docdown.cnki.net/docdown/fulltext/download?q=<token>` → `application/pdf` |
| 文本提取 | PyMuPDF（`import pymupdf`）`page.get_text()` |
| 页图渲染 | PyMuPDF `page.get_pixmap(dpi=150)` → PNG |
| 实测正文量 | 期刊 18218 字 / 12 页；硕士 98465 字 / 115 页；博士 161279 字 / 157 页（**硕博 PDF 同样有文本层**） |

---

## 1. 端到端链路（实测）

```
A3 search()  → SearchItem.detail_url
                 = https://kns.cnki.net/kcms2/article/abstract?v=<token>
   │
   ├─① GET 详情页（带机构会话）
   │      → 拿到 order 链接族（bar.cnki.net/bar/download/order?id=<token>）
   │
   ├─② 按 label 选「PDF下载」的 token
   │
   ├─③ GET https://bar.cnki.net/bar/download/order?id=<token>
   │      └─ 302 → https://docdown.cnki.net/docdown/fulltext/download?q=<token>
   │             → **200 application/pdf**（真实字节流）
   │
   └─④ 本地 PyMuPDF：
          read_html   → 逐页 get_text() 拼接 → 正文（word_count / pages）
          read_original → page.get_pixmap(dpi) → PNG（魔数 89504e47）
```

---

## 2. `bar.cnki.net` 的四类 token（同一 `id` 前缀、`&` 后段不同，服务端按后缀区分动作）

在期刊详情页实测的四个入口（A4 逐个跟进）：

| 链接（label） | 跟进结果 | 可用性 |
|---|---|---|
| HTML 阅读 | `302 → kns.cnki.net/reader/read?invoice=...` → **200 text/html，2757 B 的 SPA 外壳** | ✗ 正文不可直取 |
| 原版阅读 | 同上（SPA 外壳） | ✗ |
| **PDF 下载** | `302 → docdown.cnki.net/docdown/fulltext/download?q=...` → **200 `application/pdf`，2 248 824 B** | ✅ **本模块使用** |
| CAJ 下载 | 同上落点 → **200 `application/caj`，2 201 289 B**（魔数 `KDH `） | ✗ 无解析器 |

**硕博论文同样有 PDF 入口**（实测）：

| 类型 | PDF 体积 | 页数 | 文本层 |
|---|---|---|---|
| 博士（西北工业大学 2022） | 12 637 599 B | 157 | ✅ 161 279 字 |
| 硕士（合肥工业大学 2025） | 5 388 211 B | 115 | ✅ 98 465 字 |

> ⚠️ 修正一条旧认知：`知网取文过程.md` 记录「硕博 = 原版图像流取不到文本」——那是指**在线阅读器的渲染方式**；
> 而**机构授权下载的 PDF 本身带文本层**，故本模块对硕博同样能给出正文文本。

---

## 3. 未打通的路径（如实记录）

| 候选 | 探测结果 | 结论 |
|---|---|---|
| `kns.cnki.net/nzkpic/read/article/readonline?filename=...` | **404**（nginx 404 页） | 该前缀不可用 |
| `kns.cnki.net/nzkhtml/zkread/article/readonline?filename=...` | 200，但返回 **2757 B 的 SPA 外壳** | 是页面路由，不是数据接口 |
| `kns.cnki.net/reader/xml?filename=...&scope=readonline` | 200，**2733 B**，不含正文 | SPA 外壳 |
| `docdown.cnki.net/restapi/kreader-api/v1/pdf/img?filename=...` | **404**（缺 `idenid` 等派生参数） | 参数不可复现 |
| `kns.cnki.net/restapi/literature-api/v1/articles/catalog?vv=...&clientId=...` | 401 / `{"code":400,"message":"请求信息有误"}` | 请求体结构未知 |
| 匿名会话访问 `kns.cnki.net` | `302 → verify/home?captchaType=blockPuzzle`（**滑块**） | **仅拦匿名会话**，机构会话不受影响 |

### 关于 `vv` 参数（已查清，避免后人重复劳动）
阅读器 `app.js` 中 `getCatalogInfo` 用 `vv=Object(o["a"])(a, t.id)`，其中 `o = module("ed08")`。
追进 `module ed08` 可见其导出 `a → h`，而 **`h = function(){...}` 是一个「按 UA/屏宽返回 -1/0/1」的视图适配函数**（无参），
即 **`vv` 并非密码学签名**。接口返回 400 的根因在**请求体结构**，不在签名。
本模块未继续深挖（预算与稳定性考虑），改走已实测可行的 PDF 通道。

---

## 4. 机构订阅边界的表现（如实）

| 情形 | 详情页表现 | 本模块返回 |
|---|---|---|
| 已订购、有全文 | 有「PDF 下载」入口 | `KIND_HTML`（有文本层）或 `KIND_ORIGINAL`（无文本层/扫描） |
| 仅在线阅读 | 只有「HTML 阅读 / 原版阅读」 | `KIND_ORIGINAL` + message 说明「仅在线阅读，正文接口未复现」 |
| 仅试读 | 有入口但内容受限 | `KIND_TRIAL` |
| **未订购库** | **无任何 order 入口** | `KIND_UNSUBSCRIBED`（**绝不伪装可取**） |

> 已知未订购范例：知网「党建期刊文献总库」（见 `知网取文过程.md` §4）。

---

## 5. ⚠️ 成本与合规（必须遵守）

1. **PDF 下载消耗机构下载额度**。只有「HTML 在线阅读」不消耗额度，而该通道的正文接口未复现 ——
   因此**每一次 `read_html` 首次调用都会占用一次下载额度**。
   → 本模块对此的缓解是**磁盘强缓存**：同一文献第二次起**零网络请求**（实测二次耗时 0.00s）。
   → 上层（C2 预取 / C4 缓存层）应**复用**缓存，**绝不批量下载**。
2. **低频**：所有请求经 `CnkiHttpClient`（代理规避 + ≥2s 节流 + 串行）。
3. **不二次分发**：`outputs/` 下落的 PDF / 正文 / 页图**仅限本机个人学习使用**。
4. 仅**本人机构账号**、个人学习用途。

---

## 6. 代码契约（`src/cnki_api/reader.py`）

```python
KIND_HTML = "html"; KIND_ORIGINAL = "original"
KIND_TRIAL = "trial"; KIND_UNSUBSCRIBED = "unsubscribed"; KIND_UNKNOWN = "unknown"

class ReadError(Exception): ...

@dataclass
class ArticleContent:
    title: str; kind: str; text: str; html: str
    word_count: int; pages: int; message: str
    detail_url: str = ""; meta: dict = field(default_factory=dict)
    @property
    def ok(self) -> bool          # kind == KIND_HTML and word_count > 0

@dataclass
class PageImage:
    page: int; data: bytes; mime: str
    @property
    def is_valid_image(self) -> bool    # PNG/JPEG 魔数 + 长度 > 512

def read_html(detail_url, client=None, use_cache=True) -> ArticleContent
    # ★ 绝不向 UI 抛未捕获异常：失败 → 对应 KIND + 明确 message，text=""
def read_original(detail_url, page=1, client=None, use_cache=True, dpi=150) -> PageImage
    # 失败抛 ReadError（附可读原因）
def get_page_count(detail_url, client=None) -> int          # 取不到返回 0
def detect_availability(detail_url, client=None) -> str     # 不发 PDF 下载请求（省额度）
def get_summary(detail_url, client=None) -> dict            # 标题/摘要/关键词/基金（补 A3 缺口）
def prefetch_pdf(detail_url, client=None) -> Path | None    # 供 C2 预取，幂等
def resolve_order_links(detail_url, client=None) -> dict    # 排障用
def ensure_pdf(detail_url, client=None) -> Path             # 命中缓存零请求；无通道抛 ReadError
def close() -> None

# 模块级观测点（供上层/verify 断言）
last_read_from_cache: bool     # 最近一次读取是否命中本地暂存
last_meta: dict
```

### 缓存位置
`outputs/cache/reader/`
- `<sha1(detail_url)[:20]>.pdf` —— 原版 PDF（**额度已消耗，务必复用**）
- `<sha1>.txt` / `<sha1>.meta.json` —— 正文文本与元信息
- `<sha1>_p0001_150.png` —— 原版页图（按 (页码, dpi) 缓存）
- `<sha1>.json` —— 下载时刻的 order 链接族快照（排障/结构变化对比）

---

## 7. 已知边界与风险

1. **依赖 `bar.cnki.net` 的 order 链接形态**：若知网改版换掉该入口，需在 `_parse_order_links` / `_pick_pdf_link` 加新解析分支（解析层已隔离，UI 不受影响）。
2. **CAJ-only 文献**：少数文献仅给 CAJ，本模块**如实报错**（无 CAJ 解析器），不伪装。
3. **扫描版 PDF**（无文本层，`word_count < 100`）：返回 `KIND_ORIGINAL`，只提供图像阅读。
4. **下载额度**：见 §5.1，是最需要上层克制的点。
5. **大体量**：博士论文 PDF 可达 12 MB+，`get_pixmap` 首页约 1.7 MB PNG；C3 已按「离屏释放」设计（见 C3 任务）。
6. `detect_availability` 对「有 PDF 通道」的文献**乐观返回 `KIND_HTML`**（文本层需下载后才知道），已在注释与本文档标明。

---

## 8. 复现命令

```bash
.venv/Scripts/python.exe tools/verify_read.py
# 期望：6 PASS, 0 FAIL, 0 DEGRADED；期刊正文 ≥1000 字；硕博首页图为合法 PNG
```
