"""知网机构登录（纯 HTTP / 无浏览器 / 无 CDP）。

链路（实测确认）：
    fsso.cnki.net/Shibboleth.sso/Login?entityID=<IdP entityID>&target=<fsso/secure/default.aspx>
      -> 302 <你学校 IdP 域名>/idp/profile/SAML2/Redirect/SSO?SAMLRequest=...
      -> 200 idp 登录表单(j_username / j_password / _eventId_proceed)   # 明文、无验证码、无 RSA
      -> POST 凭证
      -> 200 Shibboleth 属性授权(consent) 表单 -> POST _eventId_proceed
      -> 200 SAML POST 绑定表单(SAMLResponse) -> POST 到真实 SP ACS fsso.cnki.net/Shibboleth.sso/SAML2/POST
      -> 302 fsso.cnki.net/secure/default.aspx
      -> 302 www.cnki.net  -> 拿到 CNKI 会话 Cookie(Ecp_ClientId / LID / Ecp_LoginStuts / c_m_LinID / SID)

要点（纪律）：
- 凭证只经 credential.load_credentials() 读取，绝不打印明文 / 绝不进日志。
- 所有请求走 CnkiHttpClient（已内置代理规避 + ≥2s 节流 + 日志脱敏）。
- 不集中轰炸：依次跟重定向，单请求间隔由客户端节流保证。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

from cnki_hover.credential import Credentials, load_credentials
from cnki_hover.http_client import CnkiHttpClient
from cnki_hover.log import get_logger, log_event
from cnki_hover.session_store import get_store

LOG = get_logger("cnki_auth")

# ---- 机构 -> IdP entityID 映射 ----
# ⚠️ **刻意不内置任何具体学校**：发布版不含使用者的机构/学校信息（隐私）。
# 机构由使用者自行配置：在运行目录放 `institutions.json`（键=机构名，值=IdP entityID）。
# entityID 从 CARSI 联邦的 DiscoFeed 获取：
#   https://fsso.cnki.net/Shibboleth.sso/DiscoFeed  → 找到你学校的 entityID
INSTITUTION_ENTITY_ID: "dict[str, str]" = {}
_INSTITUTION_FILE = "institutions.json"


def load_institutions() -> dict:
    """加载机构映射（运行目录的 `institutions.json`）。每次调用都重读，改完即生效。"""
    global INSTITUTION_ENTITY_ID
    from cnki_hover.paths import PROJECT_ROOT  # 局部导入避免循环依赖

    loaded: dict = {}
    p = PROJECT_ROOT / _INSTITUTION_FILE
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                loaded = {str(k): str(v) for k, v in data.items() if str(v).strip()}
    except Exception as e:  # noqa: BLE001
        log_event(LOG, "institutions_load_failed", error=str(e)[:120])
    INSTITUTION_ENTITY_ID = loaded
    return loaded


def configured_institutions() -> tuple:
    """当前已配置的机构名列表（供登录界面下拉）。"""
    load_institutions()
    return tuple(sorted(INSTITUTION_ENTITY_ID.keys()))


# ---------------------------------------------------------------- CARSI 机构清单
# 有了它就**不需要任何手工配置**：运行时从 CARSI 联邦取回全国机构清单
# （实测 7967 条，含 entityID + 中文名），用户敲自己学校名即可自动补全并登录。
DISCO_FEED_URL = "https://fsso.cnki.net/Shibboleth.sso/DiscoFeed"
DISCO_CACHE_FILE = "institutions.disco.json"


def _bare_client():
    """未登录也能用的客户端（DiscoFeed 是公开接口，不需要会话）。"""
    from cnki_hover.http_client import CnkiHttpClient  # 局部导入避免循环依赖

    try:
        return CnkiHttpClient()
    except TypeError:
        from cnki_hover.config import load_config

        return CnkiHttpClient(load_config())


def fetch_discofeed(force: bool = False) -> dict:
    """拉取 CARSI 机构清单并缓存（精简为 `中文名 -> entityID`）。

    首次约 11MB，缓存后仅几百 KB；之后完全离线可用。
    """
    from cnki_hover.paths import PROJECT_ROOT  # 局部导入避免循环依赖

    cache = PROJECT_ROOT / DISCO_CACHE_FILE
    if not force and cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "discofeed_cache_bad", error=str(e)[:100])
    try:
        r = _bare_client().get(DISCO_FEED_URL)
        data = json.loads(r.content.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        log_event(LOG, "discofeed_failed", error=str(e)[:120])
        return {}
    out: dict = {}
    for it in data if isinstance(data, list) else []:
        eid = (it.get("entityID") or "").strip()
        if not eid:
            continue
        zh = [d.get("value", "") for d in (it.get("DisplayNames") or [])
              if d.get("lang") == "zh"] or [d.get("value", "") for d in (it.get("DisplayNames") or [])]
        for n in zh:
            n = (n or "").strip()
            if n:
                out[n] = eid
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log_event(LOG, "discofeed_cache_write_failed", error=str(e)[:100])
    log_event(LOG, "discofeed_loaded", count=len(out))
    return out


def search_institutions(keyword: str, limit: int = 60) -> list:
    """按关键字模糊匹配机构名（用户自定义优先，然后是 CARSI 清单）。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    pool: dict = {}
    pool.update(INSTITUTION_ENTITY_ID)
    try:
        for k, v in fetch_discofeed(force=False).items():
            pool.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    hits = [n for n in pool if kw in n]
    hits.sort(key=lambda n: (0 if n.startswith(kw) else 1, len(n)))
    return hits[:limit]


