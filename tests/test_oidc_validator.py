from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response

from ai_agent.errors import AuthenticationError
from ai_agent.oauth.models import OidcProviderMetadata
from ai_agent.oauth.validator import OidcIdTokenValidator

ISSUER = "https://id.example.test"
CLIENT_ID = "agent-client"
NONCE = "transaction-nonce"
JWKS_URI = f"{ISSUER}/jwks"


def _metadata(*, jwks_uri: str | None = JWKS_URI) -> OidcProviderMetadata:
    return OidcProviderMetadata(
        issuer=ISSUER,
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=jwks_uri,
    )


def _key_material() -> tuple[ rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "key-1"
    return private_key, public_jwk


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "external-user-1",
        "nonce": NONCE,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


def _encode(
    private_key: rsa.RSAPrivateKey,
    *,
    algorithm: str = "RS256",
    kid: str | None = "key-1",
    claims: dict[str, object] | None = None,
) -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(
        _claims() if claims is None else claims,
        private_key,
        algorithm=algorithm,
        headers=headers,
    )


def _validator() -> OidcIdTokenValidator:
    return OidcIdTokenValidator(signing_algorithms=("RS256",))


async def _validate(
    encoded: str,
    metadata: OidcProviderMetadata,
    *,
    expected_nonce: str = NONCE,
) -> dict[str, object]:
    return await _validator().validate(
        encoded,
        metadata,
        client_id=CLIENT_ID,
        expected_nonce=expected_nonce,
    )


@respx.mock
async def test_valid_rsa_id_token_and_jwks_are_accepted() -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))

    claims = await _validate(_encode(private_key), _metadata())

    assert claims["sub"] == "external-user-1"


@respx.mock
async def test_id_token_signed_by_another_rsa_key_is_rejected() -> None:
    _, trusted_jwk = _key_material()
    untrusted_key, _ = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [trusted_jwk]}))

    with pytest.raises(AuthenticationError):
        await _validate(_encode(untrusted_key), _metadata())


@respx.mock
async def test_unknown_kid_is_rejected() -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key, kid="unknown-key"), _metadata())


async def test_untrusted_hs256_algorithm_is_rejected_before_jwks_fetch() -> None:
    token = jwt.encode(
        _claims(),
        "not-a-trusted-rsa-key-with-enough-length-0123456789",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(AuthenticationError):
        await _validate(token, _metadata())


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"exp": None},
        {"iat": None},
    ],
)
@respx.mock
async def test_expiration_and_issue_time_claims_are_required_and_valid(
    claim_overrides: dict[str, object],
) -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key, claims=_claims(**claim_overrides)), _metadata())


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://another-id.example.test"},
        {"aud": "another-client"},
        {"nonce": "another-nonce"},
    ],
)
@respx.mock
async def test_issuer_audience_and_nonce_must_match(
    claim_overrides: dict[str, object],
) -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key, claims=_claims(**claim_overrides)), _metadata())


@pytest.mark.parametrize(
    "audience, azp",
    [
        ([CLIENT_ID, "other-client"], None),
        ([CLIENT_ID, "other-client"], "another-client"),
        (CLIENT_ID, "another-client"),
    ],
)
@respx.mock
async def test_authorized_party_must_match_for_multiple_or_present_audience(
    audience: str | list[str],
    azp: str | None,
) -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))
    claims = _claims(aud=audience)
    if azp is not None:
        claims["azp"] = azp

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key, claims=claims), _metadata())


@respx.mock
async def test_matching_authorized_party_is_accepted_for_multiple_audience() -> None:
    private_key, public_jwk = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(200, json={"keys": [public_jwk]}))

    claims = await _validate(
        _encode(
            private_key,
            claims=_claims(aud=[CLIENT_ID, "other-client"], azp=CLIENT_ID),
        ),
        _metadata(),
    )

    assert claims["aud"] == [CLIENT_ID, "other-client"]


async def test_jwks_uri_is_required_for_id_token_validation() -> None:
    private_key, _ = _key_material()

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key), _metadata(jwks_uri=None))


@respx.mock
async def test_jwks_http_failure_is_converted_to_authentication_error() -> None:
    private_key, _ = _key_material()
    respx.get(JWKS_URI).mock(return_value=Response(503))

    with pytest.raises(AuthenticationError):
        await _validate(_encode(private_key), _metadata())
