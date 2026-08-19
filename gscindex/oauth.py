"""OAuth 用户授权：让工具以你本人的 Google 账号身份调用 API。

适用场景：你名下有多个 GSC 属性，且都在同一个 Google 账号里。
用你自己的身份授权后，GSC 的用户权限列表一个字都不用改。

授权码回调走本机的 /oauth/callback，不需要额外依赖。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
CLIENT_PATH = ROOT / "data" / "oauth_client.json"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# 第一个是干活用的（查站点/查收录/交站点地图），后两个只为在界面上显示"你授权的是哪个邮箱"。
# 已移除 indexing 权限——Indexing API 不在本项目里了。
SCOPES = [
    "https://www.googleapis.com/auth/webmasters",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class OAuthError(Exception):
    pass


# --------------------------------------------------------------------------
# 客户端配置（GCP 里创建的「OAuth 2.0 客户端 ID」，类型选「桌面应用」）
# --------------------------------------------------------------------------


def parse_client(raw: dict) -> dict:
    """从下载的客户端 JSON 里取出 client_id / client_secret。

    桌面应用类型的文件把内容包在 installed 键下，网页应用包在 web 下。
    """
    node = raw.get("installed") or raw.get("web")
    if not isinstance(node, dict):
        raise OAuthError(
            "这不是 OAuth 客户端配置文件。请在 GCP「API 和服务 → 凭据」里创建"
            "「OAuth 2.0 客户端 ID」（类型选「桌面应用」），下载它的 JSON。"
        )
    cid = node.get("client_id", "")
    secret = node.get("client_secret", "")
    if not cid or not secret:
        raise OAuthError("客户端配置不完整，缺少 client_id 或 client_secret。")
    return {
        "client_id": cid,
        "client_secret": secret,
        "project_id": node.get("project_id", ""),
    }


def save_client(raw: dict) -> dict:
    info = parse_client(raw)
    CLIENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_PATH.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def load_client() -> dict | None:
    if not CLIENT_PATH.exists():
        return None
    try:
        return json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def clear_client() -> None:
    CLIENT_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 授权流程
# --------------------------------------------------------------------------


def build_auth_url(redirect_uri: str, state: str) -> str:
    client = load_client()
    if not client:
        raise OAuthError("还没有上传 OAuth 客户端配置文件。")
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        # offline + consent 组合确保每次都能拿到 refresh_token
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return AUTH_ENDPOINT + "?" + urlencode(params)


def _token_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "")[:200]
    err = data.get("error", "")
    desc = data.get("error_description", "")
    if err == "redirect_uri_mismatch":
        return (
            "回调地址不被接受（redirect_uri_mismatch）。请确认 OAuth 客户端的类型是"
            "「桌面应用」——桌面应用允许任意本机回调地址；如果建成了「网页应用」，"
            "需要手动把回调地址加进它的「已获授权的重定向 URI」里。"
        )
    if err == "invalid_client":
        return "客户端凭据无效，请重新下载 OAuth 客户端 JSON 并上传。"
    if err == "invalid_grant":
        return "授权码已失效，请重新点一次授权（授权码只能用一次，且几分钟内有效）。"
    return (err + " " + desc).strip()[:240]


def exchange_code(code: str, redirect_uri: str) -> dict:
    """用授权码换取 refresh_token，并读回授权者邮箱。"""
    client = load_client()
    if not client:
        raise OAuthError("OAuth 客户端配置已丢失，请重新上传后再授权。")
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=45,
        )
    except requests.RequestException as exc:
        raise OAuthError("连不上 Google 令牌服务：" + str(exc)[:140]) from exc

    if resp.status_code != 200:
        raise OAuthError(_token_error(resp))

    payload = resp.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        raise OAuthError(
            "Google 没有返回 refresh_token。请到 GCP 的「OAuth 同意屏幕」检查配置后重试，"
            "或先到账号的第三方授权页移除本应用再重新授权。"
        )

    email = ""
    access = payload.get("access_token", "")
    if access:
        try:
            info = requests.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": "Bearer " + access},
                timeout=20,
            )
            if info.status_code == 200:
                email = info.json().get("email", "")
        except requests.RequestException:
            pass  # 邮箱只用于显示，取不到不影响使用

    return {
        "type": "oauth_user",
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "project_id": client.get("project_id", ""),
        "refresh_token": refresh,
        "email": email,
    }


def account_filename(creds: dict) -> str:
    """配额按 GCP 项目计算，所以凭据文件也按项目命名，一个项目一份。"""
    key = creds.get("project_id") or (creds.get("email", "").split("@")[0]) or "user"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in key)[:60]
    return "oauth-" + (safe or "user") + ".json"


def revoke(refresh_token: str) -> None:
    """尽力通知 Google 撤销授权，失败也不阻塞本地删除。"""
    try:
        requests.post(REVOKE_ENDPOINT, data={"token": refresh_token}, timeout=15)
    except requests.RequestException:
        pass
