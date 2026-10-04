"""The XML DM path keeps its media types, formatting and 4xx handling."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import TEST_IMSI, TEST_MSISDN
from tests.test_dm_session import (
    DM_PASSWORD,
    PACKAGE_1_BODY,
    SESSION_KEY,
    basic_cred,
    package,
)

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import syncml
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
async def dm_client(settings: Settings, dm_store: MemoryStore) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(settings, dm_store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "content_type",
    [
        syncml.CONTENT_TYPE_XML,
        f"{syncml.CONTENT_TYPE_XML}; charset=UTF-8",
        "text/xml",
        "application/xml",
        "",
    ],
)
async def test_xml_requests_still_receive_indented_xml(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, content_type: str
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = await dm_client.post(
        "/dm", content=package(1, PACKAGE_1_BODY, cred=basic_cred()), headers=headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == syncml.CONTENT_TYPE_XML
    assert response.content.startswith(b"<?xml")
    # Compact output is a WBXML-only property; XML keeps its formatting.
    assert b">\n  <" in response.content
    message = syncml.parse(response.content)
    assert message.of("Status")[0].data == syncml.STATUS_AUTH_ACCEPTED
    assert message.of("Get")
    session = dm_store.get_dm_session(SESSION_KEY)
    assert session is not None and session.phase == "devinfo"


@pytest.mark.parametrize(
    "content_type",
    ["application/wbxml", "application/vnd.wap.wbxml", "application/vnd.syncml+wbxml"],
)
async def test_other_wbxml_media_types_are_still_refused_with_415(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, content_type: str
) -> None:
    response = await dm_client.post(
        "/dm",
        content=package(1, PACKAGE_1_BODY, cred=basic_cred()),
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 415
    assert dm_store.get_dm_session(SESSION_KEY) is None


@pytest.mark.parametrize(
    ("field", "old", "value"),
    [
        ("CmdID", "1", "²"),
        ("MsgID", "1", "²"),
        ("MsgID", "1", "9" * 5000),
        ("MaxMsgSize", "16384", "9" * 5000),
    ],
    ids=["cmdid-superscript", "msgid-superscript", "msgid-5000-digits", "maxmsgsize-5000-digits"],
)
async def test_unparseable_xml_decimal_fields_return_400_not_500(
    dm_client: httpx.AsyncClient, dm_store: MemoryStore, field: str, old: str, value: str
) -> None:
    # str.isdigit() accepts these, but int() refuses them; main raised a 500.
    xml = package(1, PACKAGE_1_BODY, cred=basic_cred())
    before = f">{old}</{field}>".encode()
    assert before in xml
    response = await dm_client.post(
        "/dm",
        content=xml.replace(before, f">{value}</{field}>".encode(), 1),
        headers={"Content-Type": syncml.CONTENT_TYPE_XML},
    )
    assert response.status_code == 400
    assert dm_store.get_dm_session(SESSION_KEY) is None
