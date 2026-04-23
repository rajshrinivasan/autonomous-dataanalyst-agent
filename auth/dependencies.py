"""JWT verification for FastAPI routes.

Validates RS256 tokens issued by Clerk (or any JWKS-compatible provider).
The token must carry custom claims:
  - workspace_id: UUID string identifying the user's active workspace
  - role: one of admin | analyst | viewer  (defaults to "analyst" if absent)
"""

import os
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

_JWKS_URL = os.getenv("JWT_JWKS_URL", "")
_AUDIENCE = os.getenv("JWT_AUDIENCE", "")
_ALGORITHM = "RS256"

# When DEV_AUTH_BYPASS=true, skip JWT validation and return a fixed dev identity.
# Safe only for local development — never set this in production.
_DEV_BYPASS = os.getenv("DEV_AUTH_BYPASS", "").lower() in ("1", "true", "yes")
_DEV_WORKSPACE_ID = os.getenv("DEV_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")

_bearer = HTTPBearer(auto_error=not _DEV_BYPASS)


class TokenData(BaseModel):
    sub: str
    workspace_id: str
    role: str


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Cached JWKS fetch — cache survives for the process lifetime."""
    if not _JWKS_URL:
        raise RuntimeError("JWT_JWKS_URL is not set")
    resp = httpx.get(_JWKS_URL, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


def _pick_key(jwks: dict, kid: str | None) -> dict:
    """Return the matching JWK, falling back to the first key when kid is absent."""
    keys = jwks.get("keys", [])
    if not keys:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="JWKS has no keys")
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
    return keys[0]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenData:
    if _DEV_BYPASS:
        return TokenData(sub="dev-user", workspace_id=_DEV_WORKSPACE_ID, role="analyst")

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    exc_401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        header = jwt.get_unverified_header(token)
        jwks = _fetch_jwks()
        key = _pick_key(jwks, header.get("kid"))
        payload = jwt.decode(
            token,
            key,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE or None,
        )
    except (JWTError, RuntimeError, httpx.HTTPError) as exc:
        raise exc_401 from exc

    workspace_id = payload.get("workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is missing workspace_id claim",
        )

    return TokenData(
        sub=payload["sub"],
        workspace_id=str(workspace_id),
        role=payload.get("role", "analyst"),
    )


RequireAnalyst = Annotated[TokenData, Depends(get_current_user)]
