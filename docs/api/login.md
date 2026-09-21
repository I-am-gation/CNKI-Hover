# 知网机构登录（校外访问 / CARSI / Shibboleth）API 文档

> 阶段：Phase A — A2 任务（纯 HTTP 复现「校外访问（机构）登录」）
> 机构：示例大学（IdP `idp.example.edu.cn`）
> 结论：**纯 HTTP 链路已完整跑通，无浏览器 / 无 CDP / 无人工干预**；会话加密持久化可跨进程复用。
> 验收：`tools/verify_login.py` 连续 3 次独立进程运行均 **6/6 PASS, EXIT=0**（间隔 ≥2s）。

---

## 0. 关键事实速查

| 项 | 值 |
|---|---|
| 登录方式 | CARSI / Shibboleth SAML2 WebSSO（SP 发起，Redirect + POST 绑定） |
| SP（知网侧） | `fsso.cnki.net` |
| IdP（机构侧） | `idp.example.edu.cn`（entityID `https://idp.example.edu.cn/idp/shibboleth`） |
| 凭证表单 | `j_username` / `j_password`（**明文**，无 RSA、无验证码） |
| 会话形态 | CNKI 域 Cookie（`Ecp_ClientId` / `LID` / `Ecp_LoginStuts` / `c_m_LinID` / `SID`） |
| 会话绝对过期 | `c_m_expire` 实测 `2026-10-21 11:14:45`（登录后约 30 天） |
| 重登判定 | 本地缺认证 Cookie，或对内容入口探针返回 302→`verify/home`（滑块） |
| 滑块（Round 3） | **仅拦截匿名会话**；已认证会话天然绕过，无需处理 |

---

## 1. 端点表（Endpoint）

| # | 用途 | 方法 | URL | 关键参数 / 说明 |
|---|---|---|---|---|
| 1 | SP 发起登录 | GET | `https://fsso.cnki.net/Shibboleth.sso/Login` | `entityID=https://idp.example.edu.cn/idp/shibboleth`；`target=<quote("https://fsso.cnki.net/secure/default.aspx")>` |
| 2 | IdP SSO 重定向 | 302 | `https://idp.example.edu.cn/idp/profile/SAML2/Redirect/SSO` | `SAMLRequest=...&RelayState=...`（标准 SAML Redirect 绑定） |
| 3 | IdP 登录表单 | GET 200 | `https://idp.example.edu.cn/idp/.../login` | 含 `j_username` / `j_password` / `_eventId_proceed` 等隐藏域 |
| 4 | 提交凭证 | POST | 表单 action（同 #3） | `j_username=...` / `j_password=...` / `_eventId_proceed=Login` / `donotcache=1` |
| 5 | 属性释放 consent | POST | consent 表单 action | `_eventId_proceed=Proceed`（**剔除** `_eventId_AttributeReleaseRejected`） |
| 6 | SAML POST 绑定 | POST | `https://fsso.cnki.net/Shibboleth.sso/SAML2/POST` | `SAMLResponse=...` / `RelayState=...`（真实 SP ACS，需拆包，见 §2） |
| 7 | SP 落地 | 302 | `https://fsso.cnki.net/secure/default.aspx` | 此处写入 CNKI 会话 Cookie |
| 8 | 跳转 www | 302 | `https://www.cnki.net/` | 完成，获得完整会话 |
| P | 会话有效性探针 | GET | `https://kns.cnki.net/kns8s/defaultresult/index` | `200` = 已认证；`302`→`verify/home`（滑块）= 失效 |

> 注：`login.cnki.net/login/?platform=kns`、`fsso.cnki.net/`、`ds.carsi.edu.cn/` 均探测返回 200，但**实际走通的是 `fsso.cnki.net/Shibboleth.sso/Login` 这条 CARSI 入口**（Round 1 侦察确认）。

---

## 2. 完整重定向链（Round 1 实测）

