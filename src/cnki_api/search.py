"""知网检索数据接口（kns8s brief/grid）直调模块。

Phase A — A3 任务产出。下游 B3/C2/C3/C4 依赖本模块契约，签名不得改。

实测接口（Round 1~3 侦察确认，详见 docs/api/search.md）：
- 端点：POST https://kns.cnki.net/kns8s/brief/grid
- 形态：表单（application/x-www-form-urlencoded），QueryJson 为**单层** JSON 串，
  内含 Platform / Resource / Classid / QNode{QGroup[].Items[].{Field,Operator,Value,...}}。
  （旧版把 QueryJson 再包一层导致「没有指定检索分类」，已修正。）
- 字段用 Field（SU/TI/KY/...），匹配用数值 Operator：2=模糊(%=) 1=精确(=)。
  kns8s 新版不接受字符串 %=/=，故内部映射，文档如实说明。
- 排序 SortField：FFD=相关度 PT=发表时间 CF=被引 DFR=下载；SortType 统一 desc。
- 返回为 HTML 片段（含 gridTable），本模块 HTML 解析器为主；JSON 分支为兜底（结构变化可切换）。

纪律：所有请求经 CnkiHttpClient（代理规避 + ≥2s 节流）；绝不打印 Cookie/凭证。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from cnki_hover.http_client import CnkiHttpClient
from cnki_hover.log import get_logger, log_event

LOG = get_logger("cnki_search")

# ---- 公共契约常量（下游依赖，不得改名） ----

# 中文名/别名 -> 知网字段码。键同时支持中文与码本身。
FIELD_CODES: dict[str, str] = {
    "SU": "SU", "主题": "SU", "主题词": "SU",
    "TI": "TI", "篇名": "TI", "题名": "TI", "标题": "TI",
    "KY": "KY", "关键词": "KY", "关键字": "KY",
    "AB": "AB", "摘要": "AB",
    "AU": "AU", "作者": "AU",
    "FI": "FI", "第一作者": "FI",
    "RP": "RP", "通讯作者": "RP",
    "AF": "AF", "作者单位": "AF", "单位": "AF", "机构": "AF",
    "LY": "LY", "文献来源": "LY", "来源": "LY", "来源期刊": "LY",
    "FU": "FU", "基金": "FU",
    "DI": "DI", "DOI": "DI", "doi": "DI",
    "CL": "CL", "分类号": "CL",
    "RF": "RF", "参考文献": "RF",
    "FT": "FT", "全文": "FT",
}

LOGIC_OPERATORS = {"AND": "AND", "OR": "OR", "NOT": "NOT"}

# match 模式 -> kns8s 数值 Operator（实测：2=模糊 1=精确；%==/=为旧版字符串，已不可用于本端点）
MATCH_MODES = {"fuzzy": "%=", "exact": "="}
_OPERATOR_CODE = {"fuzzy": 2, "exact": 1}

# sort 名称 -> (SortField, SortType)
_SORT_FIELDS = {
    "relevance": ("FFD", "desc"),
    "date": ("PT", "desc"),
    "cited": ("CF", "desc"),
    "download": ("DFR", "desc"),
}

# kns8s 跨库资源串（总库）
_RESOURCE = "CJFQ,CJXQ,CDFD,CMFD,CPFD,IPFD,CCND,CCJD"
_CLASSID = "WD0FTY92"
_GRID_URL = "https://kns.cnki.net/kns8s/brief/grid"

# kns8s 对该字段做枚举校验：非白名单值会被服务端拒绝，返回
# `参数校验;字段【pageSize】校验失败`（实测 pageSize=2 / 5 均被拒，10/20 正常）。
# 这里做入参兜底，避免调用方传任意值后只拿到一句看不懂的服务端报错。
_ALLOWED_PAGE_SIZES = (10, 20, 30, 40, 50)


def _normalize_page_size(n) -> int:
    """把任意 page_size 归一到服务端白名单值（就近向下取，最小 10）。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return 20
    if v in _ALLOWED_PAGE_SIZES:
        return v
    below = [x for x in _ALLOWED_PAGE_SIZES if x <= v]
    return max(below) if below else _ALLOWED_PAGE_SIZES[0]

_DETAIL_TMPL = "https://kns.cnki.net/kcms2/article/abstract?filename={filename}&dbcode={dbcode}"


