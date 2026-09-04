from __future__ import annotations

import base64
import hashlib

from ai_agent.oauth.pkce import create_nonce, create_pkce_pair, create_state


def test_pkce_pair_uses_s256_and_valid_verifier_length() -> None:
    pair = create_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pair.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    assert 43 <= len(pair.code_verifier) <= 128
    assert pair.code_challenge == expected


def test_state_and_nonce_are_independent() -> None:
    state = create_state()
    nonce = create_nonce()

    assert state
    assert nonce
    assert state != nonce
