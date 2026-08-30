"""Operator console.

This page is network-exposed and renders subscriber data, including values a
handset reported over OMA-DM, so the security tests here matter as much as the
functional ones.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.conftest import ADMIN_TOKEN, TEST_IMEI, TEST_IMSI, TEST_MSISDN

from acs.api.console import SESSION_COOKIE, make_session, session_valid
from acs.api.html import esc
from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Device
from acs.store.memory import MemoryStore

CONSOLE = "/admin/ui"


@pytest.fixture
def console(settings: Settings, seeded_store: MemoryStore) -> Iterator[TestClient]:
    seeded_store.put_device(
        Device(
            device_id=TEST_IMEI,
            imsi=TEST_IMSI,
            manufacturer="SimCorp",
            model="SimPhone",
            sw_version="SIM-1.0",
            client_version="RCSAndrd-1.0",
            mo_values={"./DevInfo/Man": "SimCorp", "./DevDetail/SwV": "SIM-1.0"},
        )
    )
    app = create_app(settings, seeded_store)
    with TestClient(app, follow_redirects=False) as client:
        yield client


def sign_in(client: TestClient) -> str:
    """Sign in and return the CSRF token for subsequent forms."""
    page = client.get(f"{CONSOLE}/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = client.post(
        f"{CONSOLE}/login", data={"token": ADMIN_TOKEN, "csrf": csrf, "next": CONSOLE}
    )
    assert response.status_code == 303
    return client.cookies.get("acs_console_csrf") or ""


# ------------------------------------------------------------------- session
def test_session_value_round_trips() -> None:
    token = make_session("secret", now=1000)
    assert session_valid(token, "secret", now=1000)


def test_session_expires() -> None:
    token = make_session("secret", now=1000)
    assert not session_valid(token, "secret", now=1000 + 3601)


def test_session_signed_with_the_admin_token() -> None:
    # Rotating the admin token must invalidate every live console session.
    token = make_session("secret", now=1000)
    assert not session_valid(token, "different-secret", now=1000)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj", "MTAwMDo="])
def test_malformed_session_cookies_are_rejected(bad: str) -> None:
    assert not session_valid(bad, "secret", now=1000)


def test_session_without_a_secret_is_rejected() -> None:
    assert not session_valid(make_session("secret"), "")


# -------------------------------------------------------------- fail closed
def test_console_is_unavailable_without_an_admin_token(store: MemoryStore) -> None:
    no_token = Settings(env="test", admin_token="", sms_provider="mock")
    with TestClient(create_app(no_token, store), follow_redirects=False) as client:
        login = client.get(f"{CONSOLE}/login")
        assert login.status_code == 503
        assert "no default" in login.text
        assert client.get(CONSOLE).status_code == 503


def test_unauthenticated_access_redirects_to_sign_in(console: TestClient) -> None:
    for path in ("", "/subscribers", "/devices", "/catalog", "/conformance"):
        response = console.get(f"{CONSOLE}{path}")
        assert response.status_code == 303
        assert "/admin/ui/login" in response.headers["location"]


def test_wrong_token_is_refused(console: TestClient) -> None:
    page = console.get(f"{CONSOLE}/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = console.post(f"{CONSOLE}/login", data={"token": "wrong", "csrf": csrf})
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_sign_in_requires_csrf(console: TestClient) -> None:
    console.get(f"{CONSOLE}/login")
    response = console.post(f"{CONSOLE}/login", data={"token": ADMIN_TOKEN, "csrf": "forged"})
    assert response.status_code == 400


def test_sign_out_clears_the_session(console: TestClient) -> None:
    csrf = sign_in(console)
    assert console.post(f"{CONSOLE}/logout", data={"csrf": csrf}).status_code == 303
    assert console.get(CONSOLE).status_code == 303


def test_open_redirect_is_not_possible(console: TestClient) -> None:
    page = console.get(f"{CONSOLE}/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = console.post(
        f"{CONSOLE}/login",
        data={"token": ADMIN_TOKEN, "csrf": csrf, "next": "https://evil.example.com"},
    )
    assert response.headers["location"] == CONSOLE


# ------------------------------------------------------------------ headers
def test_pages_forbid_caching_and_scripts(console: TestClient) -> None:
    sign_in(console)
    response = console.get(CONSOLE)
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "script" not in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    # No JavaScript anywhere in the page.
    assert "<script" not in response.text.lower()
    assert "onclick" not in response.text.lower()


# ------------------------------------------------------------------ content
def test_overview_shows_the_inventory(console: TestClient) -> None:
    sign_in(console)
    body = console.get(CONSOLE).text
    assert "Overview" in body
    assert "OMA-CP parameters available" in body
    assert "SimPhone" in body


def test_numbers_page_lists_the_subscriber(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/subscribers").text
    assert TEST_MSISDN in body
    assert TEST_IMSI in body


def test_numbers_page_searches_by_msisdn(console: TestClient) -> None:
    sign_in(console)
    row = f'href="/admin/ui/subscribers/{TEST_IMSI}"'
    found = console.get(f"{CONSOLE}/subscribers", params={"q": "1012345678"}).text
    assert row in found
    missing = console.get(f"{CONSOLE}/subscribers", params={"q": "9999999999"}).text
    assert row not in missing
    assert "No subscriber matched" in missing


def test_numbers_page_searches_by_imsi(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/subscribers", params={"q": TEST_IMSI}).text
    assert TEST_MSISDN in body


def test_subscriber_detail_shows_parameters_and_devices(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/subscribers/{TEST_IMSI}").text
    assert "Parameter overrides" in body
    assert "MaxSizeFileTr" in body  # catalogue is offered for selection
    assert "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" in body
    assert TEST_IMEI in body


def test_unknown_subscriber_is_404(console: TestClient) -> None:
    sign_in(console)
    assert console.get(f"{CONSOLE}/subscribers/001019999999999").status_code == 404


# ------------------------------------------------------------------ mutation
def test_create_a_number(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers",
        data={
            "imsi": "001010000000002",
            "msisdn": "821087654321",
            "csrf": csrf,
            "entitled": "1",
            "volte_enabled": "1",
            "rcs_profile": "UP_1.0",
        },
    )
    assert response.status_code == 303
    created = seeded_store.get_subscriber("001010000000002")
    assert created is not None
    assert created.msisdn == "+821087654321"
    assert created.rcs_profile == "UP_1.0"


def test_create_rejects_a_bad_number(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers",
        data={"imsi": "001010000000003", "msisdn": "nonsense", "csrf": csrf},
    )
    assert response.status_code == 303
    assert "e=1" in response.headers["location"]


def test_create_rejects_a_duplicate_imsi(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers",
        data={"imsi": TEST_IMSI, "msisdn": TEST_MSISDN, "csrf": csrf},
    )
    assert "e=1" in response.headers["location"]


def test_update_a_number(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}",
        data={
            "msisdn": TEST_MSISDN,
            "csrf": csrf,
            "rcs_profile": "joyn_blackbird",
            "forced_vers": "-2",
            "imei_allowlist": f"{TEST_IMEI}",
            "volte_enabled": "1",
        },
    )
    assert response.status_code == 303
    subscriber = seeded_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    assert subscriber.rcs_profile == "joyn_blackbird"
    assert subscriber.forced_vers == -2
    assert subscriber.imei_allowlist == [TEST_IMEI]
    assert subscriber.entitled is False  # checkbox omitted means unchecked


def test_update_rejects_an_invalid_forced_version(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}",
        data={"msisdn": TEST_MSISDN, "csrf": csrf, "forced_vers": "7"},
    )
    assert "e=1" in response.headers["location"]


def test_update_rejects_a_bad_imei(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}",
        data={"msisdn": TEST_MSISDN, "csrf": csrf, "imei_allowlist": "abc"},
    )
    assert "e=1" in response.headers["location"]


def test_mutations_require_csrf(console: TestClient) -> None:
    sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}",
        data={"msisdn": TEST_MSISDN, "csrf": "forged"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------- overrides
def test_set_an_omacp_override(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    key = "APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr"
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "key": key, "value": "2048"},
    )
    assert response.status_code == 303
    subscriber = seeded_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    assert subscriber.overrides[key] == "2048"


def test_set_an_omadm_override(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    node = "./3GPP_IMS/1/Timer_T1"
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "dm_key": node, "value": "9999"},
    )
    assert response.status_code == 303
    subscriber = seeded_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    assert subscriber.overrides[node] == "9999"


def test_remove_an_override(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    key = "APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr"
    console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "key": key, "value": "2048"},
    )
    console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "key": key, "value": ""},
    )
    subscriber = seeded_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    assert key not in subscriber.overrides


def test_an_uncatalogued_key_is_refused(console: TestClient, seeded_store: MemoryStore) -> None:
    # A typo would otherwise sit in the record forever, silently doing nothing.
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "key": "APPLICATION:ap2002/NOPE/Invented", "value": "1"},
    )
    assert "e=1" in response.headers["location"]
    subscriber = seeded_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    assert not subscriber.overrides


def test_an_override_reaches_the_served_document(
    console: TestClient, settings: Settings, seeded_store: MemoryStore
) -> None:
    csrf = sign_in(console)
    key = "APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr"
    console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/override",
        data={"csrf": csrf, "key": key, "value": "4242"},
    )
    with TestClient(create_app(settings, seeded_store)) as device:
        first = device.get("/config", params={"vers": 0, "IMSI": TEST_IMSI, "IMEI": TEST_IMEI})
        assert first.status_code == 200
        messages = device.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
        otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())
        served = device.get(
            "/config",
            params={"vers": 0, "IMSI": TEST_IMSI, "IMEI": TEST_IMEI, "OTP": otp},
        )
    assert b'name="MaxSizeFileTr" value="4242"' in served.content


# ------------------------------------------------------------------- actions
def test_bump_version_action(console: TestClient, seeded_store: MemoryStore) -> None:
    csrf = sign_in(console)
    before = seeded_store.get_subscriber(TEST_IMSI)
    assert before is not None
    # MemoryStore returns the live object, so copy the value, not the record.
    previous_version = before.provisioning_version
    console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/action",
        data={"csrf": csrf, "action": "invalidate"},
    )
    after = seeded_store.get_subscriber(TEST_IMSI)
    assert after is not None
    assert after.provisioning_version == previous_version + 1


def test_issue_token_action_shows_the_token_once(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/action",
        data={"csrf": csrf, "action": "issue-token"},
    )
    assert "Token+issued" in response.headers["location"].replace("%20", "+")


def test_delete_action_removes_the_subscriber(
    console: TestClient, seeded_store: MemoryStore
) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/action",
        data={"csrf": csrf, "action": "delete"},
    )
    assert response.headers["location"].startswith(f"{CONSOLE}/subscribers")
    assert seeded_store.get_subscriber(TEST_IMSI) is None


def test_unknown_action_is_refused(console: TestClient) -> None:
    csrf = sign_in(console)
    response = console.post(
        f"{CONSOLE}/subscribers/{TEST_IMSI}/action",
        data={"csrf": csrf, "action": "rm-rf"},
    )
    assert "e=1" in response.headers["location"]


# ------------------------------------------------------------------- devices
def test_device_list_and_detail(console: TestClient) -> None:
    sign_in(console)
    listing = console.get(f"{CONSOLE}/devices").text
    assert TEST_IMEI in listing
    assert "SimPhone" in listing

    detail = console.get(f"{CONSOLE}/devices/{TEST_IMEI}").text
    assert "./DevInfo/Man" in detail
    assert "SimCorp" in detail
    assert TEST_MSISDN in detail  # linked back to the number


def test_device_search(console: TestClient) -> None:
    sign_in(console)
    assert TEST_IMEI in console.get(f"{CONSOLE}/devices", params={"q": "simphone"}).text
    assert TEST_IMEI not in console.get(f"{CONSOLE}/devices", params={"q": "nokia"}).text


def test_unknown_device_is_404(console: TestClient) -> None:
    sign_in(console)
    assert console.get(f"{CONSOLE}/devices/000000000000000").status_code == 404


# ----------------------------------------------------------------- catalogue
def test_catalogue_page_lists_both_planes(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/catalog").text
    assert "OMA-CP provisioning parameters" in body
    assert "OMA-DM management nodes" in body
    assert "MaxSizeFileTr" in body
    assert "./DevInfo/DevId" in body


def test_catalogue_filter_and_profile(console: TestClient) -> None:
    sign_in(console)
    filtered = console.get(
        f"{CONSOLE}/catalog", params={"q": "MaxSizeFileTr", "profile": "UP_1.0"}
    ).text
    assert "MaxSizeFileTr" in filtered
    assert "ChatAuth" not in filtered


def test_conformance_page_shows_gaps(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/conformance").text
    assert "OMADM-ENC-WBXML" in body
    assert "not-implemented" in body
    assert "Nothing here is certified" in body


# --------------------------------------------------------------------- XSS
def test_values_reported_by_a_handset_are_escaped(
    console: TestClient, seeded_store: MemoryStore
) -> None:
    """A management object value comes from an untrusted device."""
    payload = '<script>alert("xss")</script>'
    seeded_store.put_device(
        Device(
            device_id="000000000000001",
            imsi=TEST_IMSI,
            model=payload,
            mo_values={"./DevInfo/Man": payload},
        )
    )
    sign_in(console)
    detail = console.get(f"{CONSOLE}/devices/000000000000001").text
    assert "<script>alert" not in detail
    assert "&lt;script&gt;" in detail

    listing = console.get(f"{CONSOLE}/devices").text
    assert "<script>alert" not in listing


def test_escaping_helper_covers_quotes_and_angles() -> None:
    assert esc('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;"
    assert esc(None) == ""
    assert esc(42) == "42"


def test_a_search_term_is_escaped(console: TestClient) -> None:
    sign_in(console)
    body = console.get(f"{CONSOLE}/subscribers", params={"q": '"><script>x</script>'}).text
    assert "<script>x" not in body


def test_a_flash_message_is_escaped(console: TestClient) -> None:
    sign_in(console)
    body = console.get(CONSOLE, params={"m": "<script>x</script>", "e": "1"}).text
    assert "<script>x" not in body
    assert "&lt;script&gt;" in body