def all_institution_names() -> list:
    """机构名全集（供自动补全）。"""
    pool: dict = {}
    pool.update(INSTITUTION_ENTITY_ID)
    try:
        for k, v in fetch_discofeed(force=False).items():
            pool.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    return sorted(pool.keys())


def resolve_institution(name: str) -> str:
    """机构名 -> entityID。本地配置 → CARSI 精确 → CARSI 唯一模糊匹配。"""
    name = (name or "").strip()
    if not name:
        return ""
    load_institutions()
    if name in INSTITUTION_ENTITY_ID:
        return INSTITUTION_ENTITY_ID[name]
    try:
        pool = fetch_discofeed(force=False)
    except Exception:  # noqa: BLE001
        pool = {}
    if name in pool:
        return pool[name]
    cands = {eid for n, eid in pool.items() if name in n}
    if len(cands) == 1:
        return next(iter(cands))
    return ""


def remember_institution(name: str, entity_id: str) -> None:
    """登录成功后记住机构 → 写入 `institutions.json`，之后**完全免配置**。"""
    from cnki_hover.paths import PROJECT_ROOT  # 局部导入避免循环依赖

    name, entity_id = (name or "").strip(), (entity_id or "").strip()
    if not name or not entity_id:
        return
    p = PROJECT_ROOT / _INSTITUTION_FILE
    data: dict = {}
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get(name) == entity_id:
        INSTITUTION_ENTITY_ID[name] = entity_id
        return
    data[name] = entity_id
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        INSTITUTION_ENTITY_ID[name] = entity_id
        log_event(LOG, "institution_remembered", institution=name)
    except Exception as e:  # noqa: BLE001
        log_event(LOG, "institution_remember_failed", error=str(e)[:100])

# 真实的 SP ACS（SAML POST 绑定消费地址），consent 表单的 action 是 IdP 的
# "Redirect 包装"，需拆出内嵌的真实 ACS 再 POST。
FSSO_SP_ACS = "https://fsso.cnki.net/Shibboleth.sso/SAML2/POST"

# fsso「前往」按钮默认 target（login2.js: encodeURIComponent("https://fsso.cnki.net/secure/default.aspx")）
FSSO_TARGET = "https://fsso.cnki.net/secure/default.aspx"

# Shibboleth IdP 登录表单字段（明文，无 RSA / 无验证码）
IDP_USER_FIELD = "j_username"
IDP_PASS_FIELD = "j_password"

# 会话有效性探针用的内容入口（不被滑块拦截即视为已认证）
CNKI_CONTENT_PROBE = "https://kns.cnki.net/kns8s/defaultresult/index"

# 登录态在 .cnki.net 上的关键 Cookie
_CNKI_AUTH_COOKIES = ("Ecp_ClientId", "Ecp_LoginStuts", "LID", "c_m_LinID", "SID")


class LoginError(Exception):
    """机构登录失败（凭证错误 / 风控 / 网络 / 流程中断）。"""


@dataclass
class AuthResult:
    ok: bool
    method: str
    message: str
    cookies: dict = field(default_factory=dict)


def _entity_id_for(institution: str) -> str:
    """机构名 -> entityID（本地配置 / CARSI 清单）。找不到时给出可操作的提示。"""
    eid = resolve_institution(institution)
    if eid:
        return eid
    cands = search_institutions(institution, limit=8)
    tip = ""
    if cands:
        tip = "你是否想找：" + "、".join(cands[:6])
    else:
        tip = ("请确认机构名（可从输入框的下拉列表里选）；"
               "首次使用需要联网获取一次机构清单。")
    raise LoginError(
        f"未能确定机构「{institution}」的 IdP entityID。\n{tip}"
    )


