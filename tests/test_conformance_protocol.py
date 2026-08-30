"""Wire-level conformance evidence.

Each test here is named by a row in the conformance registry, and each asserts on
what actually goes out on the wire rather than on a constant existing in the
source. That distinction matters: before this file existed the server declared ten
SyncML status codes it never emitted, and answered commands it had not performed
with 200.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN, base_query

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import syncml
from acs.protocol.omadm.session import DmService
from acs.store.memory import MemoryStore

DM_PASSWORD = "dm-secret-123"
OTHER_IMEI = "356938035643800"


def cred(username: str = TEST_IMSI, password: str = DM_PASSWORD) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def package(
    msg_id: int,
    body: str,
    session_id: str = "42",
    imei: str = TEST_IMEI,
    credential: str | None = None,
    max_msg_size: int = 16384,
) -> bytes:
    cred_block = (
        ""
        if credential is None
        else f"""
    <Cred>
      <Meta>
        <Format xmlns="syncml:metinf">b64</Format>
        <Type xmlns="syncml:metinf">syncml:auth-basic</Type>
      </Meta>
      <Data>{credential}</Data>
    </Cred>"""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SyncML xmlns="SYNCML:SYNCML1.2">
  <SyncHdr>
    <VerDTD>1.2</VerDTD>
    <VerProto>DM/1.2</VerProto>
    <SessionID>{session_id}</SessionID>
    <MsgID>{msg_id}</MsgID>
    <Target><LocURI>https://acs.example.com/dm</LocURI></Target>
    <Source><LocURI>IMEI:{imei}</LocURI><LocName>{TEST_IMSI}</LocName></Source>{cred_block}
    <Meta><MaxMsgSize xmlns="syncml:metinf">{max_msg_size}</MaxMsgSize></Meta>
  </SyncHdr>
  <SyncBody>
{body}
    <Final/>
  </SyncBody>
</SyncML>
""".encode()


INIT_BODY = "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>"


@pytest.fixture
def dm_store(store: MemoryStore) -> MemoryStore:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    return store


@pytest.fixture
def dm(settings: Settings, dm_store: MemoryStore) -> DmService:
    return DmService(settings, dm_store)


def statuses(payload: bytes) -> dict[str, str]:
    """Map command name to the status code the server returned for it."""
    return {c.cmd: c.data for c in syncml.parse(payload).of("Status")}


def commands_of(payload: bytes, name: str) -> list[syncml.Command]:
    return syncml.parse(payload).of(name)


# ----------------------------------------------------------- OMADM-CMD-STATUS
def test_every_received_command_gets_a_status(dm: DmService) -> None:
    body = (
        "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
        "    <Replace><CmdID>2</CmdID><Item>"
        "<Source><LocURI>./DevInfo/Man</LocURI></Source><Data>Sim</Data></Item></Replace>"
    )
    outcome = dm.handle(package(1, body, credential=cred()))
    reported = statuses(outcome.body)
    assert reported["SyncHdr"] == syncml.STATUS_AUTH_ACCEPTED
    assert reported["Alert"] == syncml.STATUS_OK
    assert reported["Replace"] == syncml.STATUS_OK


# --------------------------------------------------------- OMADM-STATUS-406
@pytest.mark.spec
@pytest.mark.parametrize("command", ["Delete", "Copy", "Sequence", "Atomic", "Exec"])
def test_unsupported_commands_are_refused_not_acknowledged(dm: DmService, command: str) -> None:
    # Answering 200 would tell the client the operation had been performed.
    body = (
        "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
        f"    <{command}><CmdID>2</CmdID><Item>"
        "<Target><LocURI>./DevInfo/Man</LocURI></Target></Item>"
        f"</{command}>"
    )
    outcome = dm.handle(package(1, body, credential=cred()))
    assert statuses(outcome.body)[command] == syncml.STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED


def test_unknown_command_name_is_refused(dm: DmService) -> None:
    body = (
        "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
        "    <Frobnicate><CmdID>2</CmdID></Frobnicate>"
    )
    outcome = dm.handle(package(1, body, credential=cred()))
    assert statuses(outcome.body)["Frobnicate"] == syncml.STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED


# ---------------------------------------------------- OMADM-CMD-ADD-INTERIOR
@pytest.mark.spec
def test_interior_nodes_are_added_before_leaves(dm: DmService) -> None:
    """A Replace on a leaf whose parent does not exist gets 404 on a real device."""
    dm.handle(package(1, INIT_BODY, credential=cred()))
    results = (
        "    <Results><CmdID>1</CmdID><MsgRef>1</MsgRef><CmdRef>2</CmdRef>"
        "<Item><Source><LocURI>./DevInfo/Man</LocURI></Source><Data>Sim</Data></Item>"
        "</Results>"
    )
    outcome = dm.handle(package(2, results, credential=cred()))

    adds = commands_of(outcome.body, "Add")
    replaces = commands_of(outcome.body, "Replace")
    assert adds, "the server must create the interior nodes it is about to write into"
    added = [item.uri for command in adds for item in command.items]
    assert "./3GPP_IMS" in added
    assert "./3GPP_IMS/1" in added
    assert replaces, "the configuration itself must still be pushed"

    # Every leaf being replaced must have had its ancestors created.
    for command in replaces:
        for item in command.items:
            parent = item.uri.rsplit("/", 1)[0]
            interior = {n.uri for n in dm.tree.all_nodes() if n.is_interior}
            if parent in interior:
                assert parent in added, f"{item.uri} written without creating {parent}"


