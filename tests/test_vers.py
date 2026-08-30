"""Configuration version semantics."""

from __future__ import annotations

import pytest

from acs.protocol import vers as vers_mod
from acs.protocol.vers import VersAction


@pytest.mark.spec
@pytest.mark.parametrize(
    ("version", "action"),
    [
        (1, VersAction.APPLY),
        (99, VersAction.APPLY),
        (0, VersAction.DISABLE_KEEP),
        (-1, VersAction.DISABLE_DELETE_RETRY),
        (-2, VersAction.DISABLE_DELETE_NO_RETRY),
        (-3, VersAction.DORMANT),
        (-4, VersAction.BLOCKED),
    ],
)
def test_every_documented_version_maps_to_its_action(version: int, action: VersAction) -> None:
    assert vers_mod.rule_for(version).action is action


def test_unknown_negative_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported configuration version"):
        vers_mod.rule_for(-9)


@pytest.mark.spec
def test_deleting_versions_are_the_ones_that_wipe_configuration() -> None:
    assert vers_mod.rule_for(-1).delete_config is True
    assert vers_mod.rule_for(-2).delete_config is True
    assert vers_mod.rule_for(0).delete_config is False
    assert vers_mod.rule_for(-3).delete_config is False


@pytest.mark.spec
def test_no_requery_values_are_minus_two_and_minus_four() -> None:
    assert vers_mod.rule_for(-2).may_requery is False
    assert vers_mod.rule_for(-4).may_requery is False
    assert vers_mod.rule_for(-1).may_requery is True


def test_all_non_positive_versions_disable_the_client() -> None:
    for rule in vers_mod.VERS_RULES:
        assert rule.client_disabled is True
        assert vers_mod.is_disable_value(rule.version) is True
    assert vers_mod.is_disable_value(1) is False


def test_client_holds_current_only_on_exact_match() -> None:
    assert vers_mod.client_holds_current(3, 3) is True
    assert vers_mod.client_holds_current(2, 3) is False
    # A client claiming a higher version than the server knows is stale: the
    # server is authoritative.
    assert vers_mod.client_holds_current(9, 3) is False
    # A disable value is never "current".
    assert vers_mod.client_holds_current(0, 0) is False


def test_version_bump_is_monotonic_and_recovers_from_disable_values() -> None:
    assert vers_mod.next_version(1) == 2
    assert vers_mod.next_version(41) == 42
    assert vers_mod.next_version(0) == 1
    assert vers_mod.next_version(-2) == 1


def test_forceable_versions_cover_every_rule() -> None:
    assert set(vers_mod.FORCEABLE_VERSIONS) == {r.version for r in vers_mod.VERS_RULES}


def test_every_rule_carries_a_spec_reference() -> None:
    # The table is the auditable record of interpretation; a row without a
    # reference cannot be reviewed.
    for rule in vers_mod.VERS_RULES:
        assert rule.spec_ref
        assert rule.note
