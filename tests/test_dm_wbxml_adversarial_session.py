"""Differential DM sessions and error-state isolation across wire encodings."""

from __future__ import annotations

import base64
import random
import re
import time
from copy import deepcopy
from xml.sax.saxutils import escape

import httpx
import pytest
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_dm_session import DM_PASSWORD, SESSION_KEY, basic_cred, package
from tests.test_dm_wbxml import SMALL_WBXML
from tests.test_dm_wbxml_adversarial import _deadline

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import auth as dm_auth
from acs.protocol.omadm import syncml, wbxml
from acs.protocol.omadm.session import DmService
from acs.store.memory import MemoryStore

pytestmark = pytest.mark.spec


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _store() -> MemoryStore:
    store = MemoryStore()
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    return store


def _item(uri: str, value: str) -> str:
    return (
        f"<Item><Source><LocURI>{escape(uri)}</LocURI></Source>"
        f"<Data>{escape(value).replace(chr(13), '&#13;')}</Data></Item>"
    )


@pytest.mark.parametrize(
    ("version", "namespace", "mode"),
    [
        (2, "SYNCML:SYNCML1.1", "inline"),
        (2, "SYNCML:SYNCML1.2", "table"),
        (3, "SYNCML:SYNCML1.1", "opaque"),
        (3, "SYNCML:SYNCML1.2", "inline"),
    ],
)
@pytest.mark.parametrize("auth_scheme", ["basic", "md5"])
@pytest.mark.parametrize(
    "path", ["finish", "finish-errors", "abort-init", "abort-configure", "end", "auth-retry"]
)
def test_xml_and_wbxml_agree_on_each_transition_and_persisted_inventory(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    namespace: str,
    mode: str,
    auth_scheme: str,
    path: str,
) -> None:
    frozen_time = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen_time)
    stores = [_store(), _store()]
    config = settings.model_copy(update={"dm_auth_scheme": auth_scheme})
    services = [DmService(config, store) for store in stores]
    message_id = 0
    manufacturer = " 한글 제조사 📱 & <테스트> "
    model = "모델🙂" * 512
    software = " v2\r\n설정 & <새 버전> "
    init = (
        "<Alert><CmdID>1</CmdID><Data>1201</Data></Alert>"
        "<Replace><CmdID>2</CmdID>"
        + _item("./DevInfo/Man", manufacturer)
        + _item("./DevInfo/Mod", model)
        + "</Replace>"
    )
    results = (
        "<Results><CmdID>2</CmdID><MsgRef>1</MsgRef><CmdRef>2</CmdRef>"
        + _item("./DevDetail/SwV", software)
        + _item("./DevInfo/DmV", "1.2")
        + _item("./DevInfo/Lang", "ko-KR")
        + _item("./3GPP_IMS/1/Timer_T1", "9999")
        + _item("./Unknown/Node", "ignored")
        + "</Results>"
    )

    def exchange(body: str, metric: str, credential_kind: str = "valid") -> syncml.SyncMlMessage:
        nonlocal message_id
        message_id += 1
        next_nonce = base64.b64encode(f"qa-message-{message_id}".encode()).decode()
        monkeypatch.setattr(dm_auth, "make_nonce", lambda: next_nonce)
        credential: str | None = None
        auth_type = syncml.AUTH_MD5 if auth_scheme == "md5" else syncml.AUTH_BASIC
        if credential_kind != "missing":
            password = "틀린 비밀번호" if credential_kind == "wrong" else DM_PASSWORD
            if auth_scheme == "md5":
                session = stores[0].get_dm_session(SESSION_KEY)
                assert session is not None
                credential = dm_auth.md5_credential(TEST_IMSI, password, session.nonce)
            else:
                credential = basic_cred(password=password)
        xml = package(message_id, body, cred=credential, auth_type=auth_type)
        xml = xml.replace(b"SYNCML:SYNCML1.2", namespace.encode())
        wire = wbxml.encode(
            xml,
            version=version,
            public_id=f"-//SYNCML//DTD SyncML {namespace[-3:]}//EN",
            use_string_table=mode == "table",
            opaque=mode == "opaque",
        )
        with _deadline():
            plain = services[0].handle(xml, syncml.CONTENT_TYPE_XML)
            binary = services[1].handle(wire, syncml.CONTENT_TYPE_WBXML)
        assert plain.status_code == binary.status_code == 200
        assert plain.metric == binary.metric == metric
        assert plain.detail == binary.detail
        assert plain.session_finished == binary.session_finished
        assert plain.headers == binary.headers
        assert plain.content_type == syncml.CONTENT_TYPE_XML
        assert binary.content_type == syncml.CONTENT_TYPE_WBXML
        assert binary.body[0] == version
        # These responses have no whitespace-only data, so every such STR_I
        # would be formatting leaking into an element-only content position.
        assert re.search(rb"\x03[ \t\r\n]+\x00", binary.body) is None
        plain_message = syncml.parse(plain.body)
        binary_message = syncml.parse(wbxml.decode(binary.body))
        assert plain_message == binary_message
        assert stores[0].get_dm_session(SESSION_KEY) == stores[1].get_dm_session(SESSION_KEY)
        assert stores[0].get_device(TEST_IMEI) == stores[1].get_device(TEST_IMEI)
        assert stores[0].get_subscriber(TEST_IMSI) == stores[1].get_subscriber(TEST_IMSI)
        return plain_message

    missing = exchange(init, "DmAuthRejected", "missing")
    assert missing.of("Status")[0].data == "407"
    rejected = exchange(init, "DmAuthRejected", "wrong")
    assert rejected.of("Status")[0].data == "401"
    assert stores[0].get_device(TEST_IMEI) is None
    first = exchange(init, "DmInventoryRequested")
    assert first.of("Get") and first.of("Status")[0].data == "212"
    session = stores[0].get_dm_session(SESSION_KEY)
    device = stores[0].get_device(TEST_IMEI)
    assert session is not None and session.phase == "devinfo" and session.authenticated
    assert device is not None and device.manufacturer == manufacturer.strip()
    assert device.model == model and device.imsi == TEST_IMSI

    if path == "auth-retry":
        before = deepcopy(device)
        rejected = exchange(results, "DmAuthRejected", "wrong")
        assert rejected.of("Status")[0].data == "401"
        assert not rejected.of("Get", "Add", "Replace")
        assert stores[0].get_device(TEST_IMEI) == before
        session = stores[0].get_dm_session(SESSION_KEY)
        assert session is not None and session.phase == "devinfo" and not session.authenticated

    if path != "abort-init":
        second = exchange(results, "DmConfigPushed")
        assert second.of("Add") and second.of("Replace")
        session = stores[0].get_dm_session(SESSION_KEY)
        device = stores[0].get_device(TEST_IMEI)
        assert session is not None and session.phase == "configure" and session.authenticated
        assert device is not None and device.sw_version == software.strip()
        assert device.mo_values["./DevInfo/Lang"] == "ko-KR"
        assert "./Unknown/Node" not in device.mo_values
        assert "./3GPP_IMS/1/Timer_T1" not in device.mo_values

    before_finish = deepcopy(stores[0].get_device(TEST_IMEI))
    if path.startswith("abort"):
        final = exchange(
            "<Alert><CmdID>1</CmdID><Data>1223</Data></Alert>" + results,
            "DmSessionAborted",
        )
    elif path == "end":
        final = exchange(
            "<Alert><CmdID>1</CmdID><Data>1226</Data></Alert>" + results,
            "DmSessionEnded",
        )
    else:
        code = "500" if path == "finish-errors" else "200"
        metric = "DmSessionCompleteWithErrors" if path == "finish-errors" else "DmSessionComplete"
        final = exchange(
            "<Status><CmdID>1</CmdID><MsgRef>2</MsgRef><CmdRef>3</CmdRef>"
            f"<Cmd>Replace</Cmd><Data>{code}</Data></Status>",
            metric,
        )
    assert final.final and not final.of("Get", "Add", "Replace")
    assert stores[0].get_dm_session(SESSION_KEY) is None
    assert stores[0].get_device(TEST_IMEI) == before_finish


