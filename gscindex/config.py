"""配置加载与保存。

配置项分两组，对应两条性质不同的链路：
* inspect_* / concurrency / request_timeout —— Google 官方 API（查站点、查收录），配额充裕
* webauto_*                                —— 自动化操作 GSC 网页点"请求编入索引"，名额稀缺
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "site_url": "",
    "service_account_dir": "accounts",
    # 收录预检（URL Inspection API），Google 给每个凭据每天 2000 次
    "inspect_daily_quota": 2000,
    "concurrency": 4,
    "resubmit_after_days": 14,
    "request_timeout": 45,
    "max_retries": 4,
    "port": 8765,
    # 全站扫描单次最多处理多少条 URL，0 表示不限制
    "scan_limit": 0,
    # ---- 网页自动化（点 GSC 网页上的"请求编入索引"）----
    # 这个上限只是本地的保守闸门。Google 那边真实的每日上限是多少并不清楚，
    # 官方从未公开，实测也无法排除站点所有者手动提交对名额的消耗。
    # 真正的天花板以 Google 返回"超出了配额"为准，撞到就停。
    "webauto_daily_limit": 8,
    "webauto_min_delay": 4,
    "webauto_max_delay": 9,
    # 无窗口模式更快，但更容易被识别为自动化，默认关闭
    "webauto_headless": False,
}


@dataclass
class Config:
    site_url: str = ""
    service_account_dir: str = "accounts"
    inspect_daily_quota: int = 2000
    concurrency: int = 4
    resubmit_after_days: int = 14
    request_timeout: int = 45
    max_retries: int = 4
    port: int = 8765
    scan_limit: int = 0
    webauto_daily_limit: int = 8
    webauto_min_delay: int = 4
    webauto_max_delay: int = 9
    webauto_headless: bool = False

    @property
    def accounts_path(self) -> Path:
        p = Path(self.service_account_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def db_path(self) -> Path:
        return ROOT / "data" / "index.db"

    @property
    def webauto_session_path(self) -> Path:
        """浏览器登录状态的落脚点。

        webauto.profile_dir() 取的是这个路径的父目录下的 browser_profile/，
        所以这里给一个 accounts/ 下的占位路径即可——真正的登录数据在
        accounts/browser_profile/ 整个目录里，比 OAuth token 更敏感。
        """
        return self.accounts_path / "webauto_session.json"

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, patch: dict) -> None:
        for k, v in patch.items():
            if k in DEFAULTS:
                setattr(self, k, type(DEFAULTS[k])(v) if v is not None else DEFAULTS[k])

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path else ROOT / "config.json"
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )


def load(path: Path | None = None) -> Config:
    """读取 config.json，缺失字段用默认值补齐；文件不存在时返回全默认配置。

    未知字段（比如已移除的 daily_quota_per_account、batch_size）会被静默忽略，
    这样老的 config.json 不会因为多了几个字段就报错。
    """
    target = Path(path) if path else ROOT / "config.json"
    raw = dict(DEFAULTS)
    if target.exists():
        try:
            raw.update(json.loads(target.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return Config(**{k: v for k, v in raw.items() if k in DEFAULTS})
