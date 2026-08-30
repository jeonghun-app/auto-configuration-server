"""Configuration version (``VERS``) semantics.

This is the single most spec-sensitive part of an ACS and the part most often
got wrong, so the whole mapping lives in one reviewable table instead of being
scattered through the request handler.

Two distinct things are both called a "version" and must not be conflated:

* the **request** parameter ``vers`` — what the client currently holds;
* the **response** ``VERS/version`` characteristic — what the client should do.

A positive response version is a real configuration revision. Zero and the
negative values are *operational instructions*, not revisions.

.. warning::
   The interpretation of ``-1`` .. ``-4`` differs between RCC.14 releases and
   between vendor implementations. The table below is the baseline this server
   implements and documents; see ``docs/spec-coverage.md`` for the per-value
   verification status, and ADR-0003 for why it is encoded as data.
"""

from __future__ import annotations

import dataclasses
import enum


class VersAction(str, enum.Enum):
    """What the client is being told to do."""

    APPLY = "apply"
    """version > 0 — a valid configuration revision; store and apply it."""

    DISABLE_KEEP = "disable_keep"
    """version = 0 — configuration invalid, RCS disabled. The client keeps no
    valid configuration and must not re-query until a trigger occurs."""

    DISABLE_DELETE_RETRY = "disable_delete_retry"
    """version = -1 — RCS disabled, delete the stored configuration, and query
    again at the next trigger (boot, SIM change, user action)."""

    DISABLE_DELETE_NO_RETRY = "disable_delete_no_retry"
    """version = -2 — RCS disabled, delete the stored configuration, and do not
    query again until a factory reset or SIM swap."""

    DORMANT = "dormant"
    """version = -3 — keep the existing configuration but stay dormant; retry
    later. Used for a temporary operator-side withhold."""

    BLOCKED = "blocked"
    """version = -4 — this device or client is not permitted to be provisioned;
    treat as a permanent block."""


@dataclasses.dataclass(frozen=True, slots=True)
class VersRule:
    """One row of the version semantics table."""

    version: int
    action: VersAction
    client_disabled: bool
    delete_config: bool
    may_requery: bool
    spec_ref: str
    verified: bool
    note: str


#: The authoritative table. ``verified`` is ``False`` where the mapping is a
#: documented interpretation rather than a clause cross-checked against the
#: pinned RCC.14 edition — see ``docs/spec-coverage.md``.
VERS_RULES: tuple[VersRule, ...] = (
    VersRule(
        version=0,
        action=VersAction.DISABLE_KEEP,
        client_disabled=True,
        delete_config=False,
        may_requery=True,
        spec_ref="RCC.14 Configuration version values",
        verified=True,
        note="Configuration invalid; RCS off. Re-query only on a trigger.",
    ),
    VersRule(
        version=-1,
        action=VersAction.DISABLE_DELETE_RETRY,
        client_disabled=True,
        delete_config=True,
        may_requery=True,
        spec_ref="RCC.14 Configuration version values",
        verified=False,
        note="Disable and wipe configuration; re-query at the next trigger.",
    ),
    VersRule(
        version=-2,
        action=VersAction.DISABLE_DELETE_NO_RETRY,
        client_disabled=True,
        delete_config=True,
        may_requery=False,
        spec_ref="RCC.14 Configuration version values",
        verified=False,
        note="Disable and wipe; no re-query until factory reset or SIM swap.",
    ),
    VersRule(
        version=-3,
        action=VersAction.DORMANT,
        client_disabled=True,
        delete_config=False,
        may_requery=True,
        spec_ref="RCC.14 Configuration version values",
        verified=False,
        note="Dormant: keep configuration, retry after VALIDITY.",
    ),
    VersRule(
        version=-4,
        action=VersAction.BLOCKED,
        client_disabled=True,
        delete_config=False,
        may_requery=False,
        spec_ref="RCC.14 Configuration version values",
        verified=False,
        note="Device/client permanently barred from provisioning.",
    ),
)

_BY_VERSION = {rule.version: rule for rule in VERS_RULES}

#: Values an operator may force through the admin API.
FORCEABLE_VERSIONS: tuple[int, ...] = tuple(sorted(_BY_VERSION, reverse=True))


def rule_for(version: int) -> VersRule:
    """Return the rule describing a response version."""
    if version > 0:
        return VersRule(
            version=version,
            action=VersAction.APPLY,
            client_disabled=False,
            delete_config=False,
            may_requery=True,
            spec_ref="RCC.14 Configuration version values",
            verified=True,
            note="Valid configuration revision.",
        )
    rule = _BY_VERSION.get(version)
    if rule is None:
        raise ValueError(f"unsupported configuration version {version}")
    return rule


def is_disable_value(version: int) -> bool:
    """True when the version instructs the client to switch RCS off."""
    return version <= 0


def client_holds_current(request_vers: int, server_version: int) -> bool:
    """True when the client already holds the configuration we would send.

    In that case RCC.14 allows the ACS to answer with a document containing only
    the ``VERS`` characteristic, avoiding a pointless full re-provisioning.
    A client reporting a *higher* version than the server knows about is treated
    as stale rather than current: the server is authoritative.
    """
    return server_version > 0 and request_vers == server_version


def next_version(current: int) -> int:
    """Bump a configuration revision monotonically.

    A decreasing version can wedge clients, so a non-positive stored value
    restarts at 1.
    """
    return current + 1 if current > 0 else 1
