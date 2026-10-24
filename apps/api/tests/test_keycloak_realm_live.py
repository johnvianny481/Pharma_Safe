"""Checks the local Keycloak realm issues tokens the API and the signature service accept.

Runs only when the local stack is up (``make up``); skipped otherwise, including in CI.
Logs in exactly as the review UI does: authorization code + PKCE with the
``pharmasafe-ui`` client, scripted over HTTP instead of a browser.
"""

import base64
import hashlib
import html
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, text

from pharmasafe_platform.auth import Role, TokenVerifier, jwks_key_resolver
from pharmasafe_platform.db import Database
from pharmasafe_platform.signatures import SignableRecord, SignatureMeaning, SignatureService

ROOT = Path(__file__).resolve().parents[3]
ACME = UUID("11111111-1111-4111-8111-111111111111")
REDIRECT_URI = "http://localhost:5173/callback"


class _LiveEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    kc_demo_user_password: str | None = None
    pharmasafe_oidc_issuer: str = "http://localhost:8180/realms/pharmasafe"


@pytest.fixture(scope="module")
def live() -> _LiveEnv:
    env = _LiveEnv()
    if not env.kc_demo_user_password:
        pytest.skip("KC_DEMO_USER_PASSWORD not set")
    try:
        httpx.get(f"{env.pharmasafe_oidc_issuer}/.well-known/openid-configuration", timeout=3)
    except httpx.HTTPError:
        pytest.skip("local Keycloak is not running")
    return env


@pytest.fixture(scope="module")
def live_verifier(live: _LiveEnv) -> TokenVerifier:
    issuer = live.pharmasafe_oidc_issuer
    return TokenVerifier(
        issuer, "pharmasafe-api", jwks_key_resolver(f"{issuer}/protocol/openid-connect/certs")
    )


def _login(env: _LiveEnv, username: str) -> str:
    """Authorization code + PKCE with prompt=login (a full, fresh authentication)."""
    issuer = env.pharmasafe_oidc_issuer
    code_verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        page = client.get(
            f"{issuer}/protocol/openid-connect/auth",
            params={
                "client_id": "pharmasafe-ui",
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": secrets.token_urlsafe(8),
                "prompt": "login",
            },
        )
        match = re.search(r'action="([^"]+)"', page.text)
        assert match, "no login form"
        # Keycloak marks its cookies Secure; browsers send them to http://localhost, httpx doesn't.
        cookies = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
        login = client.post(
            html.unescape(match.group(1)),
            headers={"Cookie": cookies},
            data={"username": username, "password": env.kc_demo_user_password},
        )
        assert login.status_code == 302, login.text[:500]
        code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
        tokens = client.post(
            f"{issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "pharmasafe-ui",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
    assert tokens.status_code == 200, tokens.text
    return str(tokens.json()["access_token"])


def test_realm_token_carries_tenant_roles_and_reauth_claims(
    live: _LiveEnv, live_verifier: TokenVerifier
) -> None:
    principal = live_verifier.verify(_login(live, "acme.processor"))
    assert principal.tenant_id == ACME
    assert principal.roles == {Role.CASE_PROCESSOR}
    assert principal.scope.products is None
    assert principal.auth_time is not None
    assert principal.token_id is not None


def test_realm_scoped_user(live: _LiveEnv, live_verifier: TokenVerifier) -> None:
    principal = live_verifier.verify(_login(live, "acme.eu-only"))
    assert principal.scope.permits("ACM-001", "eu")
    assert not principal.scope.permits("ACM-001", "us")


def test_realm_platform_admin_has_no_tenant(live: _LiveEnv, live_verifier: TokenVerifier) -> None:
    principal = live_verifier.verify(_login(live, "platform.admin"))
    assert principal.tenant_id is None
    assert principal.roles == {Role.PLATFORM_ADMIN}


def test_relogin_is_accepted_as_signing_reauthentication(
    live: _LiveEnv, live_verifier: TokenVerifier, db: Database, owner_engine: Engine
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, region) VALUES (:id, 'acme-pharma', 'Acme', 'eu')"
                " ON CONFLICT (id) DO NOTHING"
            ),
            {"id": ACME},
        )
    session_principal = live_verifier.verify(_login(live, "acme.processor"))
    reauth = live_verifier.verify(_login(live, "acme.processor"))
    record = SignableRecord("case", f"live-{secrets.token_hex(4)}", 1, "a" * 64)
    service = SignatureService(max_auth_age=timedelta(minutes=5))
    with db.tenant_session(ACME) as session:
        signature = service.sign(
            session,
            signer=session_principal,
            reauth=reauth,
            record=record,
            meaning=SignatureMeaning.REVIEWED,
        )
    assert service.verify(signature, record).valid
