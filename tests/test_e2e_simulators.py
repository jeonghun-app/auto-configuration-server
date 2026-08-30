"""End-to-end verification: the two client simulators against a live server.

These run a real uvicorn process in a thread, so they exercise the actual ASGI
stack, real sockets and the real simulators — the same code paths used to verify
a deployment. The RCS run also harvests the OMA-DM password from the ``w7``
characteristic and feeds it to the DM simulator, proving the CP-to-DM bridge.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from xml.etree import ElementTree

import httpx
import pytest
import uvicorn
from tools.dm_client_sim import Checker as DmChecker
from tools.dm_client_sim import DmClientSimulator, run_session
from tools.rcs_client_sim import Checker, RcsClientSimulator, scenario_disabled, scenario_full

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.store.memory import MemoryStore

TEST_IMSI = "001010000000001"
TEST_MSISDN = "+821012345678"
TEST_IMEI = "356938035643809"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LiveServer:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.port = free_port()
        settings = Settings(
            env="dev",
            store_backend="memory",
            sms_provider="mock",
            dev_endpoints_enabled=True,
            admin_token="e2e-admin",
            dm_auth_scheme="basic",
            dm_account_uri=f"http://127.0.0.1:{self.port}/dm",
            log_level="WARNING",
        )
        config = uvicorn.Config(
            create_app(settings, store),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("server did not become healthy")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def live_store() -> MemoryStore:
    store = MemoryStore()
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, rcs_profile="UP_2.4"))
    store.put_subscriber(Subscriber(imsi="001010000000009", msisdn="+821000000009", entitled=False))
    return store


@pytest.fixture(scope="module")
def server(live_store: MemoryStore) -> Iterator[LiveServer]:
    live = LiveServer(live_store)
    live.start()
    try:
        yield live
    finally:
        live.stop()


def make_sim(server: LiveServer, imsi: str = TEST_IMSI) -> RcsClientSimulator:
    return RcsClientSimulator(base_url=server.base_url, imsi=imsi, imei=TEST_IMEI, profile="UP_2.4")


def test_full_rcs_provisioning_scenario_passes_every_check(server: LiveServer) -> None:
    checker = Checker()
    sim = make_sim(server)
    try:
        scenario_full(sim, checker, TEST_MSISDN)
    finally:
        sim.close()
    assert checker.failures == []
    assert checker.passed >= 14


def test_disabled_subscriber_scenario(server: LiveServer, live_store: MemoryStore) -> None:
    subscriber = live_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    subscriber.forced_vers = -2
    live_store.put_subscriber(subscriber)
    live_store.revoke_tokens_for_imsi(TEST_IMSI)
    live_store.delete_otp(TEST_MSISDN)

    sim = make_sim(server)
    sim.msisdn = TEST_MSISDN
    checker = Checker()
    try:
        # Re-authenticate first: the disable document is only served to an
        # identified subscriber.
        response = sim.request_configuration()
        assert response.status_code == 200
        otp = sim.fetch_otp(TEST_MSISDN)
        assert otp
        response = sim.request_configuration(OTP=otp)
        root = sim.parse_document(response.content)
        version = sim.apply(root)
        checker.check(version == -2, "operator disable value delivered")
        checker.check(sim.state["phase"] == "disabled", "client entered the disabled state")
    finally:
        sim.close()
        subscriber.forced_vers = None
        live_store.put_subscriber(subscriber)
        live_store.delete_otp(TEST_MSISDN)
    assert checker.failures == []


def test_not_entitled_subscriber_is_refused(server: LiveServer) -> None:
    sim = make_sim(server, imsi="001010000000009")
    checker = Checker()
    try:
        scenario_disabled(sim, checker)
    finally:
        sim.close()
    assert checker.failures == []


def test_oma_dm_session_uses_the_password_bootstrapped_by_oma_cp(
    server: LiveServer, live_store: MemoryStore
) -> None:
    # 1. RCS provisioning, which emits the w7 DM account.
    live_store.delete_otp(TEST_MSISDN)
    sim = make_sim(server)
    try:
        sim.request_configuration()
        otp = sim.fetch_otp(TEST_MSISDN)
        assert otp
        response = sim.request_configuration(OTP=otp)
        root = sim.parse_document(response.content)
        dm_account = sim.application(root, "w7")
        assert dm_account is not None, "no OMA-DM account in the configuration document"
        secret = dm_account.find("parm[@name='AAUTHSECRET']")
        username = dm_account.find("parm[@name='AAUTHNAME']")
        assert secret is not None and username is not None
        dm_password = secret.get("value") or ""
        assert dm_password
        assert username.get("value") == TEST_IMSI
    finally:
        sim.close()

    # 2. Use exactly those credentials for a full OMA-DM session.
    dm_sim = DmClientSimulator(
        base_url=server.base_url,
        imsi=TEST_IMSI,
        imei=TEST_IMEI,
        password=dm_password,
        auth="basic",
    )
    dm_checker = DmChecker()
    try:
        run_session(dm_sim, dm_checker)
    finally:
        dm_sim.close()
    assert dm_checker.failures == []
    assert "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" in dm_sim.received


def test_dm_session_with_a_wrong_password_never_reaches_configuration(
    server: LiveServer,
) -> None:
    dm_sim = DmClientSimulator(
        base_url=server.base_url,
        imsi=TEST_IMSI,
        imei=TEST_IMEI,
        password="definitely-wrong",
        auth="basic",
    )
    try:
        root, body = dm_sim._envelope()
        dm_sim._add_alert(body, 1, "1201")
        ElementTree.SubElement(body, "Final")
        response = dm_sim.post(dm_sim._serialise(root))
        parsed = dm_sim.parse(response.content)
        assert any(status["code"] == "401" for status in parsed["statuses"])
        assert not parsed["replaces"]
    finally:
        dm_sim.close()


def test_management_object_inventory_is_served(server: LiveServer) -> None:
    body = httpx.get(f"{server.base_url}/dm/mo", timeout=5).json()
    urns = [o["urn"] for o in body["objects"]]
    assert "urn:oma:mo:ext-3gpp-ims:1.0" in urns


def test_health_and_readiness_over_a_real_socket(server: LiveServer) -> None:
    assert httpx.get(f"{server.base_url}/healthz", timeout=5).json()["status"] == "ok"
    ready = httpx.get(f"{server.base_url}/readyz", timeout=5).json()
    assert ready["status"] == "ready"
