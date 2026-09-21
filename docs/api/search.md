# 知网检索数据接口（kns8s brief/grid）API 文档

> 阶段：Phase A — A3 任务（已认证会话直调检索接口，落地结构化题录）
> 结论：**POST `https://kns.cnki.net/kns8s/brief/grid` 已实测走通**，返回 HTML 片段，可解析出结构化题录。
> 验收：`tools/verify_search.py` 全部 6 项 PASS，退出码 0。
> 依赖上游：A1 `CnkiHttpClient` / `log`；A2 `get_authenticated_client`（已认证会话，约 30 天有效）。

---

## 0. 关键事实速查

| 项 | 值 |
|---|---|
| 检索端点 | `POST https://kns.cnki.net/kns8s/brief/grid` |
| 认证要求 | 必须带已认证会话 Cookie（匿名会被 302 到 verify/home 滑块） |
| 请求形态 | `application/x-www-form-urlencoded`；关键字段 `QueryJson`（**单层** JSON 串） |
| 字段码 | `Field` 用 `SU/TI/KY/AB/AU/FI/RP/AF/LY/FU/DI/CL/RF/FT` |
| 匹配算子 | **数值** `Operator`：`2`=模糊(`%=`)、`1`=精确(`=`)（kns8s 新版不接受字符串 %=/=） |
| 默认库 | `Resource=CJFQ,CJXQ,CDFD,CMFD,CPFD,IPFD,CCND,CCJD`；`Classid=WD0FTY92` |
| 排序 | `SortField`：`FFD`=相关度 `PT`=发表时间 `CF`=被引 `DFR`=下载；`SortType=desc` |
| 响应形态 | HTML 片段（含 `<div id="briefBox">` + `gridTable`）；总数在 `<input id="totalCnt" value="N">` |
| 单页上限 | `pageSize` 实测可用 10/20/50；本模块默认 20 |
| 详细链接 | 稳定 URL 由行内 `data-filename` + `data-dbname` 组装：`kcms2/article/abstract?filename=..&dbcode=..` |

> 实测坑（Round 1~3 侦察纠正）：
> 1. **QueryJson 双包 bug**：旧写法把 `{Platform,Resource,QueryJson:{QNode}}` 再 json.dumps，导致服务端读不到 `QNode` → 报「没有指定检索分类！」。正确写法：`QueryJson = json.dumps({Platform,Resource,Classid,QNode{...}})`（单层）。
> 2. **字段/算子格式**：kns8s 新版要求 `Field`(码) + `Operator`(**数值**)，旧版 `Name`+`Operate`(字符串) 会报「非法逻辑操作符。」；`Resource="CROSSDB"` 无效（静默空结果），必须用上方逗号串。

---

## 1. 请求参数（表单字段）

| 字段 | 值 | 说明 |
|---|---|---|
| `boolSearch` | `false` | 是否复合检索 |
| `QueryJson` | JSON 串 | 检索式（结构见 §2） |
| `pageNum` | `1` | 页码 |
| `pageSize` | `20` | 每页条数 |
| `SortField` | `FFD`/`PT`/`CF`/`DFR` | 排序字段（见 §3） |
| `SortType` | `desc` | 升降序 |
| `dstyle` | `1` | 展示样式 |
| `boolSortSearch` | `false` | 排序检索开关 |
| `sentenceSearch` | `false` | 句子检索 |
| `productStr`/`aside`/`searchFrom`/`manageId`/`subject`/`turnpage` | `""`/`1` | 兼容字段（默认即可） |
| `sKuaKuID` | `""` | 跨库 ID |

请求头需带 `Referer: https://kns.cnki.net/kns8s/defaultresult/index` 与 `X-Requested-With: XMLHttpRequest`（模拟 AJAX，避免被当匿名/异常）。

---

## 2. QueryJson 结构（实测生效）

```json
{
  "Platform": "",
  "Resource": "CJFQ,CJXQ,CDFD,CMFD,CPFD,IPFD,CCND,CCJD",
  "Classid": "WD0FTY92",
  "QNode": {
    "QGroup": [
      {
        "Key": "Subject", "Title": "主题", "Logic": 0,
        "Items": [
          {
            "Key": "", "Title": "主题", "Logic": 0,
            "Field": "SU",          // 字段码
            "Operator": 2,          // 2=模糊 1=精确（数值）
            "Value": "空地协同 巡逻",
            "Value2": "", "options": {}, "ExtendType": 0
          }
        ],
        "ChildItems": []
      }
    ]
  }
}
```

多条件：在 `Items` 中追加元素；**首条 `Logic=0`，其后 `Logic` 取 `AND`/`OR`/`NOT`**（即与上一条件的逻辑关系）。

---

## 3. 排序映射（实测来自结果页 UI 的 `id`）

| 用户传入 `sort` | `SortField` | 含义 |
|---|---|---|
| `relevance` | `FFD` | 相关度 |
| `date` | `PT` | 发表时间 |
| `cited` | `CF` | 被引 |
| `download` | `DFR` | 下载 |

（页面 UI 原文：`<b id="FFD">相关度`、`<b id="PT">发表时间`、`<b id="CF">被引`、`<b id="DFR">下载`。）

---

## 4. 返回结构（HTML 片段，当前 v1 解析器适配）

每个数据行 `<tr>` 含：

