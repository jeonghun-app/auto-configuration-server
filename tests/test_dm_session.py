"""OMA-DM session state machine and HTTP endpoint."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import auth as dm_auth
from acs.protocol.omadm import syncml
from acs.protocol.omadm.session import DmService, _device_id_from, password_lookup_for
from acs.store.memory import MemoryStore

DM_PASSWORD = "dm-secret-123"
DM_CONTENT_TYPE = "application/vnd.syncml.dm+xml"


def basic_cred(username: str = TEST_IMSI, password: str = DM_PASSWORD) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def package(
    msg_id: int,
    body: str,
    session_id: str = "42",
    cred: str | None = None,
    auth_type: str = syncml.AUTH_BASIC,
) -> bytes:
    cred_block = ""
    if cred is not None:
        cred_block = f"""
    <Cred>
      <Meta>
        <Format xmlns="syncml:metinf">b64</Format>
        <Type xmlns="syncml:metinf">{auth_type}</Type>
      </Meta>
      <Data>{cred}</Data>
    </Cred>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SyncML xmlns="SYNCML:SYNCML1.2">
  <SyncHdr>
    <VerDTD>1.2</VerDTD>
    <VerProto>DM/1.2</VerProto>
    <SessionID>{session_id}</SessionID>
    <MsgID>{msg_id}</MsgID>
    <Target><LocURI>https://acs.example.com/dm</LocURI></Target>
    <Source><LocURI>IMEI:{TEST_IMEI}</LocURI><LocName>{TEST_IMSI}</LocName></Source>{cred_block}
    <Meta><MaxMsgSize xmlns="syncml:metinf">16384</MaxMsgSize></Meta>
  </SyncHdr>
  <SyncBody>
{body}
    <Final/>
  </SyncBody>
