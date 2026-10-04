"""Run the WBXML CLI against the real DM service through an HTTP test transport."""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_dm_session import DM_PASSWORD, SESSION_KEY
from tools import dm_client_sim

from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import syncml, wbxml
from acs.protocol.omadm.session import DmService
from acs.store.memory import MemoryStore


@pytest.mark.spec
@pytest.mark.parametrize("scheme", ["basic", "md5"])
def test_the_wbxml_cli_drives_an_authenticated_session(
    settings: Settings, store: MemoryStore, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    service = DmService(settings.model_copy(update={"dm_auth_scheme": scheme}), store)
    sent = 0
    received: dict[str, str] = {}
    client_class = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        assert request.headers["content-type"] == syncml.CONTENT_TYPE_WBXML
        assert request.headers["accept"] == syncml.CONTENT_TYPE_WBXML
        assert request.content.startswith(b"\x03\xa4\x01")
        outcome = service.handle(request.content, request.headers["content-type"])
        assert outcome.status_code == 200
        assert outcome.content_type == syncml.CONTENT_TYPE_WBXML
        message = syncml.parse(wbxml.decode(outcome.body))
        received.update(
            (item.uri, item.data) for command in message.of("Replace") for item in command.items
        )
        return httpx.Response(
            outcome.status_code,
            content=outcome.body,
            headers={"Content-Type": outcome.content_type},
        )

    def make_client(*, timeout: float, verify: bool) -> httpx.Client:
        return client_class(transport=httpx.MockTransport(handle), timeout=timeout, verify=verify)

    monkeypatch.setattr(dm_client_sim.httpx, "Client", make_client)
    assert (
        dm_client_sim.main(
            [
                "--imsi",
                TEST_IMSI,
                "--imei",
                TEST_IMEI,
                "--password",
                DM_PASSWORD,
                "--auth",
                scheme,
                "--wbxml",
            ]
        )
        == 0
    )
    assert sent == (4 if scheme == "md5" else 3)
    assert received["./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN"] == "3"
    assert dm_client_sim.DM_WBXML_CONTENT_TYPE == syncml.CONTENT_TYPE_WBXML
    assert store.get_dm_session(SESSION_KEY) is None
    device = store.get_device(TEST_IMEI)
    assert device is not None and device.sw_version == "SIM-1.0"


def test_the_wbxml_cli_fails_if_the_server_replies_with_xml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_class = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<SyncML/>", headers={"Content-Type": syncml.CONTENT_TYPE_XML}
        )

    def make_client(*, timeout: float, verify: bool) -> httpx.Client:
        return client_class(transport=httpx.MockTransport(handle), timeout=timeout, verify=verify)

    monkeypatch.setattr(dm_client_sim.httpx, "Client", make_client)
    assert dm_client_sim.main(["--password", DM_PASSWORD, "--wbxml"]) == 1
    output = capsys.readouterr().out
    assert "requested WBXML content type" in output
    assert DM_PASSWORD not in output
