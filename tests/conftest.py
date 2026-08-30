"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.domain.service import ProvisioningService
from acs.protocol.omacp.catalog import get_catalog
from acs.protocol.omadm.motree import get_tree
from acs.protocol.omadm.session import DmService
from acs.sms.base import MockSmsSender
from acs.store.memory import MemoryStore

TEST_IMSI = "001010000000001"
TEST_MSISDN = "+821012345678"
TEST_IMEI = "356938035643809"
ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        store_backend="memory",
        sms_provider="mock",
        admin_token=ADMIN_TOKEN,
        dev_endpoints_enabled=True,
        default_rcs_profile="UP_2.4",
        pii_log_mode="mask",
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def subscriber() -> Subscriber:
    return Subscriber(
        imsi=TEST_IMSI,
        msisdn=TEST_MSISDN,
        entitled=True,
        provisioning_version=1,
        rcs_profile="UP_2.4",
    )


@pytest.fixture
def seeded_store(store: MemoryStore, subscriber: Subscriber) -> MemoryStore:
    store.put_subscriber(subscriber)
    return store


@pytest.fixture
def sms(store: MemoryStore) -> MockSmsSender:
    return MockSmsSender(store)


@pytest.fixture
def service(
    settings: Settings, seeded_store: MemoryStore, sms: MockSmsSender
) -> ProvisioningService:
    return ProvisioningService(settings, seeded_store, sms)


@pytest.fixture
def dm_service(settings: Settings, seeded_store: MemoryStore) -> DmService:
    return DmService(settings, seeded_store, get_tree())


@pytest.fixture
def client(settings: Settings, seeded_store: MemoryStore) -> Iterator[TestClient]:
    app = create_app(settings, seeded_store)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def _clear_catalog_caches() -> Iterator[None]:
    yield
    get_catalog.cache_clear()
    get_tree.cache_clear()


_COLLECTED: pytest.StashKey[frozenset[str]] = pytest.StashKey()


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record every collected test id.

    The conformance registry names the tests that prove each requirement. Checking
    those names against what pytest actually collected is what stops the registry
    from citing tests that were renamed or deleted.
    """
    config.stash[_COLLECTED] = frozenset(item.nodeid for item in items)


@pytest.fixture
def collected_node_ids(request: pytest.FixtureRequest) -> frozenset[str]:
    return request.config.stash.get(_COLLECTED, frozenset())


def base_query(**extra: object) -> dict[str, object]:
    """A realistic RCC.14 query string."""
    params: dict[str, object] = {
        "vers": 0,
        "IMSI": TEST_IMSI,
        "IMEI": TEST_IMEI,
        "terminal_vendor": "Sim",
        "terminal_model": "SimPhone",
        "terminal_sw_version": "1.0",
        "client_vendor": "Sim",
        "client_version": "RCSAndrd-1.0",
        "rcs_profile": "UP_2.4",
        "rcs_state": 0,
        "default_sms_app": 1,
    }
    params.update(extra)
    return params


def complete_otp_flow(client: TestClient, **extra: object) -> str:
    """Drive the OTP challenge and return the configuration XML."""
    first = client.get("/config", params=base_query(**extra))
    assert first.status_code == 200
    assert first.content == b""
    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())
    second = client.get("/config", params=base_query(OTP=otp, **extra))
    assert second.status_code == 200
    return second.text


def token_from_xml(xml: str) -> str:
    """Extract the provisioning token from a configuration document."""
    root = ElementTree.fromstring(xml)  # noqa: S314 - test-local server output
    node = root.find("characteristic[@type='TOKEN']/parm[@name='token']")
    assert node is not None, "document carries no TOKEN characteristic"
    return node.get("value") or ""


def provision_and_get_token(client: TestClient, **extra: object) -> str:
    """Complete provisioning and return the token for follow-up requests."""
    return token_from_xml(complete_otp_flow(client, **extra))
