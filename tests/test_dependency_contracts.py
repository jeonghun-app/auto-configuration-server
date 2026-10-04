"""Behaviour that must survive a dependency update.

Each test here pins something the application relies on but does not implement
itself: form parsing (python-multipart, Starlette), cookie and header emission
(Starlette), the XML parser's refusal of hostile documents (lxml/libxml2),
uvicorn's proxy-header rewrite of the client address and the string form of the
protocol enums, the uvicorn command line the image runs and boto3's choice of
DynamoDB endpoint. A future bump that changes any of these fails here rather than
in production.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any

import pytest
from botocore.awsrequest import AWSResponse
from fastapi.testclient import TestClient
from lxml import etree
from tests.conftest import ADMIN_TOKEN, TEST_IMSI, TEST_MSISDN, base_query
from uvicorn.main import main as uvicorn_cli
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
from acs.store import build_store
from acs.store.dynamodb import DynamoDbStore
from acs.store.memory import MemoryStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
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


# ------------------------------------------- uvicorn command line (Dockerfile)
def _image_command() -> list[str]:
    dockerfile = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    match = re.search(r"^CMD (\[.*\])$", dockerfile, re.MULTILINE)
    assert match, "the Dockerfile has no exec-form CMD"
    command: list[str] = json.loads(match.group(1))
    return command


def test_the_image_command_line_is_accepted_by_the_installed_uvicorn() -> None:
    # A renamed or removed option would otherwise surface only as a container that
    # exits at start-up. The values are the ones the Dockerfile comments justify.
    command = _image_command()
    assert command[0] == "uvicorn"
    params = uvicorn_cli.make_context("uvicorn", command[1:]).params
    assert params["app"] == "acs.app:create_app"
    assert params["factory"] is True
    assert params["port"] == 8080
    assert params["access_log"] is False
    assert params["timeout_keep_alive"] == 65
    assert params["proxy_headers"] is True
    assert params["forwarded_allow_ips"] == "*"


def test_the_lock_installs_no_alternative_http_or_event_loop_implementation() -> None:
    # The image is verified on uvicorn's h11 protocol and the asyncio loop. With
    # httptools or uvloop present, "--http auto" and "--loop auto" would switch to
    # them without any change to the command line.
    lock = (ROOT / "requirements.lock").read_text().lower()
    names = {line.split("==")[0].strip() for line in lock.splitlines() if "==" in line}
    assert "h11" in names
    assert not names & {"httptools", "uvloop", "websockets", "wsproto"}


# --------------------------------------------------- boto3 DynamoDB endpoint
class _Body:
    def stream(self, **_: Any) -> Iterator[bytes]:
        yield b"{}"


def _isolate_aws_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    for name in ("AWS_PROFILE", "AWS_ACCOUNT_ID_ENDPOINT_MODE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    # Present in an ECS task's credentials; makes botocore prefer the account-ID
    # endpoint unless an explicit endpoint is configured.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "111122223333")


def _first_request_url(store: DynamoDbStore) -> str:
    sent: list[str] = []

    def capture(request: Any, **_: Any) -> AWSResponse:
        sent.append(request.url)
        return AWSResponse(request.url, 200, {}, _Body())

    store._table.meta.client.meta.events.register("before-send", capture)
    assert store.get_subscriber(TEST_IMSI) is None
    assert sent
    return sent[0]


@pytest.mark.aws
def test_the_configured_dynamodb_endpoint_is_used_even_with_aws_endpoint_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _isolate_aws_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://global-override.invalid:1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "http://service-override.invalid:1")
    monkeypatch.setenv("ACS_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("ACS_DYNAMODB_ENDPOINT_URL", "http://dynamodb:8000")
    store = build_store(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(store, DynamoDbStore)
    assert _first_request_url(store) == "http://dynamodb:8000/"


@pytest.mark.aws
def test_without_a_configured_endpoint_dynamodb_is_reached_on_its_aws_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # botocore derives an account-ID endpoint from credentials that carry an
    # account ID, so the task's egress must allow that name and not only
    # dynamodb.<region>.amazonaws.com. Opting out restores the regional name.
    _isolate_aws_environment(monkeypatch, tmp_path)
    for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_DYNAMODB", "ACS_DYNAMODB_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    url = _first_request_url(DynamoDbStore("rcs-acs", "ap-northeast-2"))
    assert url == "https://111122223333.ddb.ap-northeast-2.amazonaws.com/"

    monkeypatch.setenv("AWS_ACCOUNT_ID_ENDPOINT_MODE", "disabled")
    url = _first_request_url(DynamoDbStore("rcs-acs", "ap-northeast-2"))
    assert url == "https://dynamodb.ap-northeast-2.amazonaws.com/"


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
