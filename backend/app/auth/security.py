import hashlib
import hmac
import secrets

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
TOTP_ISSUER = "Airplane Tracker"
MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(pw_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Verifying against a dummy hash keeps login timing the same for unknown usernames.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def totp_qr_svg(uri: str) -> str:
    qr = segno.make(uri, error="m")
    svg: str = qr.svg_inline(scale=5, dark="#000", light="#fff")
    return svg


def verify_totp(secret: str, code: str, last_step: int, now: float) -> int | None:
    """Return the matched time step, or None. Rejects reuse of an already-used step."""
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    current = int(now // totp.interval)
    for step in (current - 1, current, current + 1):
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_token(token: str) -> str:
    # Session tokens and recovery codes are high-entropy, so a fast hash is enough.
    return hashlib.sha256(token.strip().upper().encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)
