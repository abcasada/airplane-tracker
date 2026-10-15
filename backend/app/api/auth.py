from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth import security
from app.auth.deps import (
    SESSION_COOKIE,
    Ctx,
    CurrentSession,
    CurrentUser,
    check_origin_unauthenticated,
    client_ip,
    current_session,
)
from app.auth.service import AuthError, Session
from app.context import AppContext

router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)
    code: str = Field(max_length=20)


class SetupAccountBody(BaseModel):
    setup_token: str = Field(max_length=200)
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)


class SetupConfirmBody(BaseModel):
    setup_token: str = Field(max_length=200)
    code: str = Field(max_length=20)


class PasswordBody(BaseModel):
    current: str = Field(max_length=500)
    new: str = Field(max_length=500)


def _set_cookie(response: Response, ctx: AppContext, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(ctx.auth.session_ttl),
        httponly=True,
        secure=ctx.settings.is_production,
        samesite="lax",
        path="/",
    )


def _throttle_keys(request: Request, ctx: AppContext, username: str) -> list[str]:
    return [f"ip:{client_ip(request, ctx)}", f"user:{username.strip().lower()}"]


@router.get("/setup/status")
async def setup_status(ctx: Ctx) -> dict[str, bool]:
    return {"needs_setup": await ctx.auth.needs_setup()}


@router.post("/setup/account", dependencies=[Depends(check_origin_unauthenticated)])
async def setup_account(body: SetupAccountBody, request: Request, ctx: Ctx) -> dict[str, str]:
    keys = [f"setup:{client_ip(request, ctx)}"]
    now = ctx.clock.now()
    if wait := ctx.throttle.retry_after(keys, now):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Try again in {int(wait) + 1} s")
    if not await ctx.auth.needs_setup():
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup is already complete")
    if not ctx.auth.check_setup_token(body.setup_token):
        ctx.throttle.failure(keys, now)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong setup token (see the container log)")
    try:
        secret, uri = await ctx.auth.start_setup(body.username, body.password)
    except AuthError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": security.totp_qr_svg(uri)}


@router.post("/setup/confirm", dependencies=[Depends(check_origin_unauthenticated)])
async def setup_confirm(
    body: SetupConfirmBody, request: Request, response: Response, ctx: Ctx
) -> dict[str, list[str]]:
    if not await ctx.auth.needs_setup():
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup is already complete")
    if not ctx.auth.check_setup_token(body.setup_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong setup token")
    try:
        user, codes = await ctx.auth.confirm_setup(body.code)
    except AuthError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    token = await ctx.auth.create_session(
        user, client_ip(request, ctx), request.headers.get("user-agent")
    )
    _set_cookie(response, ctx, token)
    await ctx.db.audit(ctx.clock.now(), user.username, "setup.complete")
    return {"recovery_codes": codes}


@router.post("/auth/login", dependencies=[Depends(check_origin_unauthenticated)])
async def login(body: LoginBody, request: Request, response: Response, ctx: Ctx) -> dict[str, str]:
    keys = _throttle_keys(request, ctx, body.username)
    now = ctx.clock.now()
    if wait := ctx.throttle.retry_after(keys, now):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many attempts. Try again in {int(wait) + 1} s",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    ip = client_ip(request, ctx)
    try:
        user = await ctx.auth.authenticate(body.username, body.password, body.code)
    except AuthError as e:
        ctx.throttle.failure(keys, now)
        await ctx.db.audit(now, body.username[:100], "login.failed", {"ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    ctx.throttle.success(keys)
    token = await ctx.auth.create_session(user, ip, request.headers.get("user-agent"))
    _set_cookie(response, ctx, token)
    await ctx.db.audit(now, user.username, "login", {"ip": ip})
    return {"username": user.username}


@router.post("/auth/logout")
async def logout(session: CurrentSession, response: Response, ctx: Ctx) -> dict[str, bool]:
    await ctx.auth.delete_session(session.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(session: Annotated[Session, Depends(current_session)], ctx: Ctx) -> dict[str, object]:
    return {
        "username": session.user.username,
        "csrf_token": session.csrf_token,
        "recovery_codes_left": await ctx.auth.recovery_codes_left(session.user),
        "auth_mode": ctx.settings.auth_mode,
    }


@router.post("/auth/password")
async def change_password(body: PasswordBody, user: CurrentUser, ctx: Ctx) -> dict[str, bool]:
    try:
        await ctx.auth.change_password(user, body.current, body.new)
    except AuthError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await ctx.db.audit(ctx.clock.now(), user.username, "password.changed")
    return {"ok": True}


@router.post("/auth/recovery-codes")
async def new_recovery_codes(user: CurrentUser, ctx: Ctx) -> dict[str, list[str]]:
    codes = await ctx.auth.regenerate_recovery_codes(user)
    await ctx.db.audit(ctx.clock.now(), user.username, "recovery_codes.regenerated")
    return {"recovery_codes": codes}


@router.post("/auth/logout-everywhere")
async def logout_everywhere(user: CurrentUser, response: Response, ctx: Ctx) -> dict[str, bool]:
    await ctx.auth.delete_all_sessions(user)
    response.delete_cookie(SESSION_COOKIE, path="/")
    await ctx.db.audit(ctx.clock.now(), user.username, "sessions.revoked")
    return {"ok": True}
