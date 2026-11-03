"""OIDC bearer-token verification for secured AI Operations deployments.

Identity and tenant claims must come from a trusted identity broker; never from
client-supplied event JSON or unverified JWT headers.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient


_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_ALLOWED_SCOPES = frozenset({
    "aiops:read", "aiops:ingest", "aiops:execute",
    "aiops:replay", "aiops:audit", "aiops:admin",
})


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    scopes: frozenset[str]
    team: str = "unknown"
    application: str = "unknown"

    def permitted(self, required: str) -> bool:
        return required in self.scopes or "aiops:admin" in self.scopes


class OIDCVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str):
        if not (issuer.startswith("https://") and audience and
                jwks_url.startswith("https://")):
            raise RuntimeError(
                "Secure mode requires a trusted HTTPS OIDC issuer, JWKS URL and audience"
            )
        self.issuer = issuer
        self.audience = audience
        # PyJWT 2.14+ fixes a JWKS redirect vulnerability. Keep remote key
        # caching bounded; do not trust jku or x5u in incoming JWT headers.
        self.keys = PyJWKClient(jwks_url, timeout=5, cache_jwk_set=True)

    def verify(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization[len("Bearer "):].strip()
        if not token or len(token) > 16384:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=30,
            )
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

        tenant = claims.get("tenant_id")
        subject = claims.get("sub")
        scope = claims.get("scope", "")
        if (not isinstance(tenant, str) or not _TENANT_ID.fullmatch(tenant) or
                not isinstance(subject, str) or not subject.strip() or
                not isinstance(scope, str)):
            raise HTTPException(status_code=401, detail="Missing trusted identity claims")
        scopes = frozenset(scope.split()) & _ALLOWED_SCOPES
        team = claims.get("team", "unknown")
        application = claims.get("application", "unknown")
        if not isinstance(team, str) or not team or len(team) > 128:
            team = "unknown"
        if not isinstance(application, str) or not application or len(application) > 128:
            application = "unknown"
        return Principal(
            tenant_id=tenant, subject=subject, scopes=scopes,
            team=team, application=application,
        )
