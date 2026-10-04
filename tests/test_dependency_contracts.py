"""Behaviour that must survive a dependency update.

Each test here pins something the application relies on but does not implement
itself: form parsing (python-multipart, Starlette), cookie and header emission
(Starlette), the XML parser's refusal of hostile documents (lxml/libxml2),
uvicorn's proxy-header rewrite of the client address and the string form of the
protocol enums. A future bump that changes any of these fails here rather than in
production.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from lxml import etree
from tests.conftest import ADMIN_TOKEN, TEST_IMSI, TEST_MSISDN, base_query
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from acs.api.console import CSRF_COOKIE as CONSOLE_CSRF_COOKIE
from acs.api.console import SESSION_COOKIE
from acs.api.msisdn_ui import CSRF_COOKIE as MSISDN_CSRF_COOKIE
from acs.app import create_app
from acs.auth.identity import IdentityDecision, IdentityMethod
from acs.config import Settings
from acs.protocol.omacp import writer
from acs.protocol.omadm import syncml
from acs.protocol.vers import VersAction
from acs.store.memory import MemoryStore

FORM = {"content-type": "application/x-www-form-urlencoded"}


def _csrf_from(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def _set_cookies(response: object) -> dict[str, SimpleCookie]:
    """Parse every ``Set-Cookie`` header, keyed by cookie name."""
    parsed: dict[str, SimpleCookie] = {}
    for raw in response.headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(raw)
        for name in cookie:
            parsed[name] = cookie
    return parsed


def _sent_otp(client: TestClient) -> str:
    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    return "".join(ch for ch in messages[-1]["body"] if ch.isdigit())


@pytest.fixture
def https_client(settings: Settings, seeded_store: MemoryStore) -> Iterator[TestClient]:
    app = create_app(settings, seeded_store)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client


# --------------------------------------------------------- RCC.14 POST bodies
def test_a_semicolon_does_not_separate_form_fields_in_a_post_body(client: TestClient) -> None:
    # python-multipart 0.0.31 stopped treating ";" as a urlencoded separator. The
    # whole remainder becomes the value of "vers", which is not an integer, so the
    # request is refused instead of being read as two parameters.
    body = f"vers=0;IMSI={TEST_IMSI}"
    response = client.post("/config", content=body, headers=FORM)
    assert response.status_code == 400
    assert client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json() == []


def test_an_ampersand_separated_post_body_is_read(client: TestClient) -> None:
    body = "&".join(f"{key}={value}" for key, value in base_query().items())
    response = client.post("/config", content=body, headers=FORM)
    assert response.status_code == 200
    assert response.content == b""
    assert _sent_otp(client)


def test_an_otp_in_a_multipart_body_completes_provisioning(client: TestClient) -> None:
    assert client.get("/config", params=base_query()).status_code == 200
    form = {str(k): str(v) for k, v in base_query().items()}
    form["OTP"] = _sent_otp(client)
    # An uploaded file is never a configuration parameter, even one named like one.
    response = client.post("/config", data=form, files={"token": ("t.txt", b"forged")})
    assert response.status_code == 200
    assert b"wap-provisioningdoc" in response.content


def test_a_parameter_repeated_across_query_and_body_is_refused(client: TestClient) -> None:
    form = {str(k): str(v) for k, v in base_query().items()}
    response = client.post("/config", params={"IMSI": TEST_IMSI}, data=form)
    assert response.status_code == 400


# --------------------------------------------- uvicorn proxy headers (Dockerfile)
def _behind_uvicorn_proxy_headers(settings: Settings, store: MemoryStore) -> TestClient:
    # The image runs uvicorn with --proxy-headers --forwarded-allow-ips "*", so
    # request.client is rewritten from X-Forwarded-For before the app sees it.
    # With every hop trusted, uvicorn picks the *left-most* entry, which the caller
    # controls. Header enrichment must keep deciding trust on the right-most entry.
    enriching = settings.model_copy(update={"trusted_proxy_cidrs": "10.0.0.0/8"})
    # Starlette and uvicorn type the ASGI callable differently; both are ASGI 3.
    inner = create_app(enriching, store)
    app = ProxyHeadersMiddleware(inner, trusted_hosts="*")  # type: ignore[arg-type]
    return TestClient(app)  # type: ignore[arg-type]


def test_enrichment_trusts_the_right_most_forwarded_entry_behind_uvicorn(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    query = {k: v for k, v in base_query().items() if k != "IMSI"}
    with _behind_uvicorn_proxy_headers(settings, seeded_store) as client:
        response = client.get(
            "/config",
            params=query,
            headers={
                "X-3GPP-Intended-Identity": TEST_MSISDN,
                "X-Forwarded-For": "203.0.113.9, 10.0.0.7",
            },
        )
    assert response.status_code == 200
    assert b"wap-provisioningdoc" in response.content


def test_a_forged_left_most_forwarded_entry_does_not_enrich_behind_uvicorn(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    query = {k: v for k, v in base_query().items() if k != "IMSI"}
    with _behind_uvicorn_proxy_headers(settings, seeded_store) as client:
        response = client.get(
            "/config",
            params=query,
            headers={
                "X-3GPP-Intended-Identity": TEST_MSISDN,
                "X-Forwarded-For": "10.0.0.7, 203.0.113.9",
            },
        )
    assert b"wap-provisioningdoc" not in response.content
    assert response.status_code == 511


# ----------------------------------------------------------- cookies, headers
def test_console_session_cookies_are_scoped_and_hidden_from_scripts(
    https_client: TestClient,
) -> None:
    page = https_client.get("/admin/ui/login")
    login_cookie = _set_cookies(page)[CONSOLE_CSRF_COOKIE][CONSOLE_CSRF_COOKIE]
    assert login_cookie["httponly"] and login_cookie["secure"]
    assert login_cookie["samesite"].lower() == "strict"
    assert login_cookie["path"] == "/admin/ui"

    response = https_client.post(
        "/admin/ui/login", data={"token": ADMIN_TOKEN, "csrf": _csrf_from(page.text)}
    )
    assert response.status_code == 303
    cookies = _set_cookies(response)
    for name in (SESSION_COOKIE, CONSOLE_CSRF_COOKIE):
        morsel = cookies[name][name]
        assert morsel["httponly"], name
        assert morsel["secure"], name
        assert morsel["samesite"].lower() == "strict", name
        assert morsel["path"] == "/admin/ui", name
        assert morsel["max-age"] == "3600", name
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "no-store" in response.headers["cache-control"]

    # The session works, and sign-out expires it on the same path.
    assert https_client.get("/admin/ui").status_code == 200
    csrf = https_client.cookies.get(CONSOLE_CSRF_COOKIE) or ""
    signed_out = https_client.post("/admin/ui/logout", data={"csrf": csrf})
    assert signed_out.status_code == 303
    cleared = _set_cookies(signed_out)[SESSION_COOKIE][SESSION_COOKIE]
    assert cleared["path"] == "/admin/ui"
    assert cleared["max-age"] == "0"
    assert https_client.get("/admin/ui").status_code == 303


def test_console_cookies_are_not_secure_on_plain_http(client: TestClient) -> None:
    page = client.get("/admin/ui/login")
    morsel = _set_cookies(page)[CONSOLE_CSRF_COOKIE][CONSOLE_CSRF_COOKIE]
    assert not morsel["secure"]
    assert morsel["httponly"]


def test_msisdn_pages_send_csrf_cookie_and_security_headers(https_client: TestClient) -> None:
    page = https_client.get("/msisdn")
    assert page.status_code == 200
    morsel = _set_cookies(page)[MSISDN_CSRF_COOKIE][MSISDN_CSRF_COOKIE]
    assert morsel["httponly"] and morsel["secure"]
    assert morsel["samesite"].lower() == "strict"
    assert morsel["max-age"] == "900"
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert page.headers["x-content-type-options"] == "nosniff"

    submitted = https_client.post(
        "/msisdn", data={"msisdn": TEST_MSISDN, "csrf": _csrf_from(page.text)}
    )
    assert submitted.status_code == 200
    assert submitted.headers["cache-control"] == "no-store"
    # A fresh CSRF token replaces the first one.
    rotated = _set_cookies(submitted)[MSISDN_CSRF_COOKIE][MSISDN_CSRF_COOKIE]
    assert rotated.value == _csrf_from(submitted.text)
    assert rotated.value != morsel.value


def test_msisdn_form_without_the_csrf_cookie_is_refused(settings: Settings) -> None:
    with TestClient(create_app(settings, MemoryStore())) as client:
        page = client.get("/msisdn")
        client.cookies.clear()
        response = client.post(
            "/msisdn", data={"msisdn": TEST_MSISDN, "csrf": _csrf_from(page.text)}
        )
        assert response.status_code == 400


# ------------------------------------------------------------ hostile XML
def _billion_laughs(root: str, inner: str) -> bytes:
    entities = '<!ENTITY a "lollollollollollollollollollol">' + "".join(
        f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(9)
    )
    return f'<?xml version="1.0"?><!DOCTYPE {root} [{entities}]>{inner}'.encode()


def test_omacp_parser_leaves_an_external_entity_in_element_text_unexpanded() -> None:
    # An external entity in an attribute value is a well-formedness error whatever
    # the parser settings, so only element text shows whether the parser would
    # read the file.
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<wap-provisioningdoc version="1.1"><characteristic type="VERS">&x;'
        b"</characteristic></wap-provisioningdoc>"
    )
    serialised = etree.tostring(writer.parse(payload))
    assert b"root:" not in serialised
    # The reference survives as an unexpanded entity node.
    assert b'<characteristic type="VERS">&x;</characteristic>' in serialised


def test_omacp_parser_refuses_exponential_entity_expansion() -> None:
    payload = _billion_laughs(
        "wap-provisioningdoc",
        '<wap-provisioningdoc version="1.1"><characteristic type="VERS">&j;'
        "</characteristic></wap-provisioningdoc>",
    )
    with pytest.raises(etree.XMLSyntaxError):
        writer.parse(payload)


def test_syncml_parser_refuses_exponential_entity_expansion() -> None:
    payload = _billion_laughs(
        "SyncML",
        "<SyncML><SyncHdr><SessionID>&j;</SessionID><MsgID>1</MsgID></SyncHdr><SyncBody/></SyncML>",
    )
    with pytest.raises(syncml.SyncMlParseError, match="malformed"):
        syncml.parse(payload)


# ------------------------------------------------------------ enum strings
@pytest.mark.parametrize("member", [*IdentityMethod, *IdentityDecision, *VersAction])
def test_protocol_enums_format_as_their_wire_value(member: object) -> None:
    # Python 3.11 renders a (str, Enum) member as "Class.NAME" in str() and
    # f-strings. StrEnum renders the value, so any log, metric or response that
    # interpolates a member writes the wire value, never the Python name.
    value = member.value  # type: ignore[attr-defined]
    assert str(member) == value
    assert f"{member}" == value
    assert json.dumps(member) == json.dumps(value)
    assert member == value
