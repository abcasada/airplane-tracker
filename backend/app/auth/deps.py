import hashlib
import hmac
import ipaddress
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from app.context import AppContext

from .service import Session, User

SESSION_COOKIE = "at_session"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _is_trusted(ctx: AppContext, host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in ctx.settings.trusted_networks)


def client_ip(request: Request, ctx: AppContext) -> str | None:
    peer = request.client.host if request.client else None
    if _is_trusted(ctx, peer):
        forwarded = (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        )
        if forwarded:
            return forwarded
    return peer


def _check_origin(request: Request, ctx: AppContext) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_host = urlsplit(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower()}
    if ctx.settings.public_url:
        allowed.add(urlsplit(ctx.settings.public_url).netloc.lower())
    if origin_host not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")


def proxy_csrf_token(ctx: AppContext, username: str) -> str:
    return hmac.new(ctx.secret_key.encode(), username.encode(), hashlib.sha256).hexdigest()


async def current_session(request: Request, ctx: Ctx) -> Session:
    if ctx.settings.auth_mode == "proxy":
        peer = request.client.host if request.client else None
        name = request.headers.get(ctx.settings.proxy_user_header)
        user = await ctx.auth.single_user()
        if name and user and _is_trusted(ctx, peer):
            return Session("proxy", user, proxy_csrf_token(ctx, user.username), float("inf"))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    token = request.cookies.get(SESSION_COOKIE)
    session = await ctx.auth.get_session(token) if token else None
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return session


async def require_session(
    request: Request, ctx: Ctx, session: Annotated[Session, Depends(current_session)]
) -> Session:
    if request.method in UNSAFE_METHODS:
        _check_origin(request, ctx)
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
    return session


async def require_user(session: Annotated[Session, Depends(require_session)]) -> User:
    return session.user


CurrentUser = Annotated[User, Depends(require_user)]
CurrentSession = Annotated[Session, Depends(require_session)]


def check_origin_unauthenticated(request: Request, ctx: Ctx) -> None:
    """Login and setup have no session yet; the Origin check still blocks cross-site posts."""
    _check_origin(request, ctx)
