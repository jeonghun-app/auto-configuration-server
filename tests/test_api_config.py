"""HTTP surface of the configuration endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    TEST_IMSI,
    TEST_MSISDN,
    base_query,
    complete_otp_flow,
    provision_and_get_token,
)

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omacp import writer
from acs.store.memory import MemoryStore


def test_healthz_needs_no_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_catalogue_sizes(client: TestClient) -> None:
    checks = client.get("/readyz").json()["checks"]
    assert checks["store"] == "ok"
    assert checks["omacp_parameters"] > 100
    assert checks["omadm_nodes"] > 20


def test_readyz_fails_when_the_store_is_broken(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    class BrokenStore(MemoryStore):
        def health(self) -> bool:
            return False

    broken = BrokenStore()
    broken.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN))
    with TestClient(create_app(settings, broken)) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


@pytest.mark.spec
@pytest.mark.parametrize("path", ["/", "/config", "/rcs/config"])
def test_every_configured_path_serves_the_flow(client: TestClient, path: str) -> None:
    response = client.get(path, params=base_query())
    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.spec
def test_full_otp_flow_returns_a_valid_document(client: TestClient) -> None:
    xml = complete_otp_flow(client)
    assert writer.validate_structure(xml.encode()) == []
    assert "ap2001" in xml
    assert "ap2002" in xml


def test_configuration_response_declares_xml_and_forbids_caching(client: TestClient) -> None:
    token = provision_and_get_token(client)
    response = client.get("/config", params=base_query(vers=1, token=token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-Id": "abc123"})
    assert response.headers["X-Request-Id"] == "abc123"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    assert client.get("/healthz").headers["X-Request-Id"]


@pytest.mark.spec
def test_malformed_parameters_are_rejected_with_400(client: TestClient) -> None:
    response = client.get("/config", params={"IMSI": "not-digits"})
    assert response.status_code == 400


def test_malformed_status_is_configurable(settings: Settings, seeded_store: MemoryStore) -> None:
    tweaked = settings.model_copy(update={"malformed_request_status": 403})
    with TestClient(create_app(tweaked, seeded_store)) as client:
        assert client.get("/config", params={"IMSI": "bad"}).status_code == 403


@pytest.mark.spec
def test_unknown_subscriber_receives_511(client: TestClient) -> None:
    response = client.get("/config", params=base_query(IMSI="001019999999999"))
    assert response.status_code == 511
    assert response.content == b""


@pytest.mark.spec
def test_non_entitled_subscriber_receives_403(settings: Settings, store: MemoryStore) -> None:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, entitled=False))
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/config", params=base_query()).status_code == 403


def test_post_is_accepted_for_the_otp_step(client: TestClient) -> None:
    # Some clients POST the OTP so it does not appear in proxy access logs.
    client.get("/config", params=base_query())
    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())
    response = client.post("/config", params=base_query(OTP=otp))
    assert response.status_code == 200
    assert b"wap-provisioningdoc" in response.content


def test_repeated_app_parameter_filters_the_document(client: TestClient) -> None:
    token = provision_and_get_token(client)
    response = client.get(
        "/config", params=[*base_query(vers=0, token=token).items(), ("app", "ap2001")]
    )
    assert response.status_code == 200
    assert b"ap2001" in response.content
    assert b'value="ap2002"' not in response.content


def test_dev_endpoints_are_absent_outside_development(store: MemoryStore) -> None:
    production_like = Settings(
        env="test", dev_endpoints_enabled=False, admin_token="x", sms_provider="mock"
    )
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN))
    with TestClient(create_app(production_like, store)) as client:
        assert client.get("/dev/sms").status_code == 404


def test_docs_are_hidden_in_production_like_environments(store: MemoryStore) -> None:
    prod = Settings(
        env="prod",
        store_backend="dynamodb",
        sms_provider="sns",
        admin_token="x",
        pii_log_mode="mask",
    )
    app = create_app(prod, store)
    assert app.docs_url is None
    assert app.openapi_url is None


def test_invalid_production_configuration_refuses_to_start(store: MemoryStore) -> None:
    # A misconfigured ACS that starts anyway can disable RCS fleet-wide.
    bad = Settings(env="prod", store_backend="memory", sms_provider="mock")
    with pytest.raises(RuntimeError, match="store_backend=memory is unsafe"):
        create_app(bad, store)


def test_unhandled_error_does_not_leak_internals(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    app = create_app(settings, seeded_store)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
        assert response.status_code == 500
        assert response.json() == {"error": "internal_error"}
        assert "secret internal detail" not in response.text


def test_openapi_documents_the_configuration_endpoint(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/config" in schema["paths"]
    assert "/dm" in schema["paths"]


def test_msisdn_entry_flow_is_accessible(client: TestClient) -> None:
    response = client.get("/msisdn")
    assert response.status_code == 200
    body = response.text
    assert 'lang="en"' in body
    assert '<label for="msisdn">' in body
    assert 'aria-describedby="msisdn-help"' in body
    assert "Content-Security-Policy" in response.headers


def test_msisdn_flow_sends_an_otp_and_verifies_it(client: TestClient) -> None:
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    submitted = client.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": csrf})
    assert submitted.status_code == 200
    assert 'name="otp"' in submitted.text

    messages = client.get("/dev/sms", params={"msisdn": TEST_MSISDN}).json()
    otp = "".join(ch for ch in messages[0]["body"] if ch.isdigit())
    csrf2 = submitted.text.split('name="csrf" value="')[1].split('"')[0]
    verified = client.post(
        "/msisdn/verify", data={"msisdn": TEST_MSISDN, "otp": otp, "csrf": csrf2}
    )
    assert verified.status_code == 200
    assert "verified" in verified.text.lower()


def test_msisdn_flow_requires_csrf(client: TestClient) -> None:
    response = client.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": "forged"})
    assert response.status_code == 400


def test_msisdn_flow_does_not_reveal_whether_a_number_exists(client: TestClient) -> None:
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    known = client.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": csrf})

    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    unknown = client.post("/msisdn", data={"msisdn": "+821099999999", "csrf": csrf})

    assert known.status_code == unknown.status_code
    assert "If that number is eligible" in known.text
    assert "If that number is eligible" in unknown.text


def test_msisdn_flow_rejects_a_bad_number(client: TestClient) -> None:
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = client.post("/msisdn", data={"msisdn": "nonsense", "csrf": csrf})
    assert response.status_code == 400


def test_wrong_otp_in_the_web_flow_is_refused(client: TestClient) -> None:
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    submitted = client.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": csrf})
    csrf2 = submitted.text.split('name="csrf" value="')[1].split('"')[0]
    response = client.post(
        "/msisdn/verify", data={"msisdn": TEST_MSISDN, "otp": "000000", "csrf": csrf2}
    )
    assert response.status_code == 400