def test_interior_nodes_are_ordered_parent_first(dm: DmService) -> None:
    dm.handle(package(1, INIT_BODY, credential=cred()))
    outcome = dm.handle(package(2, "    <Results><CmdID>1</CmdID></Results>", credential=cred()))
    added = [item.uri for command in commands_of(outcome.body, "Add") for item in command.items]
    depths = [uri.count("/") for uri in added]
    assert depths == sorted(depths), f"interior nodes must be parent-first: {added}"


# ---------------------------------------------------------- OMADM-STATUS-418
def test_already_exists_is_not_treated_as_a_failure(dm: DmService) -> None:
    dm.handle(package(1, INIT_BODY, credential=cred()))
    dm.handle(package(2, "    <Results><CmdID>1</CmdID></Results>", credential=cred()))
    body = (
        "    <Status><CmdID>1</CmdID><MsgRef>2</MsgRef><CmdRef>1</CmdRef>"
        "<Cmd>Add</Cmd><Data>418</Data></Status>\n"
        "    <Status><CmdID>2</CmdID><MsgRef>2</MsgRef><CmdRef>2</CmdRef>"
        "<Cmd>Replace</Cmd><Data>200</Data></Status>"
    )
    outcome = dm.handle(package(3, body, credential=cred()))
    assert outcome.metric == "DmSessionComplete"
    assert outcome.detail == "failures:0"


# ------------------------------------------------------ OMADM-ALERT-1223
@pytest.mark.spec
def test_alert_1223_aborts_the_session(dm: DmService, dm_store: MemoryStore) -> None:
    dm.handle(package(1, INIT_BODY, credential=cred()))
    session_key = f"{TEST_IMEI}:42"
    assert dm_store.get_dm_session(session_key) is not None

    body = "    <Alert><CmdID>1</CmdID><Data>1223</Data></Alert>"
    outcome = dm.handle(package(2, body, credential=cred()))
    assert outcome.metric == "DmSessionAborted"
    assert outcome.session_finished is True
    assert dm_store.get_dm_session(session_key) is None
    # An abort must not be answered with more commands.
    assert not commands_of(outcome.body, "Get")
    assert not commands_of(outcome.body, "Replace")


# --------------------------------------------------- OMADM-HDR-MAXMSGSIZE
@pytest.mark.spec
def test_client_max_msg_size_is_honoured(dm: DmService) -> None:
    outcome = dm.handle(package(1, INIT_BODY, credential=cred(), max_msg_size=2048))
    advertised = syncml.parse(outcome.body).header.max_msg_size
    assert advertised == 2048, "the server must not advertise more than the client accepts"


def test_server_limit_applies_when_the_client_asks_for_more(dm: DmService) -> None:
    outcome = dm.handle(package(1, INIT_BODY, credential=cred(), max_msg_size=1_000_000))
    assert syncml.parse(outcome.body).header.max_msg_size == 16384


# ------------------------------------------------ OMADM-HDR-SESSION-BINDING
@pytest.mark.spec
def test_two_devices_with_the_same_session_id_do_not_collide(
    dm: DmService, dm_store: MemoryStore
) -> None:
    """SessionID is chosen by the device and is often a small integer."""
    dm.handle(package(1, INIT_BODY, imei=TEST_IMEI, credential=cred()))
    dm.handle(package(1, INIT_BODY, imei=OTHER_IMEI, credential=cred()))

    first = dm_store.get_dm_session(f"{TEST_IMEI}:42")
    second = dm_store.get_dm_session(f"{OTHER_IMEI}:42")
    assert first is not None and second is not None
    assert first.device_id == TEST_IMEI
    assert second.device_id == OTHER_IMEI


# --------------------------------------------------- OMADM-HDR-ADDRESSING
def test_server_addresses_its_response_to_the_client_source(dm: DmService) -> None:
    outcome = dm.handle(package(1, INIT_BODY, credential=cred()))
    header = syncml.parse(outcome.body).header
    assert header.target == f"IMEI:{TEST_IMEI}"
    assert header.source.endswith("/dm")


# ------------------------------------------------ OMADM-AUTH-NONCE-ROTATION
def test_md5_sessions_carry_a_chal_on_success(dm_store: MemoryStore) -> None:
    from acs.protocol.omadm import auth as dm_auth

    md5_settings = Settings(env="test", dm_auth_scheme="md5", sms_provider="mock")
    service = DmService(md5_settings, dm_store)

    rejected = service.handle(package(1, INIT_BODY))
    assert rejected.metric == "DmAuthRejected"
    session = dm_store.get_dm_session(f"{TEST_IMEI}:42")
    assert session is not None and session.nonce

    credential = dm_auth.md5_credential(TEST_IMSI, DM_PASSWORD, session.nonce)
    payload = package(2, INIT_BODY).replace(
        b"</SyncHdr>",
        (
            "<Cred><Meta>"
            '<Format xmlns="syncml:metinf">b64</Format>'
            '<Type xmlns="syncml:metinf">syncml:auth-md5</Type>'
            f"</Meta><Data>{credential}</Data></Cred></SyncHdr>"
        ).encode(),
    )
    accepted = service.handle(payload)
    assert accepted.metric == "DmInventoryRequested"
    # A Chal keeps the credential exchange alive for the next message.
    assert b"NextNonce" in accepted.body


