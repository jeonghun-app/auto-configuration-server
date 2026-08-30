"""SyncML DM 1.2 parsing and generation."""

from __future__ import annotations

import base64

import pytest

from acs.protocol.omadm import syncml

PACKAGE_1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<SyncML xmlns="SYNCML:SYNCML1.2">
  <SyncHdr>
    <VerDTD>1.2</VerDTD>
    <VerProto>DM/1.2</VerProto>
    <SessionID>42</SessionID>
    <MsgID>1</MsgID>
    <Target><LocURI>https://acs.example.com/dm</LocURI></Target>
    <Source><LocURI>IMEI:356938035643809</LocURI><LocName>001010000000001</LocName></Source>
    <Cred>
      <Meta>
        <Format xmlns="syncml:metinf">b64</Format>
        <Type xmlns="syncml:metinf">syncml:auth-basic</Type>
      </Meta>
      <Data>MDAxMDEwMDAwMDAwMDAxOnBhc3N3b3Jk</Data>
    </Cred>
    <Meta><MaxMsgSize xmlns="syncml:metinf">16384</MaxMsgSize></Meta>
  </SyncHdr>
  <SyncBody>
    <Alert>
      <CmdID>1</CmdID>
      <Data>1201</Data>
    </Alert>
    <Replace>
      <CmdID>2</CmdID>
      <Item>
        <Source><LocURI>./DevInfo/DevId</LocURI></Source>
        <Meta>
          <Format xmlns="syncml:metinf">chr</Format>
          <Type xmlns="syncml:metinf">text/plain</Type>
        </Meta>
        <Data>IMEI:356938035643809</Data>
      </Item>
      <Item>
        <Source><LocURI>./DevInfo/Man</LocURI></Source>
        <Data>SimCorp</Data>
      </Item>
    </Replace>
    <Final/>
  </SyncBody>