@dataclass
class SearchItem:
    title: str
    authors: str
    source: str
    year: str
    cited: int
    downloads: int
    db_type: str
    detail_url: str
    keyword: str = ""
    abstract: str = ""
    doi: str = ""
    fund: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def stable_id(self) -> str:
        """稳定文献标识（知网 filename）。

        `detail_url` 里的 `?v=<token>` **每次检索都会变**，不能当缓存键；
        filename 在库内唯一且稳定，是给 reader 做缓存/去重的正确键。
        """
        return str((self.raw or {}).get("filename", "") or "")


@dataclass
class SearchResult:
    total: int
    page: int
    page_size: int
    items: list[SearchItem]
    query: dict


class SearchError(Exception):
    """检索失败（接口错误 / 参数非法 / 结构变化无法解析）。"""


# 知网服务端**瞬时**异常特征（重试一次通常就能过；实测
# `WD0FTY92数据服务异常：getString  error:-1;null` 属于此类，非风控、非参数问题）
_TRANSIENT_MARKERS = ("数据服务异常", "error:-1", "请稍后", "服务繁忙", "系统繁忙",
                      "timeout", "超时", "502", "503", "504")


def _is_transient_error(msg: str) -> bool:
    return any(m in (msg or "") for m in _TRANSIENT_MARKERS)


def _resolve_field(field: str) -> str:
    code = FIELD_CODES.get(field)
    if not code:
        # 未知字段：原样返回（上层可自定义），避免硬失败
        LOG.warning("未知检索字段 %r，原样使用", field)
        return field
    return code


def _resolve_match(match: str) -> int:
    code = _OPERATOR_CODE.get(match)
    if code is None:
        LOG.warning("未知匹配模式 %r，回退 fuzzy", match)
        return _OPERATOR_CODE["fuzzy"]
    return code


def _build_query_json(conditions: list[dict], logic: str = "AND") -> str:
    """conditions: [{field, value, match}]；首条 Logic=0，其后 Logic=全局 logic。"""
    items = []
    for idx, c in enumerate(conditions):
        items.append({
            "Key": "",
            "Title": "主题",
            "Logic": 0 if idx == 0 else LOGIC_OPERATORS.get(logic, "AND"),
            "Field": _resolve_field(c.get("field", "SU")),
            "Operator": _resolve_match(c.get("match", "fuzzy")),
            "Value": c.get("value", ""),
            "Value2": "",
            "options": {},
            "ExtendType": 0,
        })
    state = {
        "Platform": "",
        "Resource": _RESOURCE,
        "Classid": _CLASSID,
        "QNode": {
            "QGroup": [
                {
                    "Key": "Subject",
                    "Title": "主题",
                    "Logic": 0,
                    "Items": items,
                    "ChildItems": [],
                }
            ]
        },
    }
    return json.dumps(state, ensure_ascii=False)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.replace("&nbsp;", " ").strip()


def _to_int(text: str) -> int:
    s = re.sub(r"[^\d]", "", text or "")
    return int(s) if s else 0


# ---- 解析器（可替换：当前适配 kns8s grid HTML v1） ----

