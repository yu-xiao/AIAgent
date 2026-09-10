"""Shared OIDC ID token signature and claims validation."""

from __future__ import annotations

import json
import secrets
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from ai_agent.errors import AuthenticationError
from ai_agent.oauth.models import OidcProviderMetadata


class OidcIdTokenValidator:
    def __init__(
        self,
        *,
        signing_algorithms: tuple[str, ...],
        timeout_seconds: float = 10.0,
    ) -> None:
        self._signing_algorithms = signing_algorithms
        self._timeout_seconds = timeout_seconds

    async def validate(
        self,
        encoded: str,
        metadata: OidcProviderMetadata,
        *,
        client_id: str,
        expected_nonce: str,
    ) -> dict[str, Any]:
        if not metadata.issuer.strip() or not metadata.jwks_uri:
            raise AuthenticationError("OIDC ID token cannot be verified with provider metadata.")

        try:
            header = jwt.get_unverified_header(encoded)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if not isinstance(algorithm, str) or algorithm not in self._signing_algorithms:
                raise AuthenticationError("OIDC ID token uses an untrusted signing algorithm.")
            if not isinstance(key_id, str) or not key_id.strip():
                raise AuthenticationError("OIDC ID token uses an invalid signing key.")

            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    metadata.jwks_uri,
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise AuthenticationError("OIDC signing keys response is invalid.")
            keys = payload.get("keys")
            if not isinstance(keys, list):
                raise AuthenticationError("OIDC signing keys response is invalid.")
            key_data = next(
                (
                    item
                    for item in keys
                    if isinstance(item, dict) and item.get("kid") == key_id
                ),
                None,
            )
            if key_data is None:
                raise AuthenticationError("OIDC signing key was not found.")

            key = PyJWK.from_dict(key_data, algorithm=algorithm).key
            claims = jwt.decode(
                encoded,
                key=key,
                algorithms=list(self._signing_algorithms),
                audience=client_id,
                issuer=metadata.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
            if not isinstance(claims, dict):
                raise AuthenticationError("OIDC ID token claims are invalid.")

            audience = claims.get("aud")
            if isinstance(audience, list) and len(audience) > 1:
                if claims.get("azp") != client_id:
                    raise AuthenticationError("OIDC authorized party validation failed.")
            if "azp" in claims and claims.get("azp") != client_id:
                raise AuthenticationError("OIDC authorized party validation failed.")

            nonce = claims.get("nonce")
            if not isinstance(nonce, str) or not secrets.compare_digest(
                nonce.encode("utf-8"), expected_nonce.encode("utf-8")
            ):
                raise AuthenticationError("OIDC nonce validation failed.")
            return dict(claims)
        except AuthenticationError:
            raise
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            jwt.PyJWTError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise AuthenticationError("OIDC ID token validation failed.") from exc
