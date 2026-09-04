"""PKCE, state and nonce generation for authorization code flows."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PkcePair:
    code_verifier: str
    code_challenge: str


def create_pkce_pair() -> PkcePair:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(code_verifier=verifier, code_challenge=challenge)


def create_state() -> str:
    return secrets.token_urlsafe(32)


def create_nonce() -> str:
    return secrets.token_urlsafe(32)
