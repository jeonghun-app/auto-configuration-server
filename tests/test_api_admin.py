"""Admin API."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import (
    TEST_IMEI,
    TEST_IMSI,
    TEST_MSISDN,
    base_query,
    complete_otp_flow,
    provision_and_get_token,
)

from acs.app import create_app
from acs.config import Settings
from acs.store.memory import MemoryStore


def test_admin_is_disabled_without_a_configured_token(store: MemoryStore) -> None:
    # Fail closed: no default token exists to guess.
    no_token = Settings(env="test", admin_token="", sms_provider="mock")
    with TestClient(create_app(no_token, store)) as client:
        assert client.get("/admin/subscribers").status_code == 503


def test_wrong_admin_token_is_rejected(client: TestClient) -> None:
    response = client.get("/admin/subscribers", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_missing_authorization_header_is_rejected(client: TestClient) -> None:
    assert client.get("/admin/subscribers").status_code == 401


def test_list_and_get_subscribers(client: TestClient, admin_headers: dict[str, str]) -> None:
    listed = client.get("/admin/subscribers", headers=admin_headers).json()
    assert [s["imsi"] for s in listed] == [TEST_IMSI]
    single = client.get(f"/admin/subscribers/{TEST_IMSI}", headers=admin_headers).json()
    assert single["msisdn"] == TEST_MSISDN


def test_unknown_subscriber_is_404(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.get("/admin/subscribers/00101999", headers=admin_headers).status_code == 404


def test_create_subscriber(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.put(
        "/admin/subscribers/001010000000002",
        headers=admin_headers,
        json={"msisdn": "821087654321", "rcs_profile": "UP_1.0"},
    )
    assert response.status_code == 200
    assert response.json()["msisdn"] == "+821087654321"
    assert response.json()["rcs_profile"] == "UP_1.0"


def test_create_subscriber_validates_the_imsi(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/admin/subscribers/abc", headers=admin_headers, json={"msisdn": TEST_MSISDN}
    )
    assert response.status_code == 400


def test_create_subscriber_validates_the_msisdn(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/admin/subscribers/001010000000003", headers=admin_headers, json={"msisdn": "nope"}
    )
    assert response.status_code == 422


def test_forced_vers_must_be_a_known_value(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/admin/subscribers/001010000000004",
        headers=admin_headers,
        json={"msisdn": TEST_MSISDN, "forced_vers": -9},
    )
    assert response.status_code == 422


def test_delete_subscriber_revokes_tokens(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    complete_otp_flow(client)
    assert (
        client.delete(f"/admin/subscribers/{TEST_IMSI}", headers=admin_headers).status_code == 204
    )
    assert client.get("/config", params=base_query()).status_code == 511


def test_invalidate_bumps_the_configuration_version(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    token = provision_and_get_token(client)
    bumped = client.post(f"/admin/subscribers/{TEST_IMSI}/invalidate", headers=admin_headers).json()
    assert bumped["provisioning_version"] == 2
    # The client asking with vers=1 must now be re-provisioned in full.
    response = client.get("/config", params=base_query(vers=1, token=token))
    assert response.status_code == 200
    assert b"ap2002" in response.content


def test_disable_forces_a_negative_version_and_revokes_tokens(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    token = provision_and_get_token(client)
    disabled = client.post(
        f"/admin/subscribers/{TEST_IMSI}/disable", params={"vers": -2}, headers=admin_headers
    ).json()
    assert disabled["forced_vers"] == -2

    # The previously issued token is revoked, so it no longer authenticates.
    assert client.get("/config", params=base_query(token=token)).status_code == 511

    # Re-authenticating gets the disable document, not a configuration.
    xml = complete_otp_flow(client)
    assert 'name="version" value="-2"' in xml
    assert "ap2002" not in xml


def test_disable_rejects_an_unknown_value(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        f"/admin/subscribers/{TEST_IMSI}/disable", params={"vers": 7}, headers=admin_headers
    )
    assert response.status_code == 400


def test_enable_clears_the_forced_version(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.post(
        f"/admin/subscribers/{TEST_IMSI}/disable", params={"vers": -1}, headers=admin_headers
    )
    enabled = client.post(f"/admin/subscribers/{TEST_IMSI}/enable", headers=admin_headers).json()
    assert enabled["forced_vers"] is None
    assert enabled["entitled"] is True


def test_revoke_tokens_endpoint_reports_a_count(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    complete_otp_flow(client)
    response = client.post(f"/admin/subscribers/{TEST_IMSI}/revoke-tokens", headers=admin_headers)
    assert response.json()["revoked"] >= 1


def test_device_inventory_is_exposed(client: TestClient, admin_headers: dict[str, str]) -> None:
    complete_otp_flow(client)
    devices = client.get("/admin/devices", headers=admin_headers).json()
    assert devices[0]["device_id"] == TEST_IMEI
    assert devices[0]["model"] == "SimPhone"


def test_coverage_endpoint_reports_verified_counts(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    coverage = client.get("/admin/coverage", headers=admin_headers).json()
    assert coverage["omacp"]["parameters"] > 100
    assert 0 < coverage["omacp"]["verified"] < coverage["omacp"]["parameters"]
    assert coverage["omadm"]["management_objects"] >= 4
    assert len(coverage["vers_rules"]) == 5
    assert "UP_2.4" in coverage["omacp"]["available_profiles"]


def test_subscriber_overrides_reach_the_document(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.put(
        f"/admin/subscribers/{TEST_IMSI}",
        headers=admin_headers,
        json={
            "msisdn": TEST_MSISDN,
            "overrides": {"APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr": "42"},
        },
    )
    xml = complete_otp_flow(client)
    assert 'name="MaxSizeFileTr" value="42"' in xml


def test_dm_bootstrapped_flag_reflects_provisioning(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    before = client.get(f"/admin/subscribers/{TEST_IMSI}", headers=admin_headers).json()
    assert before["dm_bootstrapped"] is False
    complete_otp_flow(client)
    after = client.get(f"/admin/subscribers/{TEST_IMSI}", headers=admin_headers).json()
    assert after["dm_bootstrapped"] is True