</SyncML>
"""


def test_header_is_parsed() -> None:
    message = syncml.parse(PACKAGE_1)
    assert message.header.session_id == "42"
    assert message.header.msg_id == "1"
    assert message.header.source == "IMEI:356938035643809"
    assert message.header.source_name == "001010000000001"
    assert message.header.max_msg_size == 16384
    assert message.header.ver_proto == "DM/1.2"


def test_credentials_are_parsed_and_decoded() -> None:
    credentials = syncml.parse(PACKAGE_1).header.credentials
    assert credentials is not None
    assert credentials.type == syncml.AUTH_BASIC
    assert credentials.decode_basic() == ("001010000000001", "password")


def test_malformed_basic_credential_decodes_to_none() -> None:
    credentials = syncml.Credentials(
        type=syncml.AUTH_BASIC, data=base64.b64encode(b"nocolon").decode()
    )
    assert credentials.decode_basic() is None


def test_non_basic_credential_is_not_decoded_as_basic() -> None:
    assert syncml.Credentials(type=syncml.AUTH_MD5, data="x").decode_basic() is None


def test_commands_and_final_are_parsed() -> None:
    message = syncml.parse(PACKAGE_1)
    assert message.final is True
    assert message.has_alert(syncml.ALERT_CLIENT_INITIATED_MGMT)
    replaces = message.of("Replace")
    assert len(replaces) == 1
    assert replaces[0].cmd_id == 2
    assert replaces[0].items[0].uri == "./DevInfo/DevId"
    assert replaces[0].items[0].data == "IMEI:356938035643809"
    assert replaces[0].items[0].format == "chr"


def test_parsing_is_namespace_tolerant() -> None:
    # Real DM clients disagree about metinf prefixes; rejecting a handset over a
    # namespace declaration would be a self-inflicted interoperability failure.
    payload = PACKAGE_1.replace(b'xmlns="SYNCML:SYNCML1.2"', b"")
    assert syncml.parse(payload).header.session_id == "42"


def test_wrong_root_element_is_rejected() -> None:
    with pytest.raises(syncml.SyncMlParseError, match="root element"):
        syncml.parse(b"<NotSyncML/>")


def test_missing_header_is_rejected() -> None:
    with pytest.raises(syncml.SyncMlParseError, match="SyncHdr"):
        syncml.parse(b'<SyncML xmlns="SYNCML:SYNCML1.2"><SyncBody/></SyncML>')


def test_missing_body_is_rejected() -> None:
    with pytest.raises(syncml.SyncMlParseError, match="SyncBody"):
        syncml.parse(b'<SyncML xmlns="SYNCML:SYNCML1.2"><SyncHdr/></SyncML>')


def test_malformed_xml_is_rejected() -> None:
    with pytest.raises(syncml.SyncMlParseError, match="malformed"):
        syncml.parse(b"<SyncML")


def test_external_entities_are_not_expanded() -> None:
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE SyncML [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<SyncML><SyncHdr><SessionID>&x;</SessionID><MsgID>1</MsgID></SyncHdr>"
        b"<SyncBody/></SyncML>"
    )
    # The entity is left unexpanded rather than read from disk, so the SessionID
    # ends up empty instead of containing file content.
    message = syncml.parse(payload)
    assert "root" not in message.header.session_id
    assert message.header.session_id == ""


# ------------------------------------------------------------------- builder
def build() -> syncml.SyncMlBuilder:
    return syncml.SyncMlBuilder(
        session_id="42", msg_id=1, target="IMEI:1", source="https://acs/dm", max_msg_size=16384
    )


def test_builder_emits_a_well_formed_header() -> None:
    payload = build().status("SyncHdr", "1", "0", syncml.STATUS_OK).build()
    message = syncml.parse(payload)
    assert message.header.session_id == "42"
    assert message.header.target == "IMEI:1"
    assert message.header.max_msg_size == 16384
    assert message.final is True


def test_status_carries_the_references() -> None:
    payload = build().status("Alert", "1", "1", syncml.STATUS_OK, target_ref="./X").build()
    status = syncml.parse(payload).of("Status")[0]
    assert status.cmd == "Alert"
    assert status.msg_ref == "1"
    assert status.cmd_ref == "1"
    assert status.data == syncml.STATUS_OK


def test_authentication_challenge_includes_a_nonce() -> None:
    payload = (
        build()
        .status(
            "SyncHdr",
            "1",
            "0",
            syncml.STATUS_INVALID_CREDENTIALS,
            challenge=(syncml.AUTH_MD5, "bm9uY2U="),
        )
        .build()
    )
    assert b"NextNonce" in payload
    assert b"syncml:auth-md5" in payload
    assert b"401" in payload


def test_get_command_lists_every_uri() -> None:
    payload = build().get(["./DevInfo/DevId", "./DevDetail/SwV"]).build()
    command = syncml.parse(payload).of("Get")[0]
    assert [item.uri for item in command.items] == ["./DevInfo/DevId", "./DevDetail/SwV"]


def test_empty_get_is_omitted() -> None:
    builder = build()
    builder.get([])
    assert builder.command_count == 0


def test_replace_carries_meta_and_data() -> None:
    payload = build().replace([("./3GPP_IMS/1/Timer_T1", "2000", "int", "text/plain")]).build()
    item = syncml.parse(payload).of("Replace")[0].items[0]
    assert item.uri == "./3GPP_IMS/1/Timer_T1"
    assert item.data == "2000"
    assert item.format == "int"


def test_add_of_an_interior_node_carries_no_data() -> None:
    payload = build().add([("./3GPP_IMS/1", "", "node", "")]).build()
    assert b"<Data/>" not in payload
    item = syncml.parse(payload).of("Add")[0].items[0]
    assert item.uri == "./3GPP_IMS/1"


def test_exec_and_alert_commands_are_emitted() -> None:
    payload = build().exec_("./FUMO/Download", "url").alert(syncml.ALERT_END_OF_SESSION).build()
    message = syncml.parse(payload)
    assert message.of("Exec")[0].items[0].uri == "./FUMO/Download"
    assert message.has_alert(syncml.ALERT_END_OF_SESSION)


def test_command_count_excludes_statuses() -> None:
    builder = build().status("SyncHdr", "1", "0", "200").get(["./DevInfo/DevId"])
    assert builder.command_count == 1


def test_non_final_package_omits_final() -> None:
    payload = build().get(["./DevInfo/DevId"]).build(final=False)
    assert syncml.parse(payload).final is False