def _parse_v1(html: str) -> tuple[int, list[SearchItem]]:
    """解析 kns8s brief/grid 返回的 HTML 片段。返回 (total, items)。"""
    # 总记录数
    total = 0
    m = re.search(r'<input id="totalCnt"[^>]*value="(\d+)"', html)
    if m:
        total = int(m.group(1))

    items: list[SearchItem] = []
    # 逐行：仅含 td.name 的 <tr> 为数据行
    for row in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        seg = row.group(1)
        if 'class="name"' not in seg and "class='name'" not in seg:
            continue

        # 标题 + 详情链接
        tm = re.search(r'<a class="fz14"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', seg, re.S)
        if not tm:
            continue
        detail_href = tm.group(1)
        title = _strip_tags(tm.group(2))

        # 详情链接：**必须优先用页面原始 token 化 href（?v=<token>）**。
        # A4 实测：自造 ?filename=&dbcode= 模板会 404，故降级为仅兜底。
        fn = re.search(r'data-filename="([^"]+)"', seg)
        db = re.search(r'data-dbname="([^"]+)"', seg)
        detail_url = detail_href or (
            _DETAIL_TMPL.format(filename=fn.group(1), dbcode=db.group(1)) if (fn and db) else ""
        )
        if detail_url.startswith("//"):
            detail_url = "https:" + detail_url
        elif detail_url.startswith("/"):
            detail_url = "https://kns.cnki.net" + detail_url

        # 作者：优先 KnowledgeNetLink 锚点；无锚点时回退整格文本（英文/外文文献常见）
        am = re.search(r"<td[^>]*class=['\"]author['\"]>(.*?)</td>", seg, re.S)
        authors = ""
        if am:
            anchors = re.findall(
                r'<a class="KnowledgeNetLink"[^>]*>([^<]*)</a>', am.group(1)
            )
            if anchors:
                authors = ";".join(a.strip() for a in (_strip_tags(x) for x in anchors) if a.strip())
            else:
                authors = _strip_tags(am.group(1))

        # 来源
        sm = re.search(r"<td[^>]*class=['\"]source['\"]>(.*?)</td>", seg, re.S)
        source = _strip_tags(sm.group(1)) if sm else ""

        # 日期 / 年
        dm = re.search(r"<td[^>]*class=['\"]date['\"]>(.*?)</td>", seg, re.S)
        date_str = _strip_tags(dm.group(1)) if dm else ""
        year = date_str[:4] if date_str else ""

        # 文献类型
        bm = re.search(r"<td[^>]*class=['\"]data['\"]>(.*?)</td>", seg, re.S)
        db_type = _strip_tags(bm.group(1)) if bm else ""

        # 被引：只取 quoteCnt 锚点的**文本**，避免把 href 里的数字一并算入（A4 修复的天文数字缺陷）
        cited = 0
        qm = re.search(r'class="quoteCnt"[^>]*>([^<]*)<', seg)
        if qm:
            cited = _to_int(qm.group(1))
        else:
            qm2 = re.search(r"<td[^>]*class=['\"]quote['\"]>(.*?)</td>", seg, re.S)
            if qm2:
                cited = _to_int(re.sub(r"<[^>]+>", " ", qm2.group(1)))

        # 下载：同理，取 downloadCnt 锚点文本
        dl = 0
        dm2 = re.search(r'class="downloadCnt"[^>]*>([^<]*)<', seg)
        if dm2:
            dl = _to_int(dm2.group(1))

        items.append(SearchItem(
            title=title,
            authors=authors,
            source=source,
            year=year,
            cited=cited,
            downloads=dl,
            db_type=db_type,
            detail_url=detail_url,
            raw={"date": date_str, "filename": fn.group(1) if fn else "",
                 "dbcode": db.group(1) if db else "",
                 "cid": (re.search(r'name="CookieName"\s*value="([^"]+)"', seg).group(1)
                         if re.search(r'name="CookieName"\s*value="([^"]+)"', seg) else "")},
        ))
    return total, items


def _parse(body: str) -> tuple[int, list[SearchItem]]:
    """解析入口：优先 HTML v1；若将来返回 JSON 可在此切换版本。"""
    s = body.strip()
    if s.startswith("{"):
        try:
            j = json.loads(s)
        except Exception:
            return _parse_v1(body)
        # JSON 分支（兜底，当前端点未返回；结构变化时可启用）
        if isinstance(j, dict) and ("tableData" in j or "Data" in j):
            return _parse_json(j)
        return _parse_v1(body)
    return _parse_v1(body)


def _parse_json(j: dict) -> tuple[int, list[SearchItem]]:
    """JSON 响应兜底解析（当前未启用，预留给结构变化）。"""
    total = int(j.get("TotalNum") or j.get("total") or 0)
    rows = j.get("tableData") or j.get("Data") or []
    items: list[SearchItem] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        items.append(SearchItem(
            title=str(r.get("Title", "")),
            authors=str(r.get("Author", "") or r.get("Authors", "")),
            source=str(r.get("Source", "") or r.get("LY", "")),
            year=str(r.get("Year", "") or r.get("Date", "") or "")[:4],
            cited=int(r.get("Cited", 0) or 0),
            downloads=int(r.get("Download", 0) or 0),
            db_type=str(r.get("DBType", "") or r.get("Type", "")),
            detail_url=str(r.get("Url", "") or r.get("detail_url", "")),
            raw=r,
        ))
    return total, items


# ---- 公共 API ----

_CACHE: dict = {}


