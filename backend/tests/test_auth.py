import httpx

from tests.conftest import PASSWORD, USERNAME, Harness


async def test_health_is_public(harness: Harness) -> None:
    r = await harness.client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_api_requires_session(harness: Harness) -> None:
    r = await harness.client.get("/api/places")
    assert r.status_code == 401


async def test_setup_requires_token(harness: Harness) -> None:
    r = await harness.client.post(
        "/api/setup/account",
        json={"setup_token": "wrong", "username": USERNAME, "password": PASSWORD},
    )
    assert r.status_code == 403


async def test_setup_rejects_short_password(harness: Harness) -> None:
    r = await harness.client.post(
        "/api/setup/account",
        json={"setup_token": harness.ctx.auth.setup_token, "username": "a", "password": "short"},
    )
    assert r.status_code == 400


async def test_setup_flow_and_lockout_of_setup(authed: Harness) -> None:
    status = await authed.client.get("/api/setup/status")
    assert status.json() == {"needs_setup": False}
    r = await authed.client.post(
        "/api/setup/account",
        json={"setup_token": "anything", "username": "x", "password": PASSWORD},
    )
    assert r.status_code == 409


async def _fresh_client(h: Harness) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://testserver")


async def test_login_with_totp(authed: Harness) -> None:
    authed.clock.advance(31)
    async with await _fresh_client(authed) as c:
        r = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD, "code": authed.code()},
        )
        assert r.status_code == 200, r.text
        assert "at_session" in r.cookies
        assert (await c.get("/api/places")).status_code == 200


async def test_totp_code_cannot_be_replayed(authed: Harness) -> None:
    authed.clock.advance(31)
    code = authed.code()
    async with await _fresh_client(authed) as c:
        body = {"username": USERNAME, "password": PASSWORD, "code": code}
        assert (await c.post("/api/auth/login", json=body)).status_code == 200
        assert (await c.post("/api/auth/login", json=body)).status_code == 401


async def test_wrong_password_and_lockout(authed: Harness) -> None:
    async with await _fresh_client(authed) as c:
        for _ in range(4):
            r = await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "nope-nope-nope", "code": "000000"},
            )
            assert r.status_code == 401
        r = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD, "code": authed.code()},
        )
        assert r.status_code == 429
        authed.clock.advance(3600)
        r = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD, "code": authed.code()},
        )
        assert r.status_code == 200


async def test_recovery_code_works_once(authed: Harness) -> None:
    authed.clock.advance(31)
    codes = await authed.client.post("/api/auth/recovery-codes")
    code = codes.json()["recovery_codes"][0]
    async with await _fresh_client(authed) as c:
        body = {"username": USERNAME, "password": PASSWORD, "code": code}
        assert (await c.post("/api/auth/login", json=body)).status_code == 200
    async with await _fresh_client(authed) as c:
        assert (await c.post("/api/auth/login", json=body)).status_code == 401


async def test_csrf_required_for_writes(authed: Harness) -> None:
    del authed.client.headers["X-CSRF-Token"]
    r = await authed.client.post(
        "/api/places", json={"name": "Home", "lat": 1, "lon": 2, "radius_nm": 5}
    )
    assert r.status_code == 403


async def test_cross_origin_write_rejected(authed: Harness) -> None:
    r = await authed.client.post(
        "/api/places",
        json={"name": "Home", "lat": 1, "lon": 2, "radius_nm": 5},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_session_expires(authed: Harness) -> None:
    authed.clock.advance(31 * 86400)
    assert (await authed.client.get("/api/places")).status_code == 401


async def test_logout(authed: Harness) -> None:
    assert (await authed.client.post("/api/auth/logout")).status_code == 200
    assert (await authed.client.get("/api/places")).status_code == 401


async def test_security_headers(harness: Harness) -> None:
    r = await harness.client.get("/healthz")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


async def test_unknown_api_route_is_json_404(harness: Harness) -> None:
    r = await harness.client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not found"