def _extract_form(html_text: str, base_url: str):
    """从 HTML 抽取第一个 <form> 的 (action绝对URL, {字段名: 值})。无表单返回 None。"""
    fm = re.search(r'<form[^>]*action="([^"]*)"[^>]*?(?:method="([^"]*)")?', html_text, re.I)
    if not fm:
        return None
    action = html.unescape(urljoin(base_url, fm.group(1) or ""))
    inputs: dict[str, str] = {}
    for m in re.finditer(r'<input\b([^>]*)>', html_text, re.I):
        attrs = m.group(1)
        nam = re.search(r'name="([^"]*)"', attrs, re.I)
        val = re.search(r'value="([^"]*)"', attrs, re.I)
        if nam and val is not None:
            inputs[html.unescape(nam.group(1))] = html.unescape(val.group(1))
    return action, inputs


def _start_url(entity_id: str) -> str:
    target = quote(FSSO_TARGET, safe="")
    return (
        "https://fsso.cnki.net/Shibboleth.sso/Login"
        f"?entityID={entity_id}&target={target}"
    )


def _follow_saml_login(client: CnkiHttpClient, creds: Credentials) -> None:
    """在 client.session 上完成 SAML/CARSI 登录。失败抛 LoginError。"""
    sess = client.session
    entity_id = _entity_id_for(creds.institution)
    url = _start_url(entity_id)

    # 1) 跟随 302 直到拿到 IdP 登录表单
    login_html, login_url = None, None
    for _ in range(8):
        r = sess.get(url, allow_redirects=False, timeout=20)
        if r.status_code == 200 and IDP_USER_FIELD in r.text:
            login_url, login_html = r.url, r.text
            break
        loc = r.headers.get("Location")
        if not loc:
            raise LoginError(f"登录链中断（未拿到 IdP 表单），末跳：{r.url}")
        url = loc if loc.startswith("http") else urljoin(url, loc)
    if not login_html:
        raise LoginError("登录链中断：未能到达 IdP 登录表单")

    # 2) POST 明文凭证
    form = _extract_form(login_html, login_url)
    if not form:
        raise LoginError("IdP 登录表单无法解析")
    post_url, _ = form
    r2 = sess.post(
        post_url,
        data={
            IDP_USER_FIELD: creds.username,
            IDP_PASS_FIELD: creds.password,
            "_eventId_proceed": "Login",
            "donotcache": "1",
        },
        allow_redirects=False,
        timeout=20,
    )

    # 3) 跟随 consent 表单 + SAML POST 绑定，直到落到 cnki 内容域
    cur, resp = post_url, r2
    for _ in range(16):
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            nxt = loc if loc.startswith("http") else urljoin(cur, loc)
            resp = sess.get(nxt, allow_redirects=False, timeout=20)
            cur = nxt
            continue
        if resp.status_code == 200:
            f = _extract_form(resp.text, cur)
            if f and ("SAMLResponse" in f[1] or "_eventId_proceed" in f[1]):
                aurl, inputs = f
                # consent: 只保留「同意」，去掉「拒绝」
                inputs.pop("_eventId_AttributeReleaseRejected", None)
                # IdP 的 Redirect 包装：拆出内嵌的真实 SP ACS
                if "SAML2/Redirect/https://" in aurl:
                    aurl = aurl.split("SAML2/Redirect/", 1)[1]
                resp = sess.post(aurl, data=inputs, allow_redirects=False, timeout=20)
                cur = aurl
                continue
            # 终止页（已落地）
            break
        raise LoginError(f"登录链异常：HTTP {resp.status_code} @ {cur}")

    # 4) 校验是否真的拿到 CNKI 会话 Cookie
    got = [c.name for c in sess.cookies if c.name in _CNKI_AUTH_COOKIES]
    if not got:
        raise LoginError("登录流程结束但未获得 CNKI 会话 Cookie（可能凭证被拒或风控）")
    log_event(LOG, "cnki_login_ok", institution=creds.institution,
              cookies=len(got))