```
① GET  fsso.cnki.net/Shibboleth.sso/Login?entityID=<IdP>&target=<fsso/secure/default.aspx>
        └─ 302 → idp.example.edu.cn/idp/profile/SAML2/Redirect/SSO?SAMLRequest=...

② GET  idp .../Redirect/SSO?SAMLRequest=...
        └─ 200 → 返回 IdP 登录表单(j_username / j_password / _eventId_proceed / 隐藏域)

③ POST idp 登录表单 action
        data = { j_username, j_password, _eventId_proceed=Login, donotcache=1 }
        └─ 200 → 属性释放(consent) 表单（若机构开启了 Shibboleth 属性同意）

④ POST consent 表单 action
        data = { 隐藏域..., _eventId_proceed=Proceed }   # 剔除 _eventId_AttributeReleaseRejected
        └─ 200 → SAML POST 绑定表单（action 为 IdP 的 "Redirect 包装"）

⑤ 拆包：若 action 含 "SAML2/Redirect/https://" 则取其后半段，得到真实 SP ACS
        aurl = aurl.split("SAML2/Redirect/", 1)[1]   # = https://fsso.cnki.net/Shibboleth.sso/SAML2/POST
   POST fsso.cnki.net/Shibboleth.sso/SAML2/POST
        data = { SAMLResponse, RelayState }
        └─ 302 → fsso.cnki.net/secure/default.aspx

⑥ GET  fsso.cnki.net/secure/default.aspx
        └─ 302 → www.cnki.net/   （此跳写入 Ecp_ClientId/LID/Ecp_LoginStuts/c_m_LinID/SID）

⑦ 完成：client.session 已持有 CNKI 认证 Cookie，可访问 kns 内容域
```

**实现约束（已在 `auth.py` 落实）：**
- 全程 `allow_redirects=False`，手动跟随 302，单跳由 `CnkiHttpClient` 内置 ≥2s 节流，避免轰炸。
- `for _ in range(8)` 找到 IdP 表单；`for _ in range(16)` 跟随 consent + SAML POST 直到落地。
- consent 表单的 `action` 是被 IdP 包了一层的 `.../SAML2/Redirect/https://fsso.cnki.net/...`，**必须拆出内嵌真实 ACS 再 POST**，否则会 404。
- `target` 必须用 `quote(FSSO_TARGET, safe="")`（即 `fsso/secure/default.aspx` 编码），若误用 `https://kns.cnki.net/` 会触发 IdP 400 "Unable to Respond"。

---

## 3. 请求参数与加密

### 3.1 入口参数
- `entityID`：`https://idp.example.edu.cn/idp/shibboleth`（取自 `fsso.cnki.net/Shibboleth.sso/DiscoFeed`，已固化在 `INSTITUTION_ENTITY_ID`）。
- `target`：`quote("https://fsso.cnki.net/secure/default.aspx", safe="")`。**不要**用 `kns.cnki.net` 作 target（会 400）。

### 3.2 凭证字段（明文）
- `j_username` / `j_password`：示例大学 IdP 的 Shibboleth 原生登录表单为**明文提交，无 RSA、无图形验证码**。
- 附加：`_eventId_proceed=Login`、`donotcache=1`。

### 3.3 SAML 报文
- `SAMLRequest`（入站）/ `SAMLResponse`（出站）均为标准 SAML2 断言，由 IdP / SP 动态生成，**客户端无需构造或解密**，只需原样回传表单隐藏域。

### 3.4 加密相关（重要澄清）
- 知网 `nids.example.edu.cn` 的 **CAS 登录表单**曾出现 AES-128-CBC 密码「加密」+ 失败 3 次出验证码——**本方案完全绕开该路径**，改走 SAML 重定向实际落到的 Shibboleth 原生 `j_username`/`j_password` 明文表单，因此没有密码加密/解密环节，也没有 RSA 公钥协商。

### 3.5 凭证来源纪律
- 仅经 `cnki_hover.credential.load_credentials()` 读取 `secrets/account.txt`；**绝不打印明文、绝不进日志**（日志走 `cnki_hover.log` 脱敏）。
- 验收脚本对密码仅打码输出 `2*************s`。

---

## 4. 会话 Cookie 形态（Session Form）

登录成功后，`.cnki.net` 域关键 Cookie：

| Cookie | 作用 / 说明 |
|---|---|
| `Ecp_ClientId` | 客户端会话标识 |
| `Ecp_LoginStuts` | 登录态 JSON；`ShowName` 字段为 **URL 编码**的机构名（需 `urllib.parse.unquote` 还原，如 `示例大学`）；可能含 `UserName` |
| `LID` | 登录态标识 |
| `c_m_LinID` | 机构（CARSI）登录标识 |
| `SID` | 服务端会话标识 |
| `c_m_expire` | **绝对过期时间**，格式 `YYYY-MM-DD HH:MM:SS`（实测 `2026-10-21 11:14:45`） |

