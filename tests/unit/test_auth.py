"""
JWT authentication dependency tests.

Uses a locally generated RSA key pair — no real JWKS server needed.
respx intercepts the httpx.get call that _fetch_jwks() makes, returning
our in-process JWKS dict instead.
"""

import base64
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

import auth.dependencies as auth_dep
from auth.dependencies import RequireAnalyst

_TEST_JWKS_URL = "http://test.local/.well-known/jwks.json"
_KID = "test-key-1"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _int_to_b64url(n: int) -> str:
    byte_len = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(byte_len, "big")).rstrip(b"=").decode()


def _make_token(
    private_key,
    workspace_id: str | None,
    role: str = "analyst",
    expired: bool = False,
) -> str:
    now = int(time.time())
    payload: dict = {
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now - 60 if expired else now + 3600,
    }
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    if role:
        payload["role"] = role
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": _KID})


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture(scope="module")
def jwks(rsa_keypair):
    _, pub = rsa_keypair
    nums = pub.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": _int_to_b64url(nums.n),
                "e": _int_to_b64url(nums.e),
            }
        ]
    }


@pytest.fixture(scope="module")
def test_app():
    app = FastAPI()

    @app.get("/protected")
    async def protected(user: RequireAnalyst):
        return {"sub": user.sub, "workspace_id": user.workspace_id, "role": user.role}

    @app.get("/public")
    async def public():
        return {"status": "ok"}

    return app


@pytest.fixture(scope="module")
def client(test_app, jwks):
    mock_resp = MagicMock()
    mock_resp.json.return_value = jwks
    mock_resp.raise_for_status.return_value = None

    original_url = auth_dep._JWKS_URL
    auth_dep._JWKS_URL = _TEST_JWKS_URL
    auth_dep._fetch_jwks.cache_clear()

    # Patch only httpx.get in the auth module — TestClient uses httpx.Client
    # internally and is unaffected by this patch.
    with patch("auth.dependencies.httpx.get", return_value=mock_resp):
        with TestClient(test_app) as c:
            yield c

    auth_dep._JWKS_URL = original_url
    auth_dep._fetch_jwks.cache_clear()


# ── Tests ──────────────────────────────────────────────────────────────────────

WORKSPACE = str(uuid.uuid4())


def test_valid_token_accepted(client, rsa_keypair):
    priv, _ = rsa_keypair
    token = _make_token(priv, WORKSPACE)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_id"] == WORKSPACE
    assert data["role"] == "analyst"


def test_no_auth_header_rejected(client):
    resp = client.get("/protected")
    assert resp.status_code in (401, 403)


def test_expired_token_returns_401(client, rsa_keypair):
    priv, _ = rsa_keypair
    token = _make_token(priv, WORKSPACE, expired=True)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_malformed_token_returns_401(client):
    resp = client.get("/protected", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_token_missing_workspace_id_returns_403(client, rsa_keypair):
    priv, _ = rsa_keypair
    token = _make_token(priv, workspace_id=None)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_public_endpoint_needs_no_auth(client):
    resp = client.get("/public")
    assert resp.status_code == 200