def login(
    creds: Optional[Credentials] = None,
    client: Optional[CnkiHttpClient] = None,
    force: bool = False,
) -> AuthResult:
    """纯 HTTP 完成机构登录。

    返回 AuthResult(ok, method, message, cookies)；失败 ok=False 且 message 说明原因
    （不抛异常，方便调用方判断）。凭证错误 / 风控表现为 ok=False。
    """
    if creds is None:
        try:
            creds = load_credentials()
        except Exception as e:  # 凭证缺失 / 占位符
            return AuthResult(ok=False, method="carsi-saml",
                              message=f"凭证读取失败：{e}")

    own_client = client is None
    if client is None:
        client = CnkiHttpClient(min_interval=2.0)
    try:
        _follow_saml_login(client, creds)
    except LoginError as e:
        if own_client:
            # 不保留半截会话
            pass
        return AuthResult(ok=False, method="carsi-saml", message=str(e))
    except Exception as e:  # 网络等
        return AuthResult(ok=False, method="carsi-saml",
                          message=f"登录异常：{type(e).__name__}: {e}")

    cookies = {c.name: c.value for c in client.session.cookies}
    return AuthResult(
        ok=True,
        method="carsi-saml",
        message=f"机构登录成功（{creds.institution}）",
        cookies=cookies,
    )


def is_session_valid(client: CnkiHttpClient) -> bool:
    """会话是否仍可用：本地有 CNKI 认证 Cookie，且对内容入口的探针不被滑块拦截。"""
    sess = client.session
    # 1) 本地 Cookie 快速判定
    has_auth_cookie = any(
        c.name in _CNKI_AUTH_COOKIES for c in sess.cookies
    )
    if not has_auth_cookie:
        return False
    # 2) 网络探针：已认证会话访问内容入口应返回 200，而非 302 到 verify/home（滑块）
    try:
        r = client.get(CNKI_CONTENT_PROBE, allow_redirects=False)
    except Exception:
        return False
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        if "verify/home" in loc:
            return False  # 被滑块拦截 => 会话失效
    return r.status_code == 200


_CLIENT_SINGLETON: Optional[CnkiHttpClient] = None


def reset_authenticated_client() -> None:
    """丢弃进程内缓存的认证客户端（测试 / 强制重登前调用）。"""
    global _CLIENT_SINGLETON
    _CLIENT_SINGLETON = None


def get_authenticated_client(force_login: bool = False) -> "CnkiHttpClient":
    """优先复用加密存储的会话；失效（或 force_login）则重登，重登失败抛 LoginError。

    ⚠️ 进程内做**单例缓存**：`is_session_valid()` 会发一次在线探针（实测 ~0.35s），
    若每次调用都探针，则阅读/检索等所有下游调用都会平白多付 ~0.35s，
    而且每次新建 Session 会丢掉连接复用与节流状态。启动时校验一次即可。
    """
    global _CLIENT_SINGLETON
    if not force_login and _CLIENT_SINGLETON is not None:
        return _CLIENT_SINGLETON

    client = CnkiHttpClient.from_store() if not force_login else CnkiHttpClient(min_interval=2.0)
    if not force_login and is_session_valid(client):
        _CLIENT_SINGLETON = client
        return client
    # 重登
    res = login(client=client, force=force_login)
    if not res.ok:
        raise LoginError(res.message)
    client.save_to_store()
    # 登录成功 → 记住该机构（写 institutions.json），之后**完全免配置**启动
    remember_institution(str(creds.institution or ""),
                         resolve_institution(str(creds.institution or "")))
    _CLIENT_SINGLETON = client
    return client


def _decode_login_status(client: CnkiHttpClient) -> dict:
    """从 Ecp_LoginStuts Cookie 解析登录态（机构名 / 用户名），不含敏感值。"""
    info: dict = {}
    for c in client.session.cookies:
        if c.name == "Ecp_LoginStuts":
            try:
                import json as _json
                raw = _json.loads(c.value)
                if isinstance(raw, dict):
                    name = raw.get("ShowName")
                    if name:
                        # ShowName 在 JSON 内为 URL 编码（如 %E7%A6%8F...）
                        info["institution"] = unquote(name)
                    if raw.get("UserName"):
                        info["username"] = raw.get("UserName")
            except Exception:
                pass
    return info


def session_summary(client: CnkiHttpClient) -> dict:
    """供 UI 展示的会话摘要（不含任何敏感值）。"""
    valid = is_session_valid(client)
    status = _decode_login_status(client)
    store = get_store()
    saved_at = None
    if store.exists():
        data = store.load()
        if data:
            saved_at = data.get("saved_at")
    expires = None
    for c in client.session.cookies:
        if c.name == "c_m_expire":
            expires = c.value
    return {
        "is_logged_in": valid,
        "method": "carsi-saml",
        "institution": status.get("institution"),
        "username": status.get("username"),
        "login_saved_at": saved_at,
        "cookie_expires": expires,
    }