判定「已拿到 CNKI 会话」的最小集合（`_CNKI_AUTH_COOKIES`）：
`("Ecp_ClientId", "Ecp_LoginStuts", "LID", "c_m_LinID", "SID")`。

> 机构身份的权威证据：`Ecp_LoginStuts.ShowName` 解码后等于机构名（验收项 `institution`）。

---

## 5. 过期与重新登录判定（Re-login Judgment）

### 5.1 绝对过期
- `c_m_expire` 为登录时刻起约 30 天的绝对时间戳；到期即失效，需重登。

### 5.2 在线探针（`is_session_valid`）
- 本地需先持有 `_CNKI_AUTH_COOKIES` 之一；
- 再对 `CNKI_CONTENT_PROBE`（`kns8s/defaultresult/index`）发起 `GET`：
  - 返回 `200` → 已认证；
  - 返回 `302` 且 `Location` 含 `verify/home`（滑块页）→ 会话失效（匿名才会被拦）。

### 5.3 复用 / 重登流程（`get_authenticated_client`）
1. `CnkiHttpClient.from_store()` 载入加密 store（`config/session.enc`，Fernet 加密）；
2. 若 `is_session_valid` 为真 → 直接复用；
3. 否则 `login()` 重登 → `save_to_store()` 落盘；
4. `force_login=True` 时跳过复用，强制重登。

### 5.4 代码层判定特征（features）
- 本地无 `_CNKI_AUTH_COOKIES` → 未登录；
- 探针 302→`verify/home` → 会话失效（滑动风控拦截匿名）；
- `session_store.is_expired()` / `saved_at` 超过 `max_age` → 落盘过期；
- `Ecp_LoginStuts.ShowName` 不匹配预期机构 → 机构身份不符。

---

## 6. 代码契约（导出符号）

模块 `src/cnki_api/auth.py`（由 `src/cnki_api/__init__.py` 再导出）：

```python
class LoginError(Exception): ...                      # 登录失败（凭证/风控/网络/流程中断）
@dataclass
class AuthResult:                                     # ok, method, message, cookies
    ok: bool; method: str; message: str; cookies: dict

def login(creds=None, client=None, force=False) -> AuthResult: ...
def get_authenticated_client(force_login=False) -> CnkiHttpClient: ...
def is_session_valid(client) -> bool: ...
def session_summary(client) -> dict: ...              # 仅含非敏感字段
```

- `login` **不抛异常**：失败返回 `AuthResult(ok=False, message=原因)`，便于调用方判定。
- `session_summary` 返回 `{is_logged_in, method, institution, username, login_saved_at, cookie_expires}`，**不含任何密码 / 令牌明文**。

---

## 7. 未完成 / 风险 / 诚实备注

> A2 任务范围内，纯 HTTP 登录**已完整成功**，以下为已知边界与后续注意点（非阻塞）。

1. **仅验证示例大学一家机构**。其他机构需在其 `idp` entityID 写入 `INSTITUTION_ENTITY_ID`（取自 `fsso/DiscoFeed`）；当前 `login()` 对未配置机构抛 `LoginError`。
2. **consent 表单兼容性**：代码仅当检测到 consent 表单时才处理「属性释放同意」；若机构关闭 consent，该步自然跳过（已兼容）。
3. **`ShowName` 编码约定**：机构名在 `Ecp_LoginStuts` 内为 URL 编码，按 `unquote` 还原；若 CNKI 改编码需同步调整 `_decode_login_status`。
4. **滑块（Round 3）已天然规避**：滑块仅拦截匿名会话；已认证会话对内容入口返回 200，无需介入。浏览器兜底（Round 4/5）**未触发、未实现**，符合「纯 HTTP 优先」纪律。
5. **限流 / 并发**：所有请求经 `CnkiHttpClient`（代理规避 + ≥2s 节流），重登不并发轰炸。
6. **凭证安全**：仅 `secrets/account.txt`；无明文日志；store 经 Fernet 加密落盘。
7. **会话时长**：依赖 `c_m_expire`（≈30 天）；长周期无人使用的桌面场景需按 §5 自动重登。

---

## 8. 复现命令

```bash
# 单跑验收（6 项检查）
.venv/Scripts/python.exe tools/verify_login.py

# 连续 3 次（间隔 ≥2s）确认持久化
for i in 1 2 3; do .venv/Scripts/python.exe tools/verify_login.py; sleep 3; done
```
