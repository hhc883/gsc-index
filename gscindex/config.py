"""配置加载与保存。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "site_url": "",
    "service_account_dir": "accounts",
    "daily_quota_per_account": 200,
    "inspect_daily_quota": 2000,
    "concurrency": 4,
    "batch_size": 100,
    "inspect_before_submit": True,
    "resubmit_after_days": 14,
    "request_timeout": 45,
    "max_retries": 4,
    "port": 8765,
}


@dataclass
class Config:
    site_url: str = ""
    service_account_dir: str = "accounts"
    daily_quota_per_account: int = 200
    inspect_daily_quota: int = 2000
    concurrency: int = 4
    batch_size: int = 100
    inspect_before_submit: bool = True
    resubmit_after_days: int = 14
    request_timeout: int = 45
    max_retries: int = 4
    port: int = 8765

    @property
    def accounts_path(self) -> Path:
        p = Path(self.service_account_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def db_path(self) -> Path:
        return ROOT / "data" / "index.db"

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
    """读取 config.json，缺失字段用默认值补齐；文件不存在时返回全默认配置。"""
    target = Path(path) if path else ROOT / "config.json"
    raw = dict(DEFAULTS)
    if target.exists():
        try:
            raw.update(json.loads(target.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return Config(**{k: v for k, v in raw.items() if k in DEFAULTS})
