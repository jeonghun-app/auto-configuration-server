"""Provisioning token issuance and validation."""

from __future__ import annotations

from tests.conftest import TEST_IMEI, TEST_IMSI

from acs.auth import token as token_mod
from acs.store.memory import MemoryStore


def test_generated_tokens_are_unique_and_long() -> None:
    tokens = {token_mod.generate_token() for _ in range(64)}
    assert len(tokens) == 64
    assert all(len(t) >= 40 for t in tokens)


def test_only_the_digest_is_persisted(store: MemoryStore) -> None:
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600)
    record = store.get_token(token_mod.hash_token(token))
    assert record is not None
    assert token not in record.token_hash


def test_valid_token_resolves(store: MemoryStore) -> None:
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600)
    record = token_mod.verify_token(store, token, TEST_IMEI)
    assert record is not None
    assert record.imsi == TEST_IMSI


def test_unknown_token_is_rejected(store: MemoryStore) -> None:
    assert token_mod.verify_token(store, "nonsense", TEST_IMEI) is None


def test_empty_token_is_rejected(store: MemoryStore) -> None:
    assert token_mod.verify_token(store, "", TEST_IMEI) is None


def test_token_bound_to_another_imei_is_rejected(store: MemoryStore) -> None:
    # Lifting a token onto a different handset must not work.
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600)
    assert token_mod.verify_token(store, token, "356938035643800") is None


def test_binding_can_be_disabled(store: MemoryStore) -> None:
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600, bind_imei=False)
    assert token_mod.verify_token(store, token, "other", bind_imei=False) is not None


def test_expired_token_is_rejected(store: MemoryStore) -> None:
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, ttl_seconds=-1)
    assert token_mod.verify_token(store, token, TEST_IMEI) is None


def test_revoked_token_is_rejected(store: MemoryStore) -> None:
    token = token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600)
    store.revoke_token(token_mod.hash_token(token))
    assert token_mod.verify_token(store, token, TEST_IMEI) is None


def test_revoking_by_imsi_invalidates_every_token(store: MemoryStore) -> None:
    tokens = [token_mod.issue_token(store, TEST_IMSI, TEST_IMEI, 3600) for _ in range(3)]
    assert store.revoke_tokens_for_imsi(TEST_IMSI) == 3
    assert all(token_mod.verify_token(store, t, TEST_IMEI) is None for t in tokens)
    # Idempotent: already-revoked tokens are not counted again.
    assert store.revoke_tokens_for_imsi(TEST_IMSI) == 0
