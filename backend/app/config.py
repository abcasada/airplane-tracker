import ipaddress
import secrets
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Docker bridge networks; cloudflared reaches the app from one of these.
DEFAULT_TRUSTED_PROXIES = [
    "127.0.0.1/32",
    "::1/128",
    "172.16.0.0/12",
    "10.0.0.0/8",
    "192.168.0.0/16",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["production", "development"] = "production"
    data_dir: Path = Path("/data")
    public_url: str | None = None
    trusted_proxies: list[str] = DEFAULT_TRUSTED_PROXIES
    auth_mode: Literal["builtin", "proxy"] = "builtin"
    proxy_user_header: str = "Remote-User"
    setup_token: str | None = None
    secret_key: str | None = None
    session_days: int = 30
    log_level: str = "INFO"
    # Background services (pollers, dataset downloads). Tests turn this off.
    run_background: bool = True

    ha_url: str | None = None
    ha_token: str | None = None
    ha_notify_service: str | None = None

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @cached_property
    def trusted_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(p, strict=False) for p in self.trusted_proxies]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "airplane-tracker.sqlite3"

    def resolved_secret_key(self) -> str:
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key)
        path.chmod(0o600)
        return key
