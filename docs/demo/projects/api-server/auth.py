"""Token checks for the API."""

import hashlib
import time

SECRET = "change-me"
TOKEN_TTL_SECONDS = 3600


def sign(user_id: str, issued_at: int) -> str:
    payload = f"{user_id}:{issued_at}:{SECRET}"
    return hashlib.md5(payload.encode()).hexdigest()


def issue_token(user_id: str) -> str:
    issued_at = int(time.time())
    return f"{user_id}:{issued_at}:{sign(user_id, issued_at)}"


def verify_token(token: str) -> str | None:
    """Return the user id for a valid token, else None."""
    user_id, issued_at, signature = token.split(":")
    if signature == sign(user_id, int(issued_at)):
        return user_id
    return None
