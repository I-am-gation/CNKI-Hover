"""登录模块集成（B2 产出）。

把 A2 的登录链路与 A1 的加密会话存储，接进 B1 的应用壳：

    LoginDialog（引导登录 UI：机构 + 账号 + 密码）
        │
        ▼
    LoginController ── 复用/重登/过期判定 ──► cnki_api.auth
        │
        ├──► session_store（Fernet 加密落盘）
        └──► tray.set_login_state()（tooltip 状态）

公共契约（下游 C5 设置页 / B3 / D1 E2E 依赖）：
    ctl = LoginController(tray=..., config=...)
    ctl.state_changed: Signal[state, detail]
    ctl.current_state() -> str
    ctl.summary() -> dict                 # 非敏感会话摘要
    ctl.has_persisted() -> bool
    ctl.silent_login() -> bool            # 复用持久化会话（零二次登录）
    ctl.login_with(username, password, institution=None) -> (ok: bool, msg: str)
    ctl.check() -> str                    # 探测会话状态
    ctl.logout() -> None
    ctl.last_action: str                  # "reuse" | "login" | "none"（供验收断言）
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from cnki_api import auth as A
from . import theme
from .config import Config, load_config
from cnki_hover.credential import Credentials, load_credentials
from cnki_hover.http_client import CnkiHttpClient
from cnki_hover.log import get_logger, log_event
from cnki_hover.session_store import get_store

LOG = get_logger("login_ui")

# 默认机构留空：由使用者在 config.json / institutions.json 里配置（隐私考虑，不内置任何学校）
DEFAULT_INSTITUTION = ""

QSS = """
QDialog { background: #16181D; }
QLabel  { color: #EDEFF3; font-size: 13px; }
QLabel#title { font-size: 16px; font-weight: 600; }
QLabel#sub   { color: #9AA3B2; font-size: 12px; }
QLabel#err   { color: #FF6B6B; font-size: 12px; }
QLineEdit {
    background: #1F2229; border: 1px solid rgba(255,255,255,38);
    border-radius: 8px; color: #EDEFF3; padding: 7px 10px; font-size: 13px;
}
QLineEdit:focus { border: 1px solid #4C8DFF; }
QPushButton {
    background: #4C8DFF; border: none; border-radius: 8px;
    color: #FFFFFF; padding: 8px 18px; font-size: 13px;
}
QPushButton:disabled { background: #39404D; color: #9AA3B2; }
QPushButton#ghost { background: transparent; border: 1px solid rgba(255,255,255,38); color: #9AA3B2; }
"""


class LoginController(QObject):
    """登录状态机 + 托盘联动。"""

    STATE_LOGGED_OUT = "logged_out"
    STATE_LOGGED_IN = "logged_in"
    STATE_EXPIRED = "expired"
    STATE_ERROR = "error"
    STATE_LOGGING_IN = "logging_in"

    state_changed = Signal(str, str)

    def __init__(self, tray: Optional[QWidget] = None, config: Optional[Config] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.tray = tray
        self.config = config or load_config()
        self.store = get_store()
        self._state = self.STATE_LOGGED_OUT
        self._detail = ""
        self._client: Optional[CnkiHttpClient] = None
        self.last_action = "none"
        self.login_attempts = 0

    # ---------------------------------------------------------------- 状态
    @property
    def state(self) -> str:
        return self._state

    def current_state(self) -> str:
        return self._state

    def _set_state(self, state: str, detail: str = "") -> None:
        self._state, self._detail = state, detail
        if self.tray is not None:
            try:
                logged = state == self.STATE_LOGGED_IN
                self.tray.set_login_state(logged, detail)
            except Exception:  # noqa: BLE001
                pass
        log_event(LOG, "login_state", state=state, detail=detail[:60])
        self.state_changed.emit(state, detail)

    # ---------------------------------------------------------------- 会话复用
    def has_persisted(self) -> bool:
        try:
            return bool(self.store.exists() and self.store.load())
        except Exception:
            return False

    def _reuse_client(self) -> Optional[CnkiHttpClient]:
        """尝试用落盘会话建 client 并在线校验；成功返回 client，否则 None。"""
        if not self.has_persisted():
            return None
        try:
            client = CnkiHttpClient.from_store()
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "reuse_construct_failed", error=str(e)[:100])
            return None
        if A.is_session_valid(client):
            return client
        return None

    def silent_login(self) -> bool:
        """静默复用持久化登录态。**不发起登录请求**（零二次登录）。"""
        client = self._reuse_client()
        if client is not None:
            self._client = client
            self.last_action = "reuse"
            s = A.session_summary(client)
            inst = s.get("institution") or DEFAULT_INSTITUTION
            self._set_state(self.STATE_LOGGED_IN, inst)
            log_event(LOG, "silent_login_ok", institution=inst)
            return True
        if self.has_persisted():
            # 有落盘会话但已失效 → 过期
            self.last_action = "none"
            self._set_state(self.STATE_EXPIRED, "会话已过期，请重新登录")
        else:
            self.last_action = "none"
            self._set_state(self.STATE_LOGGED_OUT, "尚未登录")
        return False

    # ---------------------------------------------------------------- 显式登录
    def login_with(self, username: str, password: str,
                   institution: Optional[str] = None) -> Tuple[bool, str]:
        """用给定凭证登录。失败**不抛**，返回 (False, 可读原因)，应用继续存活。"""
        inst = institution or str(self.config.get("institution", DEFAULT_INSTITUTION))
        if not username or not password:
            msg = "账号与密码不能为空"
            self._set_state(self.STATE_ERROR, msg)
            return False, msg

        self._set_state(self.STATE_LOGGING_IN, inst)
        self.login_attempts += 1
        self.last_action = "login"
        creds = Credentials(institution=inst, username=username, password=password)
        try:
            res = A.login(creds=creds)
        except Exception as e:  # noqa: BLE001  —— 任何异常都不得让 UI 崩
            msg = "登录异常：%s: %s" % (type(e).__name__, e)
            self._set_state(self.STATE_ERROR, msg)
            log_event(LOG, "login_exception", error=str(e)[:150])
            return False, msg

        if not res.ok:
            self._set_state(self.STATE_ERROR, res.message or "登录失败")
            log_event(LOG, "login_failed", method=res.method, msg=(res.message or "")[:120])
            return False, res.message or "登录失败"

        # 落盘（加密）：res.cookies 已是会话 cookie 全量字典
        ok_store = self._persist_from_result(res)
        if ok_store:
            self._client = CnkiHttpClient.from_store()
        else:
            # cookies 为空（异常情形）：退回标准通道取会话
            try:
                self._client = A.get_authenticated_client(force_login=False)
            except Exception as e:  # noqa: BLE001
                self._set_state(self.STATE_ERROR, "登录成功但会话保存失败：%s" % e)
                return False, "登录成功但会话保存失败"

        s = A.session_summary(self._client)
        inst2 = s.get("institution") or inst
        self._set_state(self.STATE_LOGGED_IN, inst2)
        log_event(LOG, "login_ok", method=res.method, institution=inst2)
        return True, "登录成功（%s）" % inst2

    def _persist_from_result(self, res) -> bool:
        """把登录结果里的会话写入加密存储。"""
        try:
            cookies = res.cookies or {}
            if not cookies:
                return False
            self.store.save({"cookies": cookies, "headers": {}})
            return True
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "persist_failed", error=str(e)[:120])
            return False

    # ---------------------------------------------------------------- 探测 / 登出
    def check(self) -> str:
        """在线探测当前会话状态（供托盘提示 / 打开搜索前调用）。"""
        if self._client is None:
            if not self.has_persisted():
                self._set_state(self.STATE_LOGGED_OUT, "尚未登录")
                return self._state
            try:
                self._client = CnkiHttpClient.from_store()
            except Exception:
                self._set_state(self.STATE_LOGGED_OUT, "尚未登录")
                return self._state
        if A.is_session_valid(self._client):
            s = A.session_summary(self._client)
            self._set_state(self.STATE_LOGGED_IN, s.get("institution") or DEFAULT_INSTITUTION)
        else:
            self._set_state(self.STATE_EXPIRED, "会话已过期，请重新登录")
        return self._state

    def logout(self) -> None:
        try:
            self.store.clear()
        except Exception:  # noqa: BLE001
            pass
        self._client = None
        self.last_action = "none"
        self._set_state(self.STATE_LOGGED_OUT, "已退出登录")

    def summary(self) -> dict:
        if self._client is None:
            return {"is_logged_in": False, "state": self._state, "detail": self._detail}
        try:
            s = dict(A.session_summary(self._client))
        except Exception:
            s = {"is_logged_in": False}
        s["state"] = self._state
        s["detail"] = self._detail
        return s

    def credentials_from_file(self) -> Optional[Credentials]:
        try:
            return load_credentials()
        except Exception:
            return None

    # ---------------------------------------------------------------- 首启引导
    def ensure_login(self, interactive: bool = True,
                     parent: Optional[QWidget] = None) -> bool:
        """首启/会话过期时的统一入口：先静默复用，失败再弹登录窗（interactive=True）。"""
        if self.silent_login():
            return True
        if not interactive:
            return False
        dlg = LoginDialog(self, parent=parent)
        return dlg.exec() == QDialog.Accepted


class LoginDialog(QDialog):
    """机构登录引导窗（纯原生，不内嵌浏览器）。"""

    def __init__(self, controller: LoginController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctl = controller
        theme.bind(self, "login", QSS)
        self.setWindowTitle("登录知网（校外访问）")
        self.setMinimumWidth(420)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        t = QLabel("登录知网 · 机构校外访问")
        t.setObjectName("title")
        root.addWidget(t)
        sub = QLabel("使用学校统一身份认证账号登录一次，登录态会加密保存在本机，"
                     "之后长期免登录。凭证不上传、不共享。")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        root.addWidget(sub)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(10)

        self.ed_institution = QLineEdit(str(self.ctl.config.get("institution", DEFAULT_INSTITUTION)))
        self.ed_username = QLineEdit()
        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_username.setPlaceholderText("学号 / 工号")
        self.ed_password.setPlaceholderText("统一身份认证密码")

        form.addRow("机构", self.ed_institution)
        form.addRow("账号", self.ed_username)
        form.addRow("密码", self.ed_password)
        root.addLayout(form)

        self.lb_err = QLabel("")
        self.lb_err.setObjectName("err")
        self.lb_err.setWordWrap(True)
        root.addWidget(self.lb_err)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("稍后")
        self.btn_cancel.setObjectName("ghost")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_login = QPushButton("登录")
        self.btn_login.setDefault(True)
        self.btn_login.clicked.connect(self.submit)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_login)
        root.addLayout(btns)

        self.ed_password.returnPressed.connect(self.submit)

    # ---- 供自动化验收使用 ----
    def set_credentials(self, username: str, password: str, institution: Optional[str] = None) -> None:
        if institution:
            self.ed_institution.setText(institution)
        self.ed_username.setText(username)
        self.ed_password.setText(password)

    def submit(self) -> bool:
        self.lb_err.setText("")
        self.btn_login.setEnabled(False)
        self.btn_login.setText("登录中…")
        self.repaint()
        ok, msg = self.ctl.login_with(
            self.ed_username.text().strip(),
            self.ed_password.text(),
            self.ed_institution.text().strip() or DEFAULT_INSTITUTION,
        )
        self.btn_login.setEnabled(True)
        self.btn_login.setText("登录")
        if ok:
            self.accept()
        else:
            self.lb_err.setText(msg)
        return ok
