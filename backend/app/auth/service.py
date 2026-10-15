import json
import logging
import secrets
from dataclasses import dataclass

from app.clock import Clock
from app.db import Database

from . import security

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class User:
    id: int
    username: str


@dataclass(frozen=True)
class Session:
    id: str
    user: User
    csrf_token: str
    expires_at: float


class AuthError(Exception):
    pass


class AuthService:
    def __init__(self, db: Database, clock: Clock, session_days: int) -> None:
        self.db = db
        self.clock = clock
        self.session_ttl = session_days * 86400.0
        self.setup_token: str | None = None

    # ---- setup -------------------------------------------------------------------------------

    async def needs_setup(self) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM users WHERE totp_confirmed = 1")
        return row is None

    async def init_setup_token(self, configured: str | None) -> None:
        if not await self.needs_setup():
            self.setup_token = None
            return
        self.setup_token = configured or secrets.token_urlsafe(12)
        if not configured:
            log.warning(
                "First-run setup: open the web UI and enter setup token %s", self.setup_token
            )

    def check_setup_token(self, token: str) -> bool:
        return self.setup_token is not None and secrets.compare_digest(self.setup_token, token)

    async def start_setup(self, username: str, password: str) -> tuple[str, str]:
        """Create (or replace) the unconfirmed account. Returns (totp secret, otpauth uri)."""
        if len(password) < security.MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters")
        username = username.strip()
        if not username:
            raise AuthError("Username is required")
        secret = security.new_totp_secret()
        await self.db.execute("DELETE FROM users WHERE totp_confirmed = 0")
        await self.db.execute(
            "INSERT INTO users(username, pw_hash, totp_secret, created_at) VALUES (?, ?, ?, ?)",
            (username, security.hash_password(password), secret, self.clock.now()),
        )
        return secret, security.totp_uri(secret, username)

    async def confirm_setup(self, code: str) -> tuple[User, list[str]]:
        row = await self.db.fetchone(
            "SELECT id, username, totp_secret, totp_last_step FROM users WHERE totp_confirmed = 0"
        )
        if row is None:
            raise AuthError("Start setup first")
        step = security.verify_totp(
            row["totp_secret"], code, row["totp_last_step"], self.clock.now()
        )
        if step is None:
            raise AuthError("That code didn't match. Check the time on your phone and try again.")
        codes = security.new_recovery_codes()
        await self.db.execute(
            "UPDATE users SET totp_confirmed = 1, totp_last_step = ?, recovery_codes = ? "
            "WHERE id = ?",
            (step, json.dumps([security.hash_token(c) for c in codes]), row["id"]),
        )
        self.setup_token = None
        return User(row["id"], row["username"]), codes

    # ---- login -------------------------------------------------------------------------------

    async def authenticate(self, username: str, password: str, code: str) -> User:
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE username = ? AND totp_confirmed = 1", (username.strip(),)
        )
        if row is None:
            security.burn_password_check(password)
            raise AuthError("Invalid username, password or code")
        if not security.verify_password(row["pw_hash"], password):
            raise AuthError("Invalid username, password or code")
        code = code.strip()
        step = security.verify_totp(
            row["totp_secret"], code, row["totp_last_step"], self.clock.now()
        )
        if step is not None:
            await self.db.execute(
                "UPDATE users SET totp_last_step = ? WHERE id = ?", (step, row["id"])
            )
        elif not await self._consume_recovery_code(row["id"], row["recovery_codes"], code):
            raise AuthError("Invalid username, password or code")
        return User(row["id"], row["username"])

    async def _consume_recovery_code(self, user_id: int, stored: str, code: str) -> bool:
        if len(code) < 10:
            return False
        hashes: list[str] = json.loads(stored)
        h = security.hash_token(code)
        if h not in hashes:
            return False
        hashes.remove(h)
        await self.db.execute(
            "UPDATE users SET recovery_codes = ? WHERE id = ?", (json.dumps(hashes), user_id)
        )
        log.warning("recovery code used; %d remaining", len(hashes))
        return True

    async def change_password(self, user: User, current: str, new: str) -> None:
        row = await self.db.fetchone("SELECT pw_hash FROM users WHERE id = ?", (user.id,))
        if row is None or not security.verify_password(row["pw_hash"], current):
            raise AuthError("Current password is wrong")
        if len(new) < security.MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {security.MIN_PASSWORD_LENGTH} characters")
        await self.db.execute(
            "UPDATE users SET pw_hash = ? WHERE id = ?", (security.hash_password(new), user.id)
        )

    async def regenerate_recovery_codes(self, user: User) -> list[str]:
        codes = security.new_recovery_codes()
        await self.db.execute(
            "UPDATE users SET recovery_codes = ? WHERE id = ?",
            (json.dumps([security.hash_token(c) for c in codes]), user.id),
        )
        return codes

    async def recovery_codes_left(self, user: User) -> int:
        row = await self.db.fetchone("SELECT recovery_codes FROM users WHERE id = ?", (user.id,))
        return len(json.loads(row["recovery_codes"])) if row else 0

    async def single_user(self) -> User | None:
        row = await self.db.fetchone("SELECT id, username FROM users WHERE totp_confirmed = 1")
        return User(row["id"], row["username"]) if row else None

    # ---- sessions ----------------------------------------------------------------------------

    async def create_session(self, user: User, ip: str | None, user_agent: str | None) -> str:
        token = security.new_token()
        now = self.clock.now()
        await self.db.execute(
            "INSERT INTO sessions(id, user_id, csrf_token, created_at, expires_at, last_seen_at, "
            "ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                security.hash_token(token),
                user.id,
                security.new_token(),
                now,
                now + self.session_ttl,
                now,
                ip,
                (user_agent or "")[:300],
            ),
        )
        await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token

    async def get_session(self, token: str) -> Session | None:
        sid = security.hash_token(token)
        row = await self.db.fetchone(
            "SELECT s.id, s.csrf_token, s.expires_at, s.last_seen_at, u.id AS uid, u.username "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            (sid,),
        )
        now = self.clock.now()
        if row is None or row["expires_at"] < now:
            return None
        expires = row["expires_at"]
        # Sliding expiry, written at most hourly.
        if now - row["last_seen_at"] > 3600:
            expires = now + self.session_ttl
            await self.db.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                (now, expires, sid),
            )
        return Session(sid, User(row["uid"], row["username"]), row["csrf_token"], expires)

    async def delete_session(self, session_id: str) -> None:
        await self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def delete_all_sessions(self, user: User) -> None:
        await self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
