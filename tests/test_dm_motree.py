"""OMA-DM management object tree."""

from __future__ import annotations

import pathlib

import pytest

from acs.errors import CatalogError
from acs.protocol.omadm.motree import build_context, load_tree


def test_real_tree_loads_the_expected_objects() -> None:
    tree = load_tree()
    urns = tree.urns
    assert "urn:oma:mo:oma-dm-devinfo:1.0" in urns
    assert "urn:oma:mo:oma-dm-devdetail:1.0" in urns
    assert "urn:oma:mo:ext-3gpp-ims:1.0" in urns
    assert len(tree.all_nodes()) > 40


def test_node_lookup_by_uri() -> None:
    tree = load_tree()
    node = tree.node("./DevInfo/DevId")
    assert node is not None
    assert node.source == "device"
    assert tree.node("./DoesNotExist") is None


def test_trailing_slash_is_tolerated() -> None:
    assert load_tree().node("./DevInfo/DevId/") is not None


def test_object_for_resolves_the_owning_management_object() -> None:
    tree = load_tree()
    mo = tree.object_for("./3GPP_IMS/1/Timer_T1")
    assert mo is not None
    assert mo.urn == "urn:oma:mo:ext-3gpp-ims:1.0"
    assert tree.object_for("./Unknown/Node") is None


def test_children_are_direct_descendants_only() -> None:
    children = load_tree().children("./DevInfo")
    names = {node.name for node in children}
    assert "DevId" in names
    assert all("/" not in node.uri[len("./DevInfo/") :] for node in children)


def test_device_query_uris_are_the_device_owned_leaves() -> None:
    uris = load_tree().device_query_uris()
    assert "./DevInfo/DevId" in uris
    assert "./DevDetail/SwV" in uris
    # Interior nodes and server-owned nodes are excluded.
    assert "./DevInfo" not in uris
    assert not any(uri.startswith("./3GPP_IMS") for uri in uris)


def test_device_query_uris_can_be_limited() -> None:
    assert len(load_tree().device_query_uris(limit=3)) == 3


def test_server_nodes_respect_feature_gates() -> None:
    tree = load_tree()
    with_volte = {n.uri for n in tree.server_nodes(["volte", "rcs"])}
    without_volte = {n.uri for n in tree.server_nodes(["rcs"])}
    assert "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" in with_volte
    assert "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" not in without_volte
    # Ungated nodes are always present.
    assert "./3GPP_IMS/1/Private_User_Identity" in without_volte


def test_rcs_nodes_are_gated_on_the_rcs_feature() -> None:
    tree = load_tree()
    assert not any(n.uri.startswith("./RCS/") for n in tree.server_nodes([]))
    assert any(n.uri.startswith("./RCS/") for n in tree.server_nodes(["rcs"]))


def test_placeholders_are_rendered_from_the_context() -> None:
    tree = load_tree()
    node = tree.node("./3GPP_IMS/1/Private_User_Identity")
    assert node is not None
    context = build_context(
        imsi="001010000000001",
        msisdn="+821012345678",
        device_id="dev",
        ims_domain="ims.example.org",
        impi="001010000000001@ims.example.org",
        impu="sip:+821012345678@ims.example.org",
    )
    assert tree.render(node, context, {}) == "001010000000001@ims.example.org"


def test_overrides_take_precedence_over_defaults() -> None:
    tree = load_tree()
    node = tree.node("./3GPP_IMS/1/Timer_T1")
    assert node is not None
    rendered = tree.render(node, {}, {"./3GPP_IMS/1/Timer_T1": "9999"})
    assert rendered == "9999"


def test_every_node_declares_a_spec_reference() -> None:
    for node in load_tree().all_nodes():
        assert node.spec, f"{node.uri} has no spec reference"


def test_verified_count_is_reported() -> None:
    tree = load_tree()
    assert 0 < tree.verified_count < len(tree.all_nodes())


# --------------------------------------------------------------- validation
def write_mo(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    (tmp_path / "01-test.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_empty_directory_is_a_fatal_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="no management objects"):
        load_tree(tmp_path)


def test_missing_directory_is_a_fatal_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="not found"):
        load_tree(tmp_path / "absent")


def test_missing_meta_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(tmp_path, "meta:\n  id: x\nnodes: []\n")
    with pytest.raises(CatalogError, match="meta.urn is required"):
        load_tree(root)


def test_node_outside_the_root_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(
        tmp_path,
        """
meta:
  id: x
  urn: urn:test
  root: ./X
nodes:
  - uri: ./Y/Node
""",
    )
    with pytest.raises(CatalogError, match="outside MO root"):
        load_tree(root)


def test_unknown_format_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(
        tmp_path,
        """
meta:
  id: x
  urn: urn:test
  root: ./X
nodes:
  - uri: ./X/Node
    format: nonsense
""",
    )
    with pytest.raises(CatalogError, match="unknown format"):
        load_tree(root)


def test_unknown_source_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(
        tmp_path,
        """
meta:
  id: x
  urn: urn:test
  root: ./X
nodes:
  - uri: ./X/Node
    source: elsewhere
""",
    )
    with pytest.raises(CatalogError, match="unknown source"):
        load_tree(root)


def test_bad_bool_default_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(
        tmp_path,
        """
meta:
  id: x
  urn: urn:test
  root: ./X
nodes:
  - uri: ./X/Node
    format: bool
    default: "1"
""",
    )
    with pytest.raises(CatalogError, match="must be 'true' or 'false'"):
        load_tree(root)


def test_duplicate_node_is_rejected(tmp_path: pathlib.Path) -> None:
    root = write_mo(
        tmp_path,
        """
meta:
  id: x
  urn: urn:test
  root: ./X
nodes:
  - uri: ./X/Node
  - uri: ./X/Node
""",
    )
    with pytest.raises(CatalogError, match="duplicate node"):
        load_tree(root)


def test_a_new_management_object_needs_no_code(tmp_path: pathlib.Path) -> None:
    # This is the extensibility contract: dropping a YAML file in adds a managed
    # object, with no change to the DM server.
    root = write_mo(
        tmp_path,
        """
meta:
  id: fumo
  urn: urn:oma:mo:oma-fumo:1.0
  root: ./FUMO
  title: Firmware update
nodes:
  - uri: ./FUMO
    format: node
    source: server
    spec: OMA FUMO
  - uri: ./FUMO/PkgURL
    format: chr
    source: server
    default: "https://fw.{ims_domain}/latest"
    spec: OMA FUMO PkgURL
""",
    )
    tree = load_tree(root)
    assert tree.urns == ["urn:oma:mo:oma-fumo:1.0"]
    node = tree.node("./FUMO/PkgURL")
    assert node is not None
    assert tree.render(node, {"ims_domain": "ims.example.org"}, {}) == (
        "https://fw.ims.example.org/latest"
    )