# ----------------------------------------------------- OMADM-TREE-CLIENT-GET
def test_client_get_is_acknowledged_but_not_answered(dm: DmService) -> None:
    body = (
        "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
        "    <Get><CmdID>2</CmdID><Item>"
        "<Target><LocURI>./DevInfo/Man</LocURI></Target></Item></Get>"
    )
    outcome = dm.handle(package(1, body, credential=cred()))
    assert statuses(outcome.body)["Get"] == syncml.STATUS_OK
    # The server holds no tree of its own, so it returns no Results.
    assert not commands_of(outcome.body, "Results")


# --------------------------------------------------------- RCC14-REQ-POST-BODY
@pytest.mark.spec
def test_otp_can_be_supplied_in_a_post_body(client: TestClient) -> None:
    """An OTP in a query string lands in every proxy log on the way."""
    first = client.get("/config", params=base_query())
    assert first.status_code == 200 and first.content == b""

    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())

    form = {str(k): str(v) for k, v in base_query().items()}
    form["OTP"] = otp
    response = client.post("/config", data=form)
    assert response.status_code == 200
    assert b"wap-provisioningdoc" in response.content
    # The OTP was never in the URL.
    assert "OTP" not in str(response.request.url)


# ------------------------------------------------- RCC14-AUTH-MSISDN-FLOW
def test_msisdn_web_verification_does_not_yet_complete_provisioning(
    client: TestClient,
) -> None:
    """Records a known gap so it cannot be quietly forgotten.

    The page verifies and consumes the OTP but mints no token and stores no
    verified state, so the client's next configuration request is challenged
    again. When that is fixed this test must be replaced by one asserting the
    recovery works, and RCC14-AUTH-MSISDN-FLOW moved to implemented.
    """
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    submitted = client.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": csrf})
    csrf2 = submitted.text.split('name="csrf" value="')[1].split('"')[0]

    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())
    verified = client.post(
        "/msisdn/verify", data={"msisdn": TEST_MSISDN, "otp": otp, "csrf": csrf2}
    )
    assert verified.status_code == 200

    # The gap: a configuration request now gets a fresh challenge, not a document.
    follow_up = client.get("/config", params=base_query(msisdn=TEST_MSISDN))
    assert follow_up.content == b"", "if this now returns a document, the gap is closed"


# ------------------------------------------------------------ RCC14-PRIV-TLS
def test_responses_request_transport_security(client: TestClient) -> None:
    response = client.get("/healthz")
    assert "max-age" in response.headers["Strict-Transport-Security"]


# ---------------------------------------------------------------- coverage
def test_dm_status_codes_emitted_cover_the_registry_claims(dm: DmService) -> None:
    """Guard against declaring a status constant that is never sent.

    The server previously defined ten status codes it never emitted. Any code the
    registry claims as implemented must be produced by one of the exchanges above.
    """
    emitted: set[str] = set()

    emitted.update(statuses(dm.handle(package(1, INIT_BODY)).body).values())
    emitted.update(statuses(dm.handle(package(1, INIT_BODY, credential=cred())).body).values())
    refused = (
        "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
        "    <Delete><CmdID>2</CmdID><Item>"
        "<Target><LocURI>./DevInfo/Man</LocURI></Target></Item></Delete>\n"
        "    <Replace><CmdID>3</CmdID><Item>"
        "<Target><LocURI>./Nope/Nothing</LocURI></Target><Data>x</Data></Item></Replace>"
    )
    emitted.update(statuses(dm.handle(package(2, refused, credential=cred())).body).values())
    emitted.update(
        statuses(dm.handle(package(1, INIT_BODY, credential=cred(password="no"))).body).values()
    )

    for code in (
        syncml.STATUS_OK,
        syncml.STATUS_AUTH_ACCEPTED,
        syncml.STATUS_NOT_FOUND,
        syncml.STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED,
        syncml.STATUS_MISSING_CREDENTIALS,
        syncml.STATUS_INVALID_CREDENTIALS,
    ):
        assert code in emitted, f"status {code} is claimed but never emitted"


def test_dm_endpoint_still_completes_a_full_session(
    settings: Settings, dm_store: MemoryStore
) -> None:
    """The conformance fixes must not have broken the happy path."""
    with TestClient(create_app(settings, dm_store)) as http:
        response = http.post(
            "/dm",
            content=package(1, INIT_BODY, credential=cred()),
            headers={"Content-Type": "application/vnd.syncml.dm+xml"},
        )
        assert response.status_code == 200
        assert commands_of(response.content, "Get")
