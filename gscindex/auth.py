"""凭据池：加载密钥、缓存 access token、按配额分派。

支持两种凭据，下游只认 .token() 这一个接口：

* 服务账号  —— 需要在每个 GSC 属性里把它的邮箱加为所有者
* OAuth 用户 —— 以你本人身份调用，你名下的属性全部自动可用，GSC 权限无需改动
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials

from . import oauth

# 服务账号一次授权拿全，Indexing 和 Search Console 共用同一个 token
SCOPES = [
    "https://www.googleapis.com/auth/indexing",
    "https://www.googleapis.com/auth/webmasters",
]

KIND_SERVICE = "service_account"
KIND_OAUTH = "oauth_user"
KIND_CN = {KIND_SERVICE: "服务账号", KIND_OAUTH: "OAuth 用户"}


class AuthError(Exception):
    pass


# Google 的认证报错基本没法直接给用户看，这里翻译成能照着做的提示
AUTH_HINTS = [
    ("account not found",
     "Google 端找不到这个服务账号。多半是它已在 GCP 控制台被删除，请重新生成一份 JSON 密钥。"),
    ("invalid jwt signature",
     "密钥签名校验失败。JSON 文件可能损坏或被改动过，请重新下载原始密钥。"),
    ("invalid_scope",
     "授权范围被拒绝。请确认该 GCP 项目已经启用 Indexing API 和 Search Console API。"),
    ("unauthorized_client",
     "该客户端未获授权，请检查服务账号是否被停用。"),
    ("invalid jwt",
     "JWT 校验失败。最常见的原因是本机系统时间不准，请先校准时间再试。"),
    ("invalid_grant",
     "授权被拒绝。请确认密钥是最新的，且本机系统时间准确。"),
]

# OAuth 凭据失效的原因和服务账号完全不同，单独一套提示
OAUTH_HINTS = [
    ("invalid_grant",
     "授权已失效，需要重新授权一次。最常见的原因是 GCP 的「OAuth 同意屏幕」"
     "还停在「测试中」状态 —— 这种状态下 refresh token 只有 7 天有效期。"
     "请把发布状态改成「正式版 / In production」，之后就不会再频繁掉线。"
     "另一种可能是你在 Google 账号的第三方授权页里手动移除了本应用。"),
    ("invalid_client",
     "OAuth 客户端凭据无效。请重新下载客户端 JSON 并上传，然后重新授权。"),
    ("invalid_scope",
     "授权范围被拒绝。请确认该 GCP 项目已启用 Indexing API 和 Search Console API，"
     "然后重新授权。"),
]

NETWORK_HINTS = ("timed out", "connection", "max retries", "getaddrinfo", "ssl", "proxy")


def friendly_auth_error(raw: str, kind: str = KIND_SERVICE) -> str:
    """把底层认证异常转成可操作的中文提示，原文附在括号里备查。"""
    low = raw.lower()
    table = OAUTH_HINTS if kind == KIND_OAUTH else AUTH_HINTS
    for key, hint in table:
        if key in low:
            return hint + "（原始信息：" + raw[:120] + "）"
    if any(k in low for k in NETWORK_HINTS):
        return "无法连接 Google 服务器，请检查网络或代理设置。（原始信息：" + raw[:120] + "）"
    return raw[:240]


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------


class Credential:
    """凭据基类。下游只依赖 name / email / token() 这几样。"""

    kind = KIND_SERVICE

    def __init__(self, name: str, email: str, project_id: str, path: Path):
        self.name = name
        self.email = email
        self.project_id = project_id
        self.path = path
        self._creds = None
        self._lock = threading.Lock()

    @property
    def kind_cn(self) -> str:
        return KIND_CN.get(self.kind, self.kind)

    @property
    def submit_scope(self) -> str:
        return "submit:" + self.name

    @property
    def inspect_scope(self) -> str:
        return "inspect:" + self.name

    def _build(self):
        raise NotImplementedError

    def token(self) -> str:
        """返回有效的 access token，过期自动刷新。"""
        with self._lock:
            if self._creds is None:
                try:
                    self._creds = self._build()
                except AuthError:
                    raise
                except Exception as exc:
                    raise AuthError(
                        friendly_auth_error("凭据无法加载: " + str(exc), self.kind)
                    ) from exc
            if not self._creds.valid:
                try:
                    self._creds.refresh(Request())
                except Exception as exc:
                    raise AuthError(friendly_auth_error(str(exc), self.kind)) from exc
            return self._creds.token

    def invalidate(self) -> None:
        with self._lock:
            self._creds = None


class ServiceAccountCredential(Credential):
    kind = KIND_SERVICE

    def _build(self):
        return service_account.Credentials.from_service_account_file(
            str(self.path), scopes=SCOPES
        )


class OAuthCredential(Credential):
    kind = KIND_OAUTH

    def __init__(self, name, email, project_id, path, data: dict):
        super().__init__(name, email, project_id, path)
        self._data = data

    def _build(self):
        return UserCredentials(
            token=None,
            refresh_token=self._data["refresh_token"],
            client_id=self._data["client_id"],
            client_secret=self._data["client_secret"],
            token_uri=oauth.TOKEN_ENDPOINT,
            scopes=oauth.SCOPES,
        )

    @property
    def refresh_token(self) -> str:
        return self._data.get("refresh_token", "")


# 兼容旧名字
Account = Credential


# --------------------------------------------------------------------------
# 凭据池
# --------------------------------------------------------------------------


class AccountPool:
    """管理 accounts/ 目录下的全部凭据文件（服务账号密钥与 OAuth 授权）。"""

    def __init__(self, directory: Path, store):
        self.directory = Path(directory)
        self.store = store
        self._lock = threading.Lock()
        self.accounts: list[Credential] = []
        self.errors: list[tuple[str, str]] = []
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self.accounts = []
            self.errors = []
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    self.errors.append((path.name, "不是合法的 JSON: " + str(exc)))
                    continue
                if not isinstance(data, dict):
                    self.errors.append((path.name, "JSON 内容不是对象"))
                    continue

                kind = data.get("type")
                if kind == KIND_SERVICE:
                    cred = self._load_service(path, data)
                elif kind == KIND_OAUTH:
                    cred = self._load_oauth(path, data)
                elif data.get("installed") or data.get("web"):
                    self.errors.append(
                        (path.name, "这是 OAuth 客户端配置文件，不是凭据。"
                                    "请到「账号管理」页的 OAuth 区域上传它并完成授权。")
                    )
                    continue
                else:
                    self.errors.append(
                        (path.name, "无法识别的凭据类型，既不是服务账号密钥也不是 OAuth 授权")
                    )
                    continue
                if cred:
                    self.accounts.append(cred)

    def _load_service(self, path: Path, data: dict) -> Credential | None:
        if not data.get("client_email") or not data.get("private_key"):
            self.errors.append((path.name, "密钥内容不完整，缺少 client_email 或 private_key"))
            return None
        return ServiceAccountCredential(
            name=path.stem,
            email=data["client_email"],
            project_id=data.get("project_id", ""),
            path=path,
        )

    def _load_oauth(self, path: Path, data: dict) -> Credential | None:
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
        if missing:
            self.errors.append(
                (path.name, "OAuth 授权信息不完整，缺少 " + "、".join(missing) + "，请重新授权")
            )
            return None
        return OAuthCredential(
            name=path.stem,
            email=data.get("email", ""),
            project_id=data.get("project_id", ""),
            path=path,
            data=data,
        )

    def __len__(self) -> int:
        return len(self.accounts)

    def by_name(self, name: str) -> Credential | None:
        return next((a for a in self.accounts if a.name == name), None)

    @property
    def has_oauth(self) -> bool:
        return any(a.kind == KIND_OAUTH for a in self.accounts)

    # ---------- 配额 ----------

    def submit_remaining(self, account: Credential, limit: int) -> int:
        return max(0, limit - self.store.quota_used(account.submit_scope))

    def inspect_remaining(self, account: Credential, limit: int) -> int:
        return max(0, limit - self.store.quota_used(account.inspect_scope))

    def total_submit_remaining(self, limit: int) -> int:
        return sum(self.submit_remaining(a, limit) for a in self.accounts)

    def total_inspect_remaining(self, limit: int) -> int:
        return sum(self.inspect_remaining(a, limit) for a in self.accounts)

    def plan_submit(
        self, count: int, limit: int, accounts: list[Credential] | None = None
    ) -> list[tuple[Credential, int]]:
        """把 count 个 URL 按各凭据今日剩余配额切分，返回 [(凭据, 配额数), ...]。

        配额在这一步就原子扣掉，避免并发重复占用；实际没用完的由调用方退还。
        传 accounts 时只在这个子集里分配——调用方应该只传真正对目标站点有权限的凭据，
        否则配额会被没有权限的账号占掉，导致整批必然失败（曾经的真实 bug）。
        """
        return self._plan(count, limit, lambda a: a.submit_scope, accounts)

    def plan_inspect(
        self, count: int, limit: int, accounts: list[Credential] | None = None
    ) -> list[tuple[Credential, int]]:
        return self._plan(count, limit, lambda a: a.inspect_scope, accounts)

    def _plan(
        self, count: int, limit: int, scope_of, accounts: list[Credential] | None
    ) -> list[tuple[Credential, int]]:
        plan: list[tuple[Credential, int]] = []
        left = count
        pool = self.accounts if accounts is None else accounts
        with self._lock:
            for acc in pool:
                if left <= 0:
                    break
                got = self.store.quota_take(scope_of(acc), limit, left)
                if got:
                    plan.append((acc, got))
                    left -= got
        return plan
