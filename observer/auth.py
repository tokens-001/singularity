import hmac
import os
from functools import wraps

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials


_TOKEN = os.environ.get("OBSERVER_AUTH_TOKEN", "")

security = HTTPBearer(auto_error=False)


def set_auth_token(token: str) -> None:
    global _TOKEN
    _TOKEN = token


def get_auth_token() -> str:
    return _TOKEN


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_token(credentials: HTTPAuthorizationCredentials | None = None) -> None:
    token = _TOKEN
    if not token:
        return

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not _constant_time_compare(credentials.credentials, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    verify_token(credentials)


def auth_dependency():
    return Depends(require_auth)
