"""PII must never reach logs or metric dimensions in the clear."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN, base_query

from acs.config import Settings
from acs.observability import JsonFormatter, Metrics, configure_logging, get_logger
from acs.security.pii import hash_id, mask_tail, normalise_msisdn, redact, redact_mapping


# ------------------------------------------------------------------ helpers
def test_mask_keeps_only_the_tail() -> None:
    assert mask_tail("001010000000001") == "***********0001"
    assert mask_tail("abc") == "***"
    assert mask_tail("") == ""
    assert mask_tail(None) == ""


def test_hash_is_stable_and_keyed() -> None:
    assert hash_id(TEST_IMSI, "secret") == hash_id(TEST_IMSI, "secret")
    assert hash_id(TEST_IMSI, "secret") != hash_id(TEST_IMSI, "other-secret")
    assert len(hash_id(TEST_IMSI, "secret")) == 16


def test_unkeyed_hashing_is_refused() -> None:
    # An unkeyed hash of a 15-digit IMSI is brute-forceable, so refusing is
    # better than offering false assurance.
    with pytest.raises(ValueError, match="pii_hash_secret is required"):
        hash_id(TEST_IMSI, "")


def test_redact_modes() -> None:
    assert redact(TEST_IMSI, "mask").endswith("0001")
    assert redact(TEST_IMSI, "mask") != TEST_IMSI
    assert redact(TEST_IMSI, "none") == TEST_IMSI
    assert redact(TEST_IMSI, "hash", "secret") == hash_id(TEST_IMSI, "secret")
    assert redact(None, "mask") == ""


def test_mapping_redaction_covers_every_sensitive_key() -> None:
    payload = {
        "imsi": TEST_IMSI,
        "imei": TEST_IMEI,
        "msisdn": TEST_MSISDN,
        "device_id": TEST_IMEI,
        "device": TEST_IMEI,
        "otp": "123456",
        "token": "secret-token",
        "authorization": "Basic abc",
        "version": 3,
    }
    redacted = redact_mapping(payload, "mask")
    for key in (
        "imsi",
        "imei",
        "msisdn",
        "device_id",
        "device",
        "otp",
        "token",
        "authorization",
    ):
        assert redacted[key] != payload[key], key
    assert redacted["version"] == 3


def test_msisdn_normalisation_rejects_rubbish() -> None:
    assert normalise_msisdn("abc") is None
    assert normalise_msisdn("") is None
    assert normalise_msisdn(None) is None
    assert normalise_msisdn("+821012345678") == "+821012345678"


# ------------------------------------------------------------------ formatter
def capture(logger_name: str, settings: Settings) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        JsonFormatter(settings.service_name, settings.pii_log_mode, settings.pii_hash_secret)
    )
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_formatter_redacts_identifiers(settings: Settings) -> None:
    _, stream = capture("test.pii", settings)
    get_logger("test.pii").info("served", extra={"imsi": TEST_IMSI, "msisdn": TEST_MSISDN})
    payload = json.loads(stream.getvalue())
    assert payload["imsi"] != TEST_IMSI
    assert payload["msisdn"] != TEST_MSISDN
    assert TEST_IMSI not in stream.getvalue()


def test_formatter_survives_reserved_field_names(settings: Settings) -> None:
    # 'created', 'module', 'name' etc. are LogRecord attributes; using them as
    # structured fields must not raise.
    _, stream = capture("test.reserved", settings)
    get_logger("test.reserved").info(
        "ok", extra={"created": True, "module": "x", "name": "y", "args": [1]}
    )
    payload = json.loads(stream.getvalue())
    assert payload["created"] is True
    assert payload["module"] == "x"


def test_formatter_includes_exceptions(settings: Settings) -> None:
    _, stream = capture("test.exc", settings)
    logger = get_logger("test.exc")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    assert "ValueError" in json.loads(stream.getvalue())["exception"]


def test_hash_mode_produces_stable_pseudonyms() -> None:
    settings = Settings(env="test", pii_log_mode="hash", pii_hash_secret="k")
    _, stream = capture("test.hash", settings)
    logger = get_logger("test.hash")
    logger.info("a", extra={"imsi": TEST_IMSI})
    logger.info("b", extra={"imsi": TEST_IMSI})
    lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    assert lines[0]["imsi"] == lines[1]["imsi"]
    assert lines[0]["imsi"] != TEST_IMSI


# -------------------------------------------------------------------- metrics
def test_metric_dimensions_are_low_cardinality(capsys: pytest.CaptureFixture[str]) -> None:
    # Subscriber identifiers as dimensions would be both a PII leak and an
    # unbounded CloudWatch cost.
    Metrics("RcsAcs", "test").emit("ConfigServed", 1, dimensions={"Outcome": "ok"})
    document = json.loads(capsys.readouterr().out.strip())
    dimensions = document["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]
    assert set(dimensions) == {"Environment", "Outcome"}


def test_metric_document_is_valid_emf(capsys: pytest.CaptureFixture[str]) -> None:
    Metrics("RcsAcs", "test").emit("ConfigBytes", 42, unit="Bytes")
    document = json.loads(capsys.readouterr().out.strip())
    assert document["ConfigBytes"] == 42
    metric = document["_aws"]["CloudWatchMetrics"][0]
    assert metric["Namespace"] == "RcsAcs"
    assert metric["Metrics"][0] == {"Name": "ConfigBytes", "Unit": "Bytes"}


# --------------------------------------------------------------- end to end
def test_no_raw_identifier_appears_in_request_logs(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    client.get("/config", params=base_query())
    output = capsys.readouterr().out
    assert TEST_IMSI not in output
    assert TEST_IMEI not in output
    assert TEST_MSISDN not in output
    assert TEST_MSISDN.lstrip("+") not in output


def test_no_raw_identifier_appears_in_oma_dm_logs(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drives an *authenticated* session, which is where the DM logging happens.

    An earlier version of this test stopped at the authentication rejection and so
    never reached the session logging. CI's container log scan caught the leak that
    the weaker test missed — twice. The session key is namespaced by device id, so
    it carries an identifier of its own.
    """
    import base64

    from acs.app import create_app
    from acs.domain.models import Subscriber
    from acs.store.memory import MemoryStore

    store = MemoryStore()
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password="dm-secret"))
    credential = base64.b64encode(f"{TEST_IMSI}:dm-secret".encode()).decode()

    def package(msg_id: int, body: str) -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<SyncML xmlns="SYNCML:SYNCML1.2">
  <SyncHdr>
    <VerDTD>1.2</VerDTD><VerProto>DM/1.2</VerProto>
    <SessionID>77</SessionID><MsgID>{msg_id}</MsgID>
    <Target><LocURI>https://acs.example.com/dm</LocURI></Target>
    <Source><LocURI>IMEI:{TEST_IMEI}</LocURI><LocName>{TEST_IMSI}</LocName></Source>
    <Cred>
      <Meta>
        <Format xmlns="syncml:metinf">b64</Format>
        <Type xmlns="syncml:metinf">syncml:auth-basic</Type>
      </Meta>
      <Data>{credential}</Data>
    </Cred>
    <Meta><MaxMsgSize xmlns="syncml:metinf">16384</MaxMsgSize></Meta>
  </SyncHdr>
  <SyncBody>
{body}
    <Final/>
  </SyncBody>
</SyncML>
""".encode()

    with TestClient(create_app(settings, store)) as dm_client:
        capsys.readouterr()
        headers = {"Content-Type": "application/vnd.syncml.dm+xml"}
        first = dm_client.post(
            "/dm",
            content=package(1, "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>"),
            headers=headers,
        )
        assert first.status_code == 200
        # Second package reaches the configuration push, which logs again.
        dm_client.post(
            "/dm",
            content=package(2, "    <Results><CmdID>1</CmdID></Results>"),
            headers=headers,
        )
        output = capsys.readouterr().out

    assert TEST_IMEI not in output
    assert TEST_IMSI not in output
    assert "dm session init" in output, "the test must actually reach the session logging"


def test_uvicorn_access_log_is_disabled(settings: Settings) -> None:
    # The access log line contains the full query string: IMSI, OTP and token.
    configure_logging(settings)
    assert logging.getLogger("uvicorn.access").disabled is True


def test_configure_logging_is_idempotent(settings: Settings) -> None:
    configure_logging(settings)
    configure_logging(settings)
    assert len(logging.getLogger().handlers) == 1
