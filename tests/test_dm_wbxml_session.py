"""WBXML HTTP sessions share XML authentication, inventory and state handling."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from copy import deepcopy
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_dm_session import (
    DEVICE_VALUES,
    DM_PASSWORD,
    PACKAGE_1_BODY,
    SESSION_KEY,
    basic_cred,
    package,
)
from tests.test_dm_wbxml import HEADER, INVALID_DOCUMENTS, expansion_bomb

from acs.api.dm import MAX_BODY_BYTES, dm_session
from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import auth as dm_auth
from acs.protocol.omadm import syncml, wbxml
from acs.protocol.omadm.session import DmService
from acs.store.memory import MemoryStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def dm_store(store: MemoryStore) -> MemoryStore:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    return store


@pytest.fixture
async def dm_app(settings: Settings, dm_store: MemoryStore) -> AsyncIterator[FastAPI]:
    app = create_app(settings, dm_store)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def dm_client(dm_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dm_app), base_url="http://testserver"
    ) as client:
        yield client


async def exchange(
    client: httpx.AsyncClient, xml: bytes, binary: bool = True
) -> syncml.SyncMlMessage:
    content_type = syncml.CONTENT_TYPE_WBXML if binary else syncml.CONTENT_TYPE_XML
    response = await client.post(
        "/dm",
        content=wbxml.encode(xml) if binary else xml,
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    return syncml.parse(wbxml.decode(response.content) if binary else response.content)


@pytest.mark.spec
@pytest.mark.parametrize(
    "encodings",
    [(True, True, True), (True, False, True), (False, True, False)],
    ids=["wbxml", "wbxml-xml-wbxml", "xml-wbxml-xml"],
)
async def test_wbxml_requests_preserve_state_through_a_complete_dm_session(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, encodings: tuple[bool, bool, bool]
) -> None:
    first = await exchange(dm_client, package(1, PACKAGE_1_BODY, cred=basic_cred()), encodings[0])
    assert first.of("Status")[0].data == syncml.STATUS_AUTH_ACCEPTED
    uris = [item.uri for command in first.of("Get") for item in command.items]
    assert "./DevDetail/SwV" in uris
    session = dm_store.get_dm_session(SESSION_KEY)
    assert session is not None and session.phase == "devinfo"
    assert session.last_msg_id == 1

    items = "".join(
        f"<Item><Source><LocURI>{uri}</LocURI></Source>"
        f"<Data>{DEVICE_VALUES.get(uri, 'value')}</Data></Item>"
        for uri in uris
    )
    results = f"<Results><CmdID>1</CmdID><MsgRef>1</MsgRef><CmdRef>2</CmdRef>{items}</Results>"
    second = await exchange(dm_client, package(2, results, cred=basic_cred()), encodings[1])
    assert second.of("Add")
    pushed = {item.uri: item.data for command in second.of("Replace") for item in command.items}
    assert pushed["./3GPP_IMS/1/Private_User_Identity"].startswith(TEST_IMSI)
    assert pushed["./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN"] == "3"
    session = dm_store.get_dm_session(SESSION_KEY)
    assert session is not None and session.phase == "configure"
    assert session.last_msg_id == 2

    status = (
        "<Status><CmdID>1</CmdID><MsgRef>2</MsgRef><CmdRef>2</CmdRef>"
        "<Cmd>Replace</Cmd><Data>200</Data></Status>"
    )
    final = await exchange(dm_client, package(3, status, cred=basic_cred()), encodings[2])
    assert final.final
    assert not final.of("Get", "Add", "Replace")
    assert dm_store.get_dm_session(SESSION_KEY) is None
    device = dm_store.get_device(TEST_IMEI)
    assert device is not None and device.sw_version == "SIM-1.0"
    assert device.imsi == TEST_IMSI


async def test_six_thousand_replace_commands_receive_the_same_response_in_xml_and_wbxml(
    settings: Settings, dm_store: MemoryStore
) -> None:
    body = "<Alert><CmdID>1</CmdID><Data>1201</Data></Alert>" + "".join(
        f"<Replace><CmdID>{i}</CmdID></Replace>" for i in range(2, 6002)
    )
    xml = package(1, body, cred=basic_cred())
    wire = wbxml.encode(xml)
    assert len(wire) < 64 * 1024
    # The equivalent XML request is larger than the default 64 KiB body cap.
    large_settings = settings.model_copy(update={"dm_max_msg_size": 128 * 1024})
    subscriber = dm_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    responses: list[bytes] = []
    for payload, content_type in (
        (xml, syncml.CONTENT_TYPE_XML),
        (wire, syncml.CONTENT_TYPE_WBXML),
    ):
        store = MemoryStore()
        store.put_subscriber(deepcopy(subscriber))
        app = create_app(large_settings, store)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client,
        ):
            response = await client.post(
                "/dm", content=payload, headers={"Content-Type": content_type}
            )
        assert response.status_code == 200
        assert response.headers["content-type"] == content_type
        session = store.get_dm_session(SESSION_KEY)
        assert session is not None and session.phase == "devinfo"
        assert session.authenticated and session.last_msg_id == 1
        assert store.get_device(TEST_IMEI) is not None
        responses.append(response.content)

    message = syncml.parse(responses[0])
    statuses = message.of("Status")
    assert len(statuses) == 6002
    assert all(status.data == "200" for status in statuses[1:])
    assert message.of("Get") and message.final
    assert responses[1] == wbxml.encode(responses[0])


@pytest.mark.parametrize(
    "stage", ["challenge", "init", "devinfo", "configure", "abort", "end", "rejected"]
)
def test_wbxml_encoding_failure_preserves_session_and_device_state(
    settings: Settings,
    dm_store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    service = DmService(settings, dm_store)
    body = PACKAGE_1_BODY
    credential = basic_cred()
    msg_id = 1
    if stage == "challenge":
        credential = None
    elif stage != "init":
        assert service.handle(package(1, PACKAGE_1_BODY, cred=basic_cred())).status_code == 200
        msg_id = 2
        body = (
            "<Results><CmdID>1</CmdID><Item><Source><LocURI>./DevDetail/SwV</LocURI>"
            "</Source><Data>updated</Data></Item></Results>"
        )
        if stage == "configure":
            assert service.handle(package(2, body, cred=basic_cred())).status_code == 200
            body = "<Status><CmdID>1</CmdID><Data>200</Data></Status>"
            msg_id = 3
        elif stage in ("abort", "end"):
            code = "1223" if stage == "abort" else "1226"
            body = f"<Alert><CmdID>1</CmdID><Data>{code}</Data></Alert>"
        elif stage == "rejected":
            credential = basic_cred(password="wrong")

    before_session = deepcopy(dm_store.get_dm_session(SESSION_KEY))
    before_device = deepcopy(dm_store.get_device(TEST_IMEI))
    wire = wbxml.encode(package(msg_id, body, cred=credential))
    with monkeypatch.context() as patch:
        patch.setattr(wbxml, "MAX_ENCODE_BYTES", 1)
        outcome = service.handle(wire, syncml.CONTENT_TYPE_WBXML)

    assert outcome.status_code == 500
    assert outcome.metric == "DmEncodingError"
    assert outcome.detail == "response_encoding_failed"
    assert not outcome.body and not outcome.session_finished
    assert dm_store.get_dm_session(SESSION_KEY) == before_session
    assert dm_store.get_device(TEST_IMEI) == before_device
    assert "dm response encoding failure" in caplog.messages
    assert "dm parse failure" not in caplog.messages
    assert service.handle(wire, syncml.CONTENT_TYPE_WBXML).status_code == 200


@pytest.mark.spec
@pytest.mark.parametrize("version", [b"\x02", b"\x03"])
async def test_server_wbxml_response_matches_independent_header_status_and_metinf_bytes(
    dm_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, version: bytes
) -> None:
    monkeypatch.setattr(dm_auth, "make_nonce", lambda: "bm9uY2U=")
    # The public SyncML/MetInf token tables define this vector independently of
    # the codec, so matching encode/decode bugs cannot make the assertion pass.
    wire = (
        version + b"\xa4\x01\x6a\x00"
        b"\x6d\x6c\x65\x031\x00\x01\x5b\x031\x00\x01\x01"
        b"\x6b\x46\x4b\x031\x00\x01\x4f\x031201\x00\x01\x01\x12\x01\x01"
    )
    response = await dm_client.post(
        "/dm", content=wire, headers={"Content-Type": "application/vnd.syncml.dm+wbxml"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.syncml.dm+wbxml"
    assert response.content.startswith(version + b"\xa4\x01\x6a\x00\x6d\x6c\x71\x031.2\x00\x01")
    assert b"\x5a\x00\x01\x4c\x0316384\x00\x01\x01\x01" in response.content
    assert (
        b"\x00\x00\x6b\x69\x4b\x031\x00\x01\x5c\x031\x00\x01"
        b"\x4c\x030\x00\x01\x4a\x03SyncHdr\x00\x01"
    ) in response.content
    assert (
        b"\x49\x5a\x00\x01\x47\x03b64\x00\x01"
        b"\x53\x03syncml:auth-basic\x00\x01\x50\x03bm9uY2U=\x00\x01\x01\x01"
    ) in response.content
    assert response.content.endswith(b"\x00\x00\x4f\x03407\x00\x01\x01\x12\x01\x01")
    assert re.search(rb"\x03[ \t\r\n]+\x00", response.content) is None


@pytest.mark.spec
async def test_server_commands_match_independent_compact_wbxml_tokens(
    dm_client: httpx.AsyncClient,
) -> None:
    responses: list[bytes] = []
    for msg_id, body in ((1, PACKAGE_1_BODY), (2, ""), (3, "")):
        response = await dm_client.post(
            "/dm",
            content=wbxml.encode(package(msg_id, body, cred=basic_cred())),
            headers={"Content-Type": "application/vnd.syncml.dm+wbxml"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.syncml.dm+wbxml"
        assert re.search(rb"\x03[ \t\r\n]+\x00", response.content) is None
        responses.append(response.content)

    # Public SyncML page 0 tokens with content: Get 53, Add 45, Replace 60,
    # Item 54, Target 6E, LocURI 57. Expected bytes do not use the codec tables.
    assert (b"\x53\x4b\x034\x00\x01\x54\x6e\x57\x03./DevInfo/DevId\x00\x01\x01\x01") in responses[0]
    assert (
        b"\x45\x4b\x032\x00\x01\x54\x6e\x57\x03./3GPP_IMS\x00\x01\x01"
        b"\x5a\x00\x01\x47\x03node\x00\x01\x53\x03node\x00\x01\x01\x01"
    ) in responses[1]
    assert (
        b"\x00\x00\x60\x4b\x033\x00\x01\x54\x6e\x57"
        b"\x03./3GPP_IMS/1/Private_User_Identity\x00\x01\x01"
        b"\x5a\x00\x01\x47\x03chr\x00\x01"
    ) in responses[1]
    assert responses[2].endswith(b"\x12\x01\x01")


@pytest.mark.spec
@pytest.mark.parametrize(
    ("credential", "code"),
    [(None, "407"), (basic_cred(password="bad"), "401")],
    ids=["missing", "wrong"],
)
async def test_wbxml_authentication_failures_return_wbxml_challenges(
    dm_client: httpx.AsyncClient, credential: str | None, code: str
) -> None:
    message = await exchange(dm_client, package(1, PACKAGE_1_BODY, cred=credential))
    assert message.of("Status")[0].data == code
    assert not message.of("Get", "Replace")


@pytest.mark.spec
@pytest.mark.parametrize("password", [DM_PASSWORD, "wrong"], ids=["valid", "wrong"])
async def test_wbxml_md5_credentials_are_checked_against_the_stored_nonce(
    settings: Settings, dm_store: MemoryStore, password: str
) -> None:
    app = create_app(settings.model_copy(update={"dm_auth_scheme": "md5"}), dm_store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        response = await client.post(
            "/dm",
            content=wbxml.encode(package(1, PACKAGE_1_BODY)),
            headers={"Content-Type": syncml.CONTENT_TYPE_WBXML},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == syncml.CONTENT_TYPE_WBXML
        root = ElementTree.fromstring(wbxml.decode(response.content))
        nonce = root.findtext(".//{syncml:metinf}NextNonce")
        session = dm_store.get_dm_session(SESSION_KEY)
        assert session is not None and session.nonce == nonce
        assert nonce
        credential = dm_auth.md5_credential(TEST_IMSI, password, nonce)
        xml = package(2, PACKAGE_1_BODY, cred=credential, auth_type=syncml.AUTH_MD5)
        # Authentication covers the credential and nonce, not the serialized body.
        response = await client.post(
            "/dm",
            content=wbxml.encode(xml, use_string_table=True, opaque=True),
            headers={"Content-Type": syncml.CONTENT_TYPE_WBXML},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == syncml.CONTENT_TYPE_WBXML
        message = syncml.parse(wbxml.decode(response.content))
        assert message.of("Status")[0].data == ("212" if password == DM_PASSWORD else "401")
        assert bool(message.of("Get")) is (password == DM_PASSWORD)


@pytest.mark.spec
@pytest.mark.parametrize("binary", [False, True], ids=["xml", "wbxml"])
@pytest.mark.parametrize("auth_type", [syncml.AUTH_BASIC, syncml.AUTH_MD5])
async def test_non_ascii_credentials_are_rejected_in_both_encodings(
    dm_client: httpx.AsyncClient, binary: bool, auth_type: str
) -> None:
    await exchange(dm_client, package(1, PACKAGE_1_BODY), binary)
    credential = basic_cred(password="틀림") if auth_type == syncml.AUTH_BASIC else "틀림"
    message = await exchange(
        dm_client,
        package(2, PACKAGE_1_BODY, cred=credential, auth_type=auth_type),
        binary,
    )
    assert message.of("Status")[0].data == syncml.STATUS_INVALID_CREDENTIALS
    assert not message.of("Get", "Replace")


@pytest.mark.spec
@pytest.mark.parametrize("version", [wbxml.VERSION_12, wbxml.VERSION_13])
async def test_wbxml_media_type_parameters_preserve_the_requested_binary_version(
    dm_client: httpx.AsyncClient, version: int
) -> None:
    response = await dm_client.post(
        "/dm",
        content=wbxml.encode(package(1, PACKAGE_1_BODY, cred=basic_cred()), version=version),
        headers={"Content-Type": "Application/Vnd.Syncml.Dm+Wbxml; charset=UTF-8"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == syncml.CONTENT_TYPE_WBXML
    assert response.content[0] == version
    assert syncml.parse(wbxml.decode(response.content)).of("Get")


@pytest.mark.spec
@pytest.mark.parametrize("code", ["1223", "1226"])
async def test_wbxml_can_abort_or_end_a_session(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, code: str
) -> None:
    await exchange(dm_client, package(1, PACKAGE_1_BODY, cred=basic_cred()))
    message = await exchange(
        dm_client,
        package(2, f"<Alert><CmdID>1</CmdID><Data>{code}</Data></Alert>", cred=basic_cred()),
    )
    assert message.final
    assert not message.of("Get", "Replace")
    assert dm_store.get_dm_session(SESSION_KEY) is None


@pytest.mark.parametrize(("wire", "reason"), INVALID_DOCUMENTS)
async def test_bad_wbxml_returns_400_without_creating_a_session(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, wire: bytes, reason: str
) -> None:
    response = await dm_client.post(
        "/dm", content=wire, headers={"Content-Type": syncml.CONTENT_TYPE_WBXML}
    )
    assert response.status_code == 400
    assert response.content == b""
    assert dm_store.get_dm_session(SESSION_KEY) is None


@pytest.mark.parametrize(
    "wire",
    [
        HEADER + b"\x6d" * (wbxml.MAX_DEPTH + 1),
        HEADER + b"\x6d" + b"\x12" * wbxml.MAX_ELEMENTS,
        expansion_bomb(),
    ],
)
async def test_wbxml_resource_limits_return_400_over_http(
    dm_client: httpx.AsyncClient, wire: bytes
) -> None:
    response = await dm_client.post(
        "/dm", content=wire, headers={"Content-Type": syncml.CONTENT_TYPE_WBXML}
    )
    assert response.status_code == 400
    assert response.content == b""


@pytest.mark.parametrize("size", [16384 * 4 + 1, MAX_BODY_BYTES + 1])
async def test_oversized_wbxml_returns_413_over_http(
    dm_client: httpx.AsyncClient, size: int
) -> None:
    response = await dm_client.post(
        "/dm", content=b"x" * size, headers={"Content-Type": syncml.CONTENT_TYPE_WBXML}
    )
    assert response.status_code == 413


@pytest.mark.parametrize("field", ["MsgID", "CmdID", "MaxMsgSize"])
async def test_oversized_wbxml_decimal_fields_return_400(
    dm_client: httpx.AsyncClient, field: str
) -> None:
    xml = package(1, PACKAGE_1_BODY, cred=basic_cred())
    old = f">{'16384' if field == 'MaxMsgSize' else '1'}</{field}>".encode()
    new = f">{'9' * 5000}</{field}>".encode()
    assert old in xml
    response = await dm_client.post(
        "/dm",
        content=wbxml.encode(xml.replace(old, new)),
        headers={"Content-Type": syncml.CONTENT_TYPE_WBXML},
    )
    assert response.status_code == 400


async def test_wbxml_errors_do_not_echo_device_data(
    dm_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    table = TEST_IMSI.encode() + b"\x00"
    response = await dm_client.post(
        "/dm",
        content=b"\x03\x00\x00\x6a" + bytes([len(table)]) + table + b"\x2d",
        headers={"Content-Type": syncml.CONTENT_TYPE_WBXML},
    )
    assert response.status_code == 400
    assert TEST_IMSI not in response.text
    assert TEST_IMSI not in caplog.text
    assert all(TEST_IMSI not in str(record.__dict__) for record in caplog.records)


async def test_the_dm_route_stops_reading_as_soon_as_the_body_limit_is_exceeded(
    dm_app: FastAPI,
) -> None:
    calls = 0

    async def receive() -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert calls <= 2, "the route must not consume the rest of an oversized request"
        return {
            "type": "http.request",
            "body": b"x" * MAX_BODY_BYTES if calls == 1 else b"x",
            "more_body": True,
        }

    request = Request({"type": "http", "app": dm_app}, receive)
    response = await dm_session(request)
    assert response.status_code == 413
    assert calls == 2