@pytest.mark.anyio
async def test_mutated_binary_requests_return_http_errors_without_changing_existing_state(
    settings: Settings,
) -> None:
    store = _store()
    service = DmService(settings, store)
    init = "<Alert><CmdID>1</CmdID><Data>1201</Data></Alert>"
    assert service.handle(package(1, init, cred=basic_cred())).status_code == 200
    before_session = deepcopy(store.get_dm_session(SESSION_KEY))
    before_device = deepcopy(store.get_device(TEST_IMEI))
    app = create_app(settings, store)
    rng = random.Random(192)
    rejected = 0
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://qa.test"
        ) as client,
    ):
        candidates = [SMALL_WBXML[:index] for index in range(len(SMALL_WBXML))]
        candidates.extend(
            SMALL_WBXML[:index] + bytes([SMALL_WBXML[index] ^ 255]) + SMALL_WBXML[index + 1 :]
            for index in range(len(SMALL_WBXML))
        )
        candidates.extend(rng.randbytes(rng.randrange(1024)) for _ in range(100))
        for candidate in candidates:
            try:
                wbxml.decode(candidate)
            except wbxml.WbxmlError:
                rejected += 1
                with _deadline():
                    response = await client.post(
                        "/dm",
                        content=candidate,
                        headers={"Content-Type": syncml.CONTENT_TYPE_WBXML},
                    )
                assert response.status_code == 400 and response.content == b""
                assert store.get_dm_session(SESSION_KEY) == before_session
                assert store.get_device(TEST_IMEI) == before_device
                assert len(store.list_devices()) == 1
        assert rejected >= 100
