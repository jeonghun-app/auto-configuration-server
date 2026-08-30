"""OMA-CP document construction and serialisation."""

from __future__ import annotations

import pytest
from lxml import etree
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN

from acs.config import Settings
from acs.protocol.omacp import builder, writer
from acs.protocol.omacp.document import (
    APP_ID_DM_ACCOUNT,
    APP_ID_IMS,
    APP_ID_RCS,
    Characteristic,
    ProvisioningDoc,
)
from acs.protocol.request import ConfigQuery, parse_config_query


def build(
    settings: Settings, query: ConfigQuery | None = None, **kwargs: object
) -> ProvisioningDoc:
    defaults: dict[str, object] = {
        "settings": settings,
        "query": query or ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI),
        "imsi": TEST_IMSI,
        "msisdn": TEST_MSISDN,
        "version": 1,
        "validity": 86400,
        "profile": "UP_2.4",
    }
    defaults.update(kwargs)
    return builder.build_document(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- structure
def test_document_starts_with_vers(settings: Settings) -> None:
    doc = build(settings)
    assert doc.characteristics[0].type == "VERS"
    assert doc.characteristics[0].find_parm("version").value == "1"  # type: ignore[union-attr]
    assert doc.characteristics[0].find_parm("validity").value == "86400"  # type: ignore[union-attr]


def test_both_applications_are_emitted(settings: Settings) -> None:
    doc = build(settings)
    assert doc.application(APP_ID_IMS) is not None
    assert doc.application(APP_ID_RCS) is not None


def test_rcs_application_references_the_ims_application(settings: Settings) -> None:
    rcs = build(settings).application(APP_ID_RCS)
    assert rcs is not None
    assert rcs.find_parm("AppRef").value == "ap2001"  # type: ignore[union-attr]


def test_token_characteristic_is_included_when_issued(settings: Settings) -> None:
    doc = build(settings, token="tok-123")
    token = next(c for c in doc.characteristics if c.type == "TOKEN")
    assert token.find_parm("token").value == "tok-123"  # type: ignore[union-attr]


def test_placeholders_are_resolved_from_the_subscriber(settings: Settings) -> None:
    ims = build(settings).application(APP_ID_IMS)
    assert ims is not None
    impi = ims.find_parm("Private_User_Identity")
    assert impi is not None
    assert impi.value == f"{TEST_IMSI}@ims.mnc001.mcc001.3gppnetwork.org"


def test_nested_characteristics_are_created(settings: Settings) -> None:
    rcs = build(settings).application(APP_ID_RCS)
    assert rcs is not None
    messaging = next(c for c in rcs.children if c.type == "MESSAGING")
    assert {c.type for c in messaging.children} >= {"CHAT", "FT"}


def test_subscriber_overrides_win_over_catalogue_defaults(settings: Settings) -> None:
    doc = build(
        settings,
        overrides={"APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr": "1"},
    )
    rcs = doc.application(APP_ID_RCS)
    assert rcs is not None
    ft = next(
        c for c in next(c for c in rcs.children if c.type == "MESSAGING").children if c.type == "FT"
    )
    assert ft.find_parm("MaxSizeFileTr").value == "1"  # type: ignore[union-attr]


# ------------------------------------------------------------ request rules
def test_default_sms_app_zero_disables_messaging_authorisations(settings: Settings) -> None:
    query = ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, default_sms_app=0)
    rcs = build(settings, query=query).application(APP_ID_RCS)
    assert rcs is not None
    services = next(c for c in rcs.children if c.type == "SERVICES")
    for name in ("standaloneMsgAuth", "ChatAuth", "GroupChatAuth"):
        assert services.find_parm(name).value == "0"  # type: ignore[union-attr]


def test_default_sms_app_one_leaves_messaging_enabled(settings: Settings) -> None:
    query = ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, default_sms_app=1)
    rcs = build(settings, query=query).application(APP_ID_RCS)
    assert rcs is not None
    services = next(c for c in rcs.children if c.type == "SERVICES")
    assert services.find_parm("ChatAuth").value == "1"  # type: ignore[union-attr]


def test_requested_app_subset_filters_applications(settings: Settings) -> None:
    query = ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, apps=("ap2001",))
    doc = build(settings, query=query)
    assert doc.application(APP_ID_IMS) is not None
    assert doc.application(APP_ID_RCS) is None


# ------------------------------------------------------------------ profiles
def test_profile_selection_changes_the_document(settings: Settings) -> None:
    up24 = build(settings, profile="UP_2.4")
    legacy = build(settings, profile="joyn_blackbird")

    def transport(doc: ProvisioningDoc) -> str:
        rcs = doc.application(APP_ID_RCS)
        assert rcs is not None
        other = next(c for c in rcs.children if c.type == "OTHER")
        proto = next(c for c in other.children if c.type == "TRANSPORTPROTO")
        return proto.find_parm("psSignalling").value  # type: ignore[union-attr]

    assert transport(up24) == "SIPoTLS"
    assert transport(legacy) == "SIPoTCP"