| 列 `class` | 提取 | 类型 |
|---|---|---|
| `name` | `<a class="fz14" href>` 文本 = 标题；同行的 `data-filename`+`data-dbname` → 稳定 `detail_url` | str |
| `author` | 优先 `<a class="KnowledgeNetLink">` 文本（中文文献）；无锚点时回退整格文本（外文/英文文献，如 `Yang Jianhua;Ding Zhaowei`） | str（分号分隔） |
| `source` | 来源（期刊/会议名） | str |
| `date` | `2026-06-29 16:28` → `year` 取前 4 位 | str |
| `data` | 文献类型（`期刊`/`会议`/`外文期刊`…） | str |
| `quote` | 被引数（空=0） | int |
| `download` | 下载数（空=0） | int |

总数：`<input id="totalCnt" type="hidden" value="N"/>` → `total`。

> 解析器做成可替换：`_parse_v1(html)` 为当前版本；`_parse()` 入口会先判是否 JSON（`{...}` 开头），若是走 `_parse_json`（兜底分支，当前端点未返回 JSON，预留结构变化切换）。文档写明：当前适配 **kns8s grid HTML v1**。

---

## 5. 代码契约（导出符号，签名锁定，下游 B3/C2/C3/C4 依赖）

`src/cnki_api/search.py`：

```python
FIELD_CODES: dict[str, str]          # 中文名/别名 -> 知网码（主题SU/篇名TI/关键词KY/摘要AB/作者AU/第一作者FI/通讯作者RP/作者单位AF/文献来源LY/基金FU/DOI DI/分类号CL/参考文献RF/全文FT）
LOGIC_OPERATORS = {"AND": "AND", "OR": "OR", "NOT": "NOT"}
MATCH_MODES = {"fuzzy": "%=", "exact": "="}   # 文档说明：实际走数值 Operator(2/1)，此处保留规范算符令牌

@dataclass SearchItem:  title, authors, source, year, cited(int), downloads(int), db_type, detail_url, keyword="", abstract="", doi="", fund="", raw={}
@dataclass SearchResult: total, page, page_size, items:list[SearchItem], query:dict
class SearchError(Exception): ...

def search(keyword="", field="SU", match="fuzzy", logic="AND", conditions=None,
           page=1, page_size=20, sort="relevance", client=None, use_cache=False) -> SearchResult
def search_simple(text, **kw) -> SearchResult
def parse_prefix(text) -> tuple[str, str, str]   # "作者:张三" -> ("AU","张三","作者")
```

- 所有字段值统一 `str`/`int`；缺失填空串 `""` 或 `0`，**绝不返回 None**（B3 断言字段齐全无空值）。
- `conditions`：`[{field, value, match}]` 列表，与 `keyword` 可合并。
- `client=None` 时内部调用 `get_authenticated_client()`（复用 A2 加密 store 会话）。
- `use_cache`：模块级内存缓存（按 sort+page+page_size+QueryJson 命中）。

---

## 6. 验收数值（tools/verify_search.py，退出码 0）

1. 「空地协同 巡逻」SU：`total=22`（≥10），首轮 20 条 `title/authors/source/year/cited/downloads/detail_url` 全非空且 cited/downloads 为 int。
2. 作者「张三」AU：`total=97`，首条含「空地协同巡逻」且 authors 含「张三」→ 相关 PASS。
3. 精确 vs 模糊：对「空地协同 巡逻」两者 `total`/顺序恰巧相同；已给出可判定证据——请求 `Operator` 确不同（fuzzy=2, exact=1）。补充实测：单字「巡逻」SU 两者亦同（22599），印证该端点对单/双 token 同形短语模糊≈精确；区别在参数层面成立。
4. 同参数 3 次重复：标题序列完全一致（各 20 条）。
5. `parse_prefix("作者:张三")` = `("AU","张三","作者")` PASS。

---

## 7. 未覆盖字段 / 已知边界（诚实备注）

1. **`keyword`/`abstract`/`doi`/`fund` 在 brief 网格中不出**：`SearchItem` 这些字段当前恒为 `""`。brief/grid 仅给题录级字段（题名/作者/来源/年/被引/下载/类型/链接）。摘要/DOI/基金需二次进入详情页（`kcms2/article/abstract` 或 `detail`）抓取，留待 B/C 阶段；契约已预留字段，不影响下游。
2. **`detail_url` 稳定性**：优先用 `data-filename`+`data-dbname` 组装的无状态 URL；个别行若无这两个属性则回退到 `td.name a.fz14` 的会话态 `v=` 令牌链接（会过期，但当前数据均含 filename/dbname）。
3. **精确/模糊在该端点经常等价**：因 kns8s 对 SU 短语的模糊(%=)与精确(=)实现上趋同，验收以「请求参数不同」为可判定证据；若下游需要强区分，建议对 `field` 用 `TI`(篇名) 或在值内显式加引号。
4. **风控/频率**：所有请求经 `CnkiHttpClient`（≥2s 节流、代理规避）；本任务全程 ≤22 次请求。若遇风控，按降级策略降频 ≥10s 并重试。
5. **结构变化兜底**：`_parse` 已含 JSON 分支；若 CNKI 改回 JSON 网格，仅需实现 `_parse_json` 的列映射即可切换，无需改调用方。
6. **未覆盖**：分页翻页（仅测 page=1）、多库筛选（仅总库）、句子检索 `sentenceSearch`、跨语言 `SimpTrad` 均未实测。
