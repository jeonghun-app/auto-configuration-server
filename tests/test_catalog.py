"""Provisioning catalogue loading, validation and profile overlays."""

from __future__ import annotations

import pathlib

import pytest

from acs.errors import CatalogError
from acs.protocol.omacp.catalog import available_profiles, load_catalog


def write_catalog(
    tmp_path: pathlib.Path, base: str, profile: str = "", body: str = ""
) -> pathlib.Path:
    (tmp_path / "base.yaml").write_text(base, encoding="utf-8")
    if profile:
        (tmp_path / "profiles").mkdir(exist_ok=True)
        (tmp_path / "profiles" / f"{profile}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


MINIMAL_BASE = """
meta:
  id: base
entries:
  - path: APPLICATION:ap2002/SERVICES
    parm: ChatAuth
    type: bool01
    default: "1"
    spec: RCC.07
    verified: true
  - path: APPLICATION:ap2002/MESSAGING/FT
    parm: MaxSizeFileTr
    type: int
    default: "10240"
    spec: RCC.07
"""


def test_real_catalogue_loads_and_is_non_trivial() -> None:
    catalog = load_catalog("UP_2.4")
    assert len(catalog.entries) > 100
    assert "ap2001" in catalog.app_ids()
    assert "ap2002" in catalog.app_ids()


def test_every_entry_declares_a_spec_reference() -> None:
    # A parameter with no reference cannot be reviewed against the standard.
    for entry in load_catalog().entries:
        assert entry.spec, f"{entry.key} has no spec reference"


def test_verified_count_is_reported_honestly() -> None:
    catalog = load_catalog()
    assert 0 < catalog.verified_count < len(catalog.entries)


def test_profiles_are_discoverable() -> None:
    profiles = available_profiles()
    assert "UP_2.4" in profiles
    assert "UP_1.0" in profiles
    assert "joyn_blackbird" in profiles


def test_up_1_0_removes_chatbot_parameters() -> None:
    base = load_catalog().by_key()
    up10 = load_catalog("UP_1.0").by_key()
    chatbot_keys = [k for k in base if "Chatbot" in k]
    assert chatbot_keys
    for key in chatbot_keys:
        assert key not in up10


def test_blackbird_overrides_the_file_transfer_mechanism() -> None:
    entry = load_catalog("joyn_blackbird").by_key()["APPLICATION:ap2002/MESSAGING/FT/ftDefaultMech"]
    assert entry.default == "MSRP"
    up24 = load_catalog("UP_2.4").by_key()["APPLICATION:ap2002/MESSAGING/FT/ftDefaultMech"]
    assert up24.default == "HTTP"


def test_overlay_can_add_a_new_entry(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        MINIMAL_BASE,
        "custom",
        """
meta:
  id: custom
entries:
  - path: APPLICATION:ap2002/SERVICES
    parm: NewSwitch
    type: bool01
    default: "1"
    spec: operator
""",
    )
    catalog = load_catalog("custom", root=root)
    assert "APPLICATION:ap2002/SERVICES/NewSwitch" in catalog.by_key()


def test_missing_base_catalogue_is_a_fatal_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(root=tmp_path)


def test_duplicate_entry_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        """
meta:
  id: base
entries:
  - path: APPLICATION:ap2002/SERVICES
    parm: ChatAuth
    type: bool01
    default: "1"
  - path: APPLICATION:ap2002/SERVICES
    parm: ChatAuth
    type: bool01
    default: "0"
""",
    )
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(root=root)


def test_invalid_type_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        """
meta:
  id: base
entries:
  - path: APPLICATION:ap2002/SERVICES
    parm: ChatAuth
    type: nonsense
""",
    )
    with pytest.raises(CatalogError, match="unknown type"):
        load_catalog(root=root)


def test_enum_without_values_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        """
meta:
  id: base
entries:
  - path: APPLICATION:ap2002/SERVICES
    parm: Mode
    type: enum
""",
    )
    with pytest.raises(CatalogError, match="declares no values"):
        load_catalog(root=root)


def test_non_integer_default_on_int_entry_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        """
meta:
  id: base
entries:
  - path: APPLICATION:ap2002/MESSAGING/FT
    parm: MaxSizeFileTr
    type: int
    default: "big"
""",
    )
    with pytest.raises(CatalogError, match="not an integer"):
        load_catalog(root=root)


def test_invalid_characteristic_path_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_catalog(
        tmp_path,
        """
meta:
  id: base
entries:
  - path: "APPLICATION:ap2002/BAD PATH"
    parm: X
""",
    )
    with pytest.raises(CatalogError, match="invalid characteristic path"):
        load_catalog(root=root)


def test_placeholders_are_declared_only_from_the_known_set() -> None:
    allowed = {
        "imsi",
        "msisdn",
        "msisdn_national",
        "mcc",
        "mnc",
        "ims_domain",
        "impi",
        "impu",
        "device_id",
        "acs_host",
        "acs_fqdn",
        "profile",
    }
    for entry in load_catalog().entries:
        unknown = entry.placeholders() - allowed
        assert not unknown, f"{entry.key} uses unknown placeholders {unknown}"