# ----------------------------------------------------------------- OMA-DM
def test_dm_account_is_bootstrapped_when_a_password_exists(settings: Settings) -> None:
    doc = build(settings, dm_password="s3cret")
    dm = doc.application(APP_ID_DM_ACCOUNT)
    assert dm is not None
    assert dm.find_parm("ADDR") is not None
    assert dm.find_parm("AAUTHNAME").value == TEST_IMSI  # type: ignore[union-attr]
    assert dm.find_parm("AAUTHSECRET").value == "s3cret"  # type: ignore[union-attr]


def test_dm_account_is_omitted_when_bootstrap_is_disabled() -> None:
    settings = Settings(env="test", dm_bootstrap_in_cp=False)
    doc = build(settings, dm_password="s3cret")
    assert doc.application(APP_ID_DM_ACCOUNT) is None


# ------------------------------------------------------------ special forms
def test_vers_only_document_has_no_applications() -> None:
    doc = builder.build_vers_only_document(7, 3600)
    assert len(doc.characteristics) == 1
    assert doc.characteristics[0].type == "VERS"


def test_message_document_carries_a_msg_characteristic() -> None:
    doc = builder.build_message_document(1, 3600, "Title", "Body", True, True)
    msg = next(c for c in doc.characteristics if c.type == "MSG")
    assert msg.find_parm("title").value == "Title"  # type: ignore[union-attr]
    assert msg.find_parm("Reject_btn").value == "1"  # type: ignore[union-attr]


# ------------------------------------------------------------------ writer
def test_serialised_document_is_valid_and_declares_utf8(settings: Settings) -> None:
    payload = writer.to_xml(build(settings))
    assert payload.startswith(b"<?xml version='1.0' encoding='UTF-8'?>")
    assert writer.validate_structure(payload) == []


def test_structural_validation_catches_a_missing_vers() -> None:
    doc = ProvisioningDoc()
    doc.add(Characteristic("APPLICATION")).add_parm("AppID", "ap2002")
    problems = writer.validate_structure(writer.to_xml(doc))
    assert any("VERS" in problem for problem in problems)


def test_structural_validation_rejects_a_wrong_root() -> None:
    problems = writer.validate_structure(b"<other/>")
    assert any("wap-provisioningdoc" in problem for problem in problems)


def test_structural_validation_reports_malformed_xml() -> None:
    problems = writer.validate_structure(b"<wap-provisioningdoc")
    assert problems and "not well-formed" in problems[0]


def test_special_characters_in_values_are_escaped(settings: Settings) -> None:
    doc = build(
        settings,
        overrides={"APPLICATION:ap2002/OTHER/deviceID": 'a&b<c>"d"'},
    )
    payload = writer.to_xml(doc)
    assert b"a&amp;b&lt;c&gt;" in payload
    root = writer.parse(payload)
    found = root.find(".//parm[@name='deviceID']")
    assert found is not None
    assert found.get("value") == 'a&b<c>"d"'


def test_serialisation_is_deterministic(settings: Settings) -> None:
    # Some clients are order sensitive, so identical inputs must produce
    # byte-identical output.
    assert writer.to_xml(build(settings)) == writer.to_xml(build(settings))


def test_parser_does_not_resolve_external_entities() -> None:
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<wap-provisioningdoc version="1.1">'
        b'<characteristic type="VERS"><parm name="version" value="&x;"/></characteristic>'
        b"</wap-provisioningdoc>"
    )
    # With entity resolution off, lxml refuses the document outright rather than
    # substituting file content — which is the outcome we want.
    with pytest.raises(etree.XMLSyntaxError):
        writer.parse(payload)
    assert writer.validate_structure(payload)[0].startswith("not well-formed")


def test_document_paths_are_reported_for_coverage(settings: Settings) -> None:
    paths = build(settings).paths()
    assert any(p.endswith("/ChatAuth") for p in paths)
    assert len(paths) == build(settings).parm_count()


def test_doctype_can_be_emitted(settings: Settings) -> None:
    payload = writer.to_xml(build(settings), doctype=True)
    assert b"WAPFORUM//DTD PROV 1.0" in payload


def test_writer_produces_parsable_tree(settings: Settings) -> None:
    element = writer.to_element(build(settings))
    assert isinstance(element, etree._Element)
    assert element.tag == "wap-provisioningdoc"


@pytest.mark.spec
def test_empty_optional_values_are_omitted_not_emitted_blank(settings: Settings) -> None:
    # rcseOnlyAPN has an empty default; emitting value="" would look like a real
    # setting to the client.
    payload = writer.to_xml(build(settings))
    assert b'name="rcseOnlyAPN"' not in payload


def test_query_parsed_from_wire_produces_a_document(settings: Settings) -> None:
    query = parse_config_query(
        {"vers": ["0"], "IMSI": [TEST_IMSI], "IMEI": [TEST_IMEI], "app": ["ap2002"]}
    )
    doc = build(settings, query=query)
    assert doc.application(APP_ID_RCS) is not None
    assert doc.application(APP_ID_IMS) is None
