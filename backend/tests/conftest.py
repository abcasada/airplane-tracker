from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyotp
import pytest
from fastapi import FastAPI

from app.clock import FakeClock
from app.config import Settings
from app.context import AppContext
from app.main import create_app

USERNAME = "pilot"
PASSWORD = "correct horse battery"


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    clock: FakeClock
    ctx: AppContext
    totp_secret: str = ""
    csrf: str = ""

    def code(self) -> str:
        return pyotp.TOTP(self.totp_secret).at(int(self.clock.now()))

    async def setup_account(self) -> list[str]:
        token = self.ctx.auth.setup_token
        r = await self.client.post(
            "/api/setup/account",
            json={"setup_token": token, "username": USERNAME, "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
        self.totp_secret = r.json()["secret"]
        r = await self.client.post(
            "/api/setup/confirm", json={"setup_token": token, "code": self.code()}
        )
        assert r.status_code == 200, r.text
        await self.refresh_csrf()
        codes: list[str] = r.json()["recovery_codes"]
        return codes

    async def refresh_csrf(self) -> None:
        me = await self.client.get("/api/auth/me")
        assert me.status_code == 200, me.text
        self.csrf = me.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf


def make_settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(
        _env_file=None,
        env="development",
        data_dir=tmp_path,
        run_background=False,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    clock = FakeClock()
    app = create_app(make_settings(tmp_path), clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield Harness(app, client, clock, app.state.ctx)


@pytest.fixture
async def authed(harness: Harness) -> Harness:
    await harness.setup_account()
    return harness