</SyncML>
""".encode()


PACKAGE_1_BODY = f"""    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>
    <Replace>
      <CmdID>2</CmdID>
      <Item>
        <Source><LocURI>./DevInfo/DevId</LocURI></Source>
        <Data>IMEI:{TEST_IMEI}</Data>
      </Item>
      <Item>
        <Source><LocURI>./DevInfo/Man</LocURI></Source>
        <Data>SimCorp</Data>
      </Item>
      <Item>
        <Source><LocURI>./DevInfo/Mod</LocURI></Source>
        <Data>SimPhone</Data>
      </Item>
    </Replace>"""


@pytest.fixture
def dm_store(store: MemoryStore) -> MemoryStore:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    return store


@pytest.fixture
def dm(settings: Settings, dm_store: MemoryStore) -> DmService:
    return DmService(settings, dm_store)


# --------------------------------------------------------------- protocol
def test_disabled_dm_returns_404(dm_store: MemoryStore) -> None:
    off = Settings(env="test", dm_enabled=False, sms_provider="mock")
    assert DmService(off, dm_store).handle(package(1, PACKAGE_1_BODY)).status_code == 404


def test_wbxml_is_refused_explicitly(dm: DmService) -> None:
    # Answering with XML a client cannot decode is worse than a clear refusal.
    outcome = dm.handle(package(1, PACKAGE_1_BODY), "application/vnd.syncml.dm+wbxml")
    assert outcome.status_code == 415
    assert outcome.detail == "wbxml_not_supported"


def test_malformed_payload_returns_400(dm: DmService) -> None:
    assert dm.handle(b"<not-syncml").status_code == 400


def test_missing_session_id_returns_400(dm: DmService) -> None:
    payload = package(1, PACKAGE_1_BODY, session_id="")
    assert dm.handle(payload).status_code == 400


def test_oversized_payload_is_refused(dm: DmService) -> None:
    assert dm.handle(b"x" * (16384 * 4 + 1)).status_code == 413


# ------------------------------------------------------------ authentication
def test_missing_credentials_produce_a_syncml_challenge(dm: DmService) -> None:
    outcome = dm.handle(package(1, PACKAGE_1_BODY))
    # The HTTP status stays 200: DM carries the auth outcome in the body, and an
    # HTTP 401 makes many DM clients abort instead of retrying.
    assert outcome.status_code == 200
    assert outcome.metric == "DmAuthRejected"
    message = syncml.parse(outcome.body)
    status = message.of("Status")[0]
    assert status.data == syncml.STATUS_INVALID_CREDENTIALS
    assert b"NextNonce" in outcome.body


def test_wrong_password_is_rejected(dm: DmService) -> None:
    payload = package(1, PACKAGE_1_BODY, cred=basic_cred(password="wrong"))
    assert dm.handle(payload).metric == "DmAuthRejected"


def test_unknown_user_is_indistinguishable_from_a_wrong_password(dm: DmService) -> None:
    unknown = dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred(username="001019999999999")))
    wrong = dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred(password="wrong")))
    assert unknown.metric == wrong.metric == "DmAuthRejected"


def test_valid_basic_credentials_are_accepted(dm: DmService) -> None:
    outcome = dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    assert outcome.metric == "DmInventoryRequested"
    status = syncml.parse(outcome.body).of("Status")[0]
    assert status.data == syncml.STATUS_AUTH_ACCEPTED


def test_auth_can_be_disabled_for_local_testing(dm_store: MemoryStore) -> None:
    open_settings = Settings(env="test", dm_auth_scheme="none", sms_provider="mock")
    outcome = DmService(open_settings, dm_store).handle(package(1, PACKAGE_1_BODY))
    assert outcome.metric == "DmInventoryRequested"


def test_md5_credentials_are_verified_against_the_session_nonce(
    dm_store: MemoryStore,
) -> None:
    md5_settings = Settings(env="test", dm_auth_scheme="md5", sms_provider="mock")
    service = DmService(md5_settings, dm_store)

    first = service.handle(package(1, PACKAGE_1_BODY))
    assert first.metric == "DmAuthRejected"
    session = dm_store.get_dm_session("42")
    assert session is not None and session.nonce

    credential = dm_auth.md5_credential(TEST_IMSI, DM_PASSWORD, session.nonce)
    second = service.handle(package(2, PACKAGE_1_BODY, cred=credential, auth_type=syncml.AUTH_MD5))
    assert second.metric == "DmInventoryRequested"


def test_md5_with_a_wrong_password_is_rejected(dm_store: MemoryStore) -> None:
    md5_settings = Settings(env="test", dm_auth_scheme="md5", sms_provider="mock")
    service = DmService(md5_settings, dm_store)
    service.handle(package(1, PACKAGE_1_BODY))
    session = dm_store.get_dm_session("42")
    assert session is not None
    bad = dm_auth.md5_credential(TEST_IMSI, "wrong", session.nonce)
    outcome = service.handle(package(2, PACKAGE_1_BODY, cred=bad, auth_type=syncml.AUTH_MD5))
    assert outcome.metric == "DmAuthRejected"


# ---------------------------------------------------------------- full session
DEVICE_VALUES = {
    "./DevInfo/DevId": f"IMEI:{TEST_IMEI}",
    "./DevInfo/Man": "SimCorp",
    "./DevInfo/Mod": "SimPhone",
    "./DevInfo/DmV": "1.2",
    "./DevInfo/Lang": "en",
    "./DevDetail/SwV": "SIM-1.0",
    "./DevDetail/FwV": "SIM-FW-1.0",
}


def run_session(dm: DmService, dm_store: MemoryStore) -> dict[str, str]:
    first = dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    gets = syncml.parse(first.body).of("Get")
    assert gets, "server must request the device inventory"
    uris = [item.uri for command in gets for item in command.items]

    results_items = "".join(
        "<Item><Source><LocURI>{uri}</LocURI></Source>" "<Data>{value}</Data></Item>".format(
            uri=uri, value=DEVICE_VALUES.get(uri, f"v-{uri.rsplit('/', 1)[-1]}")
        )
        for uri in uris
    )
    body = (
        "    <Status><CmdID>1</CmdID><MsgRef>1</MsgRef><CmdRef>0</CmdRef>"
        "<Cmd>SyncHdr</Cmd><Data>200</Data></Status>\n"
        "    <Results><CmdID>2</CmdID><MsgRef>1</MsgRef><CmdRef>2</CmdRef>"
        f"{results_items}</Results>"
    )
    second = dm.handle(package(2, body, cred=basic_cred()))
    assert second.metric == "DmConfigPushed"
    replaces = syncml.parse(second.body).of("Replace")
    pushed = {item.uri: item.data for command in replaces for item in command.items}

    third_body = (
        "    <Status><CmdID>1</CmdID><MsgRef>2</MsgRef><CmdRef>0</CmdRef>"
        "<Cmd>SyncHdr</Cmd><Data>200</Data></Status>\n"
        "    <Status><CmdID>2</CmdID><MsgRef>2</MsgRef><CmdRef>1</CmdRef>"
        "<Cmd>Replace</Cmd><Data>200</Data></Status>"
    )
    third = dm.handle(package(3, third_body, cred=basic_cred()))
    assert third.session_finished is True
    assert third.metric == "DmSessionComplete"
    assert dm_store.get_dm_session("42") is None
    return pushed


def test_full_session_pushes_ims_and_volte_configuration(
    dm: DmService, dm_store: MemoryStore
) -> None:
    pushed = run_session(dm, dm_store)
    assert pushed["./3GPP_IMS/1/Private_User_Identity"].startswith(TEST_IMSI)
    assert pushed["./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN"] == "3"
    assert pushed["./3GPP_IMS/1/SMS_Over_IP_Networks_Indication"] == "true"
    assert pushed["./3GPP_IMS/1/Ext/RCS/rcsVolteSingleRegistration"] == "true"
    assert any(uri.startswith("./RCS/") for uri in pushed)


def test_volte_nodes_are_omitted_for_a_non_volte_subscriber(
    dm: DmService, dm_store: MemoryStore
) -> None:
    subscriber = dm_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    subscriber.volte_enabled = False
    dm_store.put_subscriber(subscriber)
    pushed = run_session(dm, dm_store)
    assert "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" not in pushed
    # Core IMS identity is still provisioned.
    assert "./3GPP_IMS/1/Private_User_Identity" in pushed


def test_device_inventory_is_recorded_from_the_session(
    dm: DmService, dm_store: MemoryStore
) -> None:
    run_session(dm, dm_store)
    device = dm_store.get_device(TEST_IMEI)
    assert device is not None
    assert device.manufacturer == "SimCorp"
    assert device.model == "SimPhone"
    assert device.sw_version == "SIM-1.0"
    assert device.dm_client_version == "1.2"
    assert device.imsi == TEST_IMSI
    assert device.mo_values["./DevInfo/DevId"] == f"IMEI:{TEST_IMEI}"


def test_subscriber_overrides_reach_the_dm_push(dm: DmService, dm_store: MemoryStore) -> None:
    subscriber = dm_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    subscriber.overrides = {"./3GPP_IMS/1/Timer_T1": "9999"}
    dm_store.put_subscriber(subscriber)
    pushed = run_session(dm, dm_store)
    assert pushed["./3GPP_IMS/1/Timer_T1"] == "9999"


def test_client_ending_the_session_is_acknowledged(dm: DmService, dm_store: MemoryStore) -> None:
    dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    body = "    <Alert><CmdID>1</CmdID><Data>1226</Data></Alert>"
    outcome = dm.handle(package(2, body, cred=basic_cred()))
    assert outcome.session_finished is True
    assert outcome.detail == "client_ended"
    assert dm_store.get_dm_session("42") is None


def test_first_package_without_an_alert_is_a_protocol_error(dm: DmService) -> None:
    body = "    <Get><CmdID>1</CmdID><Item><Target><LocURI>./DevInfo</LocURI></Target></Item></Get>"
    outcome = dm.handle(package(1, body, cred=basic_cred()))
    assert outcome.status_code == 400
    assert "Alert 1200 or 1201" in outcome.detail


def test_server_initiated_alert_is_also_accepted(dm: DmService) -> None:
    body = "    <Alert><CmdID>1</CmdID><Data>1200</Data></Alert>"
    assert dm.handle(package(1, body, cred=basic_cred())).metric == "DmInventoryRequested"


def test_client_reported_failures_are_surfaced(dm: DmService, dm_store: MemoryStore) -> None:
    dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    dm.handle(package(2, "    <Results><CmdID>1</CmdID></Results>", cred=basic_cred()))
    body = (
        "    <Status><CmdID>1</CmdID><MsgRef>2</MsgRef><CmdRef>1</CmdRef>"
        "<Cmd>Replace</Cmd><Data>500</Data></Status>"
    )
    outcome = dm.handle(package(3, body, cred=basic_cred()))
    assert outcome.metric == "DmSessionCompleteWithErrors"


def test_unknown_node_in_a_command_is_reported_not_found(dm: DmService) -> None:
    dm.handle(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    body = (
        "    <Replace><CmdID>1</CmdID><Item><Target><LocURI>./Nope/X</LocURI></Target>"
        "<Data>v</Data></Item></Replace>"
    )
    outcome = dm.handle(package(2, body, cred=basic_cred()))
    codes = {s.cmd: s.data for s in syncml.parse(outcome.body).of("Status")}
    assert codes["Replace"] == syncml.STATUS_NOT_FOUND


# ------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"IMEI:{TEST_IMEI}", TEST_IMEI),
        (f"imei:{TEST_IMEI}", TEST_IMEI),
        (f"urn:gsma:imei:{TEST_IMEI}", TEST_IMEI),
        (TEST_IMEI, TEST_IMEI),
        ("", ""),
    ],
)
def test_device_id_normalisation(source: str, expected: str) -> None:
    assert _device_id_from(source) == expected


def test_password_lookup_helper(dm_store: MemoryStore) -> None:
    lookup = password_lookup_for(dm_store)
    assert lookup(TEST_IMSI) == DM_PASSWORD
    assert lookup("001019999999999") is None
    assert lookup("") is None


# ---------------------------------------------------------------- HTTP layer
def test_dm_endpoint_answers_over_http(settings: Settings, dm_store: MemoryStore) -> None:
    with TestClient(create_app(settings, dm_store)) as client:
        response = client.post(
            "/dm",
            content=package(1, PACKAGE_1_BODY, cred=basic_cred()),
            headers={"Content-Type": DM_CONTENT_TYPE},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/vnd.syncml.dm+xml")
        assert response.headers["cache-control"] == "no-store"
        assert b"<Get>" in response.content


def test_dm_endpoint_is_absent_when_disabled(dm_store: MemoryStore) -> None:
    off = Settings(env="test", dm_enabled=False, sms_provider="mock")
    with TestClient(create_app(off, dm_store)) as client:
        assert client.post("/dm", content=b"x").status_code == 404


def test_management_object_introspection_endpoint(
    settings: Settings, dm_store: MemoryStore
) -> None:
    with TestClient(create_app(settings, dm_store)) as client:
        body = client.get("/dm/mo").json()
        urns = [o["urn"] for o in body["objects"]]
        assert "urn:oma:mo:ext-3gpp-ims:1.0" in urns
        assert body["total_nodes"] > 40


def test_dm_endpoint_refuses_a_huge_body(settings: Settings, dm_store: MemoryStore) -> None:
    with TestClient(create_app(settings, dm_store)) as client:
        response = client.post("/dm", content=b"x" * (512 * 1024 + 1))
        assert response.status_code == 413
