from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.auth.service import AuthService
from app.auth.throttle import LoginThrottle
from app.clock import Clock
from app.config import Settings
from app.db import Database

if TYPE_CHECKING:
    from app.runtime import Runtime


@dataclass
class AppContext:
    settings: Settings
    db: Database
    clock: Clock
    auth: AuthService
    throttle: LoginThrottle
    http: httpx.AsyncClient
    secret_key: str
    runtime: Runtime | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def rt(self) -> Runtime:
        if self.runtime is None:
            raise RuntimeError("runtime not started")
        return self.runtime