def search(
    keyword: str = "",
    field: str = "SU",
    match: str = "fuzzy",
    logic: str = "AND",
    conditions: Optional[list[dict]] = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "relevance",
    client: Optional[CnkiHttpClient] = None,
    use_cache: bool = False,
) -> SearchResult:
    """直调 kns8s brief/grid，返回结构化题录。

    conditions: 多条件列表 [{field, value, match}]；与 keyword 二选一或合并。
    sort: relevance/date/cited/download（映射见 _SORT_FIELDS）。
    """
    if conditions is None:
        conditions = []
    if keyword:
        conditions = [{"field": field, "value": keyword, "match": match}] + list(conditions)
    if not conditions:
        raise SearchError("keyword 与 conditions 不能同时为空")

    sort_field, sort_type = _SORT_FIELDS.get(sort, _SORT_FIELDS["relevance"])
    query_json = _build_query_json(conditions, logic)
    page_size = _normalize_page_size(page_size)

    data = {
        "boolSearch": "false",
        "QueryJson": query_json,
        "pageNum": str(page),
        "pageSize": str(page_size),
        "SortField": sort_field,
        "SortType": sort_type,
        "dstyle": "1",
        "boolSortSearch": "false",
        "sentenceSearch": "false",
        "productStr": "",
        "aside": "",
        "searchFrom": "",
        "manageId": "",
        "subject": "",
        "turnpage": "1",
        "sKuaKuID": "",
    }

    query_meta = {
        "keyword": keyword, "field": field, "match": match, "logic": logic,
        "conditions": conditions, "page": page, "page_size": page_size,
        "sort": sort,
    }

    cache_key = (sort_field, sort_type, page, page_size, query_json)
    if use_cache and cache_key in _CACHE:
        res = _CACHE[cache_key]
        res.query = query_meta
        return res

    own = False
    if client is None:
        from cnki_api.auth import get_authenticated_client
        client = get_authenticated_client()
        own = True

    headers = {
        "Referer": "https://kns.cnki.net/kns8s/defaultresult/index",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _fetch():
        try:
            t0 = time.monotonic()
            r = client.post(_GRID_URL, data=data, headers=headers)
            cost_ms = round((time.monotonic() - t0) * 1000, 1)
        except Exception as e:  # noqa: BLE001
            raise SearchError(f"检索请求异常：{type(e).__name__}: {e}") from e
        body_text = r.content.decode("utf-8", errors="replace")
        # 错误探测：知网偶尔返回「数据服务异常：getString error:-1」这类瞬时错误
        nm = re.search(r'class="no-content"[^>]*value="([^"]*)"', body_text)
        if nm and nm.group(1) and "暂无" not in nm.group(1):
            raise SearchError(f"检索接口错误：{nm.group(1)}")
        return body_text, cost_ms

    try:
        body, cost = _fetch()
    except SearchError as e:
        # 降级策略（任务卡）：接口**偶发**异常自动重试 1 次，仍失败才报错态。
        if _is_transient_error(str(e)):
            log_event(LOG, "search_transient_retry", detail=str(e)[:90])
            time.sleep(2.0)          # 保持低频，不立刻重打
            body, cost = _fetch()
        else:
            raise

    total, items = _parse(body)
    log_event(LOG, "search_done", sort=sort, page=page, total=total,
              items=len(items), cost_ms=cost)

    res = SearchResult(
        total=total, page=page, page_size=page_size, items=items, query=query_meta
    )
    if use_cache:
        _CACHE[cache_key] = res
    return res


def search_simple(text: str, **kw) -> SearchResult:
    """无前缀纯文本检索（默认主题 SU 模糊）。"""
    return search(keyword=text, field="SU", match="fuzzy", **kw)


_PREFIX_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z]{1,6})\s*[:：.。、,，]\s*(.+)$")


def has_prefix(text: str) -> bool:
    """这段输入是否构成**有效**的字段前缀（分隔符 + **已知字段名**）。"""
    m = _PREFIX_RE.match(text or "")
    return bool(m and FIELD_CODES.get(m.group(1)) and m.group(2).strip())


def parse_prefix(text: str) -> tuple[str, str, str]:
    """解析「字段:值」前缀。

    返回 (field_code, value, field_label)。无前缀（或**字段名不认识**）-> ("SU", text, "主题")。

    两处关键修正（用户实测反馈）：
    1. 分隔符原来只认 `:` / `：`。用户打 `作者.无人机` 时不被识别，
       整串被当成主题词去检索 → 结果是一堆莫名其妙的英文文献。现已支持 `. 。 、 , ，`。
    2. 必须**校验字段名是已知字段**才当前缀。否则像 `3.5 实验方法` 这种正常检索词
       会被误判成「字段=3.5」并搜出错误结果。
    """
    t = (text or "").strip()
    m = _PREFIX_RE.match(text or "")
    if m:
        label, value = m.group(1), m.group(2).strip()
        code = FIELD_CODES.get(label)
        if code and value:
            return (code, value, label)
    return ("SU", t, "主题")
