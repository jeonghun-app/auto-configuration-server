"""Hostile SMSC traffic, boundary encodings and deployment configuration."""

from __future__ import annotations

import dataclasses
import logging
import socket
import struct
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from tests.conftest import TEST_MSISDN, base_query
from tests.test_smpp import (
    BODY,
    OTP,
    PASSWORD,
    FakeSmsc,
    default_behaviour,
    port_request,
    read_pdu,
    respond,
    sender_for,
    silent,
    text_request,
)

from acs.app import create_app
from acs.config import Settings
from acs.observability import JsonFormatter
from acs.sms import build_sms_sender
from acs.sms.base import SmsRequest
from acs.sms.smpp import (
    BIND_TRANSCEIVER,
    BIND_TRANSCEIVER_RESP,
    DELIVER_SM,
    DELIVER_SM_RESP,
    ENQUIRE_LINK,
    ENQUIRE_LINK_RESP,
    ESME_RX_T_APPN,
    GENERIC_NACK,
    SUBMIT_SM,
    SUBMIT_SM_RESP,
    UNBIND,
    Pdu,
    SmppError,
    SmppSmsSender,
    encode_short_message,
)
from acs.store.memory import MemoryStore


@pytest.fixture
def peer() -> Iterator[FakeSmsc]:
    smsc = FakeSmsc()
    yield smsc
    smsc.close()


def formatted_records(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter("qa", "mask", "")
    return "\n".join(formatter.format(record) for record in caplog.records)


@pytest.mark.spec
def test_every_response_can_arrive_one_octet_at_a_time(peer: FakeSmsc) -> None:
    def fragmented(_peer: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        response = Pdu(request.command_id | 0x80000000, 0, request.sequence_number, b"qa\x00")
        for octet in response.encode():
            conn.sendall(bytes([octet]))
        return request.command_id != UNBIND

    peer.behaviours = dict.fromkeys((BIND_TRANSCEIVER, SUBMIT_SM, UNBIND), fragmented)
    assert sender_for(peer).send(port_request()).binary
    assert peer.ids() == [BIND_TRANSCEIVER, SUBMIT_SM, UNBIND]


@pytest.mark.spec
@pytest.mark.parametrize("length", [0, 15, 65537, 0xFFFFFFFF])
def test_invalid_lengths_are_rejected_without_waiting_for_a_body(
    peer: FakeSmsc, length: int
) -> None:
    def corrupt(smsc: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        conn.sendall(
            struct.pack(">IIII", length, BIND_TRANSCEIVER_RESP, 0, request.sequence_number)
        )
        answer, _ = read_pdu(conn)
        smsc.record(answer)
        return False

    peer.behaviours[BIND_TRANSCEIVER] = corrupt
    started = time.monotonic()
    with pytest.raises(SmppError, match="invalid command_length"):
        sender_for(peer, timeout=0.3).send(port_request())
    assert time.monotonic() - started < 1
    assert peer.wait_for_received(2)[-1].command_id == GENERIC_NACK
    assert SUBMIT_SM not in peer.ids()


@pytest.mark.spec
@pytest.mark.parametrize(
    ("command_id", "sequence"), [(SUBMIT_SM_RESP, 1), (BIND_TRANSCEIVER_RESP, 2)]
)
def test_a_wrong_bind_response_never_releases_a_submit(
    peer: FakeSmsc, command_id: int, sequence: int
) -> None:
    def wrong(_peer: FakeSmsc, conn: socket.socket, _request: Pdu) -> bool:
        conn.sendall(Pdu(command_id, 0, sequence, b"qa\x00").encode())
        return False

    peer.behaviours[BIND_TRANSCEIVER] = wrong
    with pytest.raises(SmppError, match="unexpected SMPP response"):
        sender_for(peer).send(port_request())
    assert SUBMIT_SM not in peer.ids()


@pytest.mark.parametrize(
    ("command_id", "response_id", "status"),
    [(ENQUIRE_LINK, ENQUIRE_LINK_RESP, 0), (DELIVER_SM, DELIVER_SM_RESP, ESME_RX_T_APPN)],
)
def test_infinite_smsc_requests_cannot_extend_the_submit_deadline(
    peer: FakeSmsc, command_id: int, response_id: int, status: int
) -> None:
    answers: list[Pdu] = []

    def flood(smsc: FakeSmsc, conn: socket.socket, _request: Pdu) -> bool:
        for sequence in range(100, 1000):
            conn.sendall(Pdu(command_id, 0, sequence, b"").encode())
            answer, _ = read_pdu(conn)
            answers.append(answer)
            if smsc.stop.wait(0.01):
                break
        return False

    peer.behaviours[SUBMIT_SM] = flood
    started = time.monotonic()
    with pytest.raises(SmppError, match="did not answer within"):
        sender_for(peer, timeout=0.15).send(port_request())
    elapsed = time.monotonic() - started
    assert 0.1 <= elapsed < 0.8
    assert len(answers) >= 2
    assert all((p.command_id, p.command_status) == (response_id, status) for p in answers)


def test_trickling_octets_does_not_restart_the_response_deadline(peer: FakeSmsc) -> None:
    def trickle(smsc: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        response = Pdu(BIND_TRANSCEIVER_RESP, 0, request.sequence_number, b"qa\x00")
        for octet in response.encode():
            conn.sendall(bytes([octet]))
            if smsc.stop.wait(0.02):
                break
        return False

    peer.behaviours[BIND_TRANSCEIVER] = trickle
    started = time.monotonic()
    with pytest.raises(SmppError, match="did not answer within"):
        sender_for(peer, timeout=0.15).send(port_request())
    assert time.monotonic() - started < 0.8
    assert SUBMIT_SM not in peer.ids()


@pytest.mark.parametrize(("delay", "succeeds"), [(0.02, True), (0.35, False)])
def test_a_response_on_either_side_of_the_deadline_has_the_expected_outcome(
    peer: FakeSmsc, delay: float, succeeds: bool
) -> None:
    def delayed(smsc: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        smsc.stop.wait(delay)
        return default_behaviour(smsc, conn, request)

    peer.behaviours[BIND_TRANSCEIVER] = delayed
    if succeeds:
        assert sender_for(peer, timeout=0.15).send(port_request()).binary
    else:
        with pytest.raises(SmppError, match="did not answer within"):
            sender_for(peer, timeout=0.15).send(port_request())


@pytest.mark.parametrize("partial", ["header", "body"])
def test_a_reset_mid_response_is_a_bounded_delivery_failure(
    peer: FakeSmsc, partial: str, caplog: pytest.LogCaptureFixture
) -> None:
    def reset(_peer: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        response = Pdu(SUBMIT_SM_RESP, 0, request.sequence_number, BODY.encode()).encode()
        conn.sendall(response[:7] if partial == "header" else response[:-1])
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        return False

    peer.behaviours[SUBMIT_SM] = reset
    with caplog.at_level(logging.INFO), pytest.raises(SmppError) as caught:
        sender_for(peer).send(port_request())
    output = formatted_records(caplog) + str(caught.value)
    for value in (PASSWORD, OTP, TEST_MSISDN, TEST_MSISDN.lstrip("+")):
        assert value not in output


def test_a_silent_tls_peer_cannot_hold_the_handshake_indefinitely(peer: FakeSmsc) -> None:
    # The fake waits for a PDU and never sends a TLS ServerHello.
    started = time.monotonic()
    with pytest.raises(SmppError, match="did not answer within"):
        sender_for(peer, use_tls=True, timeout=0.15).send(port_request())
    assert time.monotonic() - started < 0.8
    assert SUBMIT_SM not in peer.ids()


def test_a_silent_unbind_preserves_the_accepted_message_and_has_a_deadline(
    peer: FakeSmsc,
) -> None:
    peer.behaviours[UNBIND] = silent
    started = time.monotonic()
    result = sender_for(peer, timeout=0.15).send(port_request())
    assert result.message_id.startswith("smpp-")
    assert time.monotonic() - started < 0.8


@pytest.mark.parametrize(
    "failure",
    ["bind-refused", "submit-refused", "nack", "wrong-command", "closed", "timeout"],
)
def test_failure_bodies_never_reach_formatted_logs_or_public_exceptions(
    peer: FakeSmsc, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    hostile_body = f"{TEST_MSISDN}:{OTP}:{PASSWORD}".encode() + b"\x00"
    if failure == "bind-refused":
        peer.behaviours[BIND_TRANSCEIVER] = respond(0x0D, hostile_body)
    elif failure == "submit-refused":
        peer.behaviours[SUBMIT_SM] = respond(0x58, hostile_body)
    elif failure == "timeout":
        peer.behaviours[SUBMIT_SM] = silent
    elif failure == "closed":
        peer.behaviours[SUBMIT_SM] = lambda _s, _c, _p: False
    else:

        def unexpected(_peer: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
            command = GENERIC_NACK if failure == "nack" else BIND_TRANSCEIVER_RESP
            conn.sendall(Pdu(command, 3, request.sequence_number, hostile_body).encode())
            return True

        peer.behaviours[SUBMIT_SM] = unexpected
    with caplog.at_level(logging.INFO), pytest.raises(SmppError) as caught:
        sender_for(peer, timeout=0.15).send(port_request())
    recorded = formatted_records(caplog) + str(caught.value)
    assert "smpp" in recorded
    for value in (TEST_MSISDN, TEST_MSISDN.lstrip("+"), OTP, PASSWORD):
        assert value not in recorded


def test_a_waiting_smsc_does_not_block_health_requests(
    peer: FakeSmsc, settings: Settings, seeded_store: MemoryStore
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def waiting(smsc: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        entered.set()
        assert release.wait(timeout=3)
        return default_behaviour(smsc, conn, request)

    peer.behaviours[SUBMIT_SM] = waiting
    configured = settings.model_copy(
        update={
            "sms_provider": "smpp",
            "smpp_host": "127.0.0.1",
            "smpp_port": peer.port,
            "smpp_system_id": "qa",
            "smpp_password": SecretStr(PASSWORD),
            "smpp_timeout_seconds": 2,
            "smpp_tls": False,
        }
    )
    with (
        TestClient(create_app(configured, seeded_store)) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        slow = pool.submit(client.post, "/config", data=base_query(SMS_port=37273))
        try:
            assert entered.wait(timeout=2)
            started = time.monotonic()
            healthy = client.get("/healthz")
            assert healthy.status_code == 200
            assert time.monotonic() - started < 0.8
            assert not slow.done()
        finally:
            release.set()
        assert slow.result(timeout=3).status_code == 200


@pytest.mark.parametrize("field", ["sequence_number", "command_length", "command_status"])
def test_an_smsc_cannot_echo_the_otp_into_error_logs_via_numeric_header_fields(
    peer: FakeSmsc, caplog: pytest.LogCaptureFixture, field: str
) -> None:
    def echo(_peer: FakeSmsc, conn: socket.socket, request: Pdu) -> bool:
        if field == "command_length":
            wire = struct.pack(">IIII", int(OTP), SUBMIT_SM_RESP, 0, request.sequence_number)
        elif field == "command_status":
            wire = Pdu(SUBMIT_SM_RESP, int(OTP, 16), request.sequence_number).encode()
        else:
            wire = Pdu(SUBMIT_SM_RESP, 0, int(OTP), b"qa\x00").encode()
        conn.sendall(wire)
        return True

    peer.behaviours[SUBMIT_SM] = echo
    with caplog.at_level(logging.INFO), pytest.raises(SmppError) as caught:
        sender_for(peer).send(port_request())
    output = formatted_records(caplog) + str(caught.value)
    assert OTP not in output


@pytest.mark.spec
@pytest.mark.parametrize(
    ("body", "port", "expected_coding", "expected_length"),
    [
        ("A" * 160, None, 0, 160),
        ("한" * 70, None, 8, 140),
        ("@" * 70, None, 8, 140),
        ("🔒" * 35, None, 8, 140),
        ("a" * 133, 65535, 4, 140),
        ("한" * 44 + "a", 1, 4, 140),
        ("🔒" * 33 + "a", 1, 4, 140),
    ],
)
def test_multibyte_bodies_fit_the_wire_octet_budget(
    body: str, port: int | None, expected_coding: int, expected_length: int
) -> None:
    _, coding, encoded = encode_short_message(SmsRequest(TEST_MSISDN, body, port))
    assert (coding, len(encoded)) == (expected_coding, expected_length)
    with pytest.raises(SmppError, match="concatenation"):
        encode_short_message(SmsRequest(TEST_MSISDN, body + "a", port))


@pytest.mark.spec
@pytest.mark.parametrize(
    ("field", "maximum"),
    [("system_id", 15), ("password", 8), ("system_type", 12), ("source_addr", 20)],
)
def test_c_octet_fields_accept_the_limit_and_reject_overflow_or_embedded_nul(
    field: str, maximum: int
) -> None:
    for value, valid in (("a" * maximum, True), ("a" * (maximum + 1), False), ("a\x00b", False)):
        options = {field: SecretStr(value) if field == "password" else value}
        if valid:
            SmppSmsSender(**options)  # type: ignore[arg-type]
        else:
            with pytest.raises(ValueError, match=field):
                SmppSmsSender(**options)  # type: ignore[arg-type]


def test_a_nul_in_a_request_address_is_refused_before_connecting(peer: FakeSmsc) -> None:
    for request in (
        dataclasses.replace(text_request(), msisdn=TEST_MSISDN + "\x00ignored"),
        dataclasses.replace(text_request(), sender_id="RCS\x00ignored"),
    ):
        with pytest.raises(SmppError, match="addr"):
            sender_for(peer).send(request)
    assert peer.connections == 0


CREDENTIALS = ("smpp_host", "smpp_system_id", "smpp_password")
MISSING = [subset for count in (1, 2, 3) for subset in combinations(CREDENTIALS, count)]


@pytest.mark.parametrize("environment", ["staging", "prod"])
@pytest.mark.parametrize("missing", MISSING)
def test_every_missing_credential_combination_refuses_production_startup(
    environment: str, missing: tuple[str, ...]
) -> None:
    options = {
        "env": environment,
        "store_backend": "dynamodb",
        "sms_provider": "smpp",
        "smpp_host": "127.0.0.1",
        "smpp_system_id": "qa",
        "smpp_password": SecretStr(PASSWORD),
    }
    for field in missing:
        options[field] = SecretStr("") if field == "smpp_password" else ""
    settings = Settings(**options)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="sms_provider=smpp requires"):
        create_app(settings, MemoryStore())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("smpp_port", -1),
        ("smpp_port", 0),
        ("smpp_port", 65536),
        ("smpp_timeout_seconds", -1.0),
        ("smpp_timeout_seconds", 0.0),
        ("smpp_timeout_seconds", float("nan")),
        ("smpp_timeout_seconds", float("inf")),
    ],
)
def test_invalid_transport_settings_fail_before_the_first_otp(field: str, value: float) -> None:
    refused = False
    try:
        settings = Settings(
            env="prod",
            store_backend="dynamodb",
            sms_provider="smpp",
            smpp_host="127.0.0.1",
            smpp_system_id="qa",
            smpp_password=SecretStr(PASSWORD),
            **{field: value},  # type: ignore[arg-type]
        )
        refused = bool(settings.validate_startup())
        build_sms_sender(settings, MemoryStore())
    except (ValueError, ValidationError):
        refused = True
    assert refused, f"{field}={value} accepted at startup"


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf")])
def test_an_invalid_timeout_cannot_leave_a_never_sent_otp_pending(
    settings: Settings, seeded_store: MemoryStore, timeout: float
) -> None:
    configured = settings.model_copy(
        update={
            "sms_provider": "smpp",
            "smpp_host": "127.0.0.1",
            "smpp_system_id": "qa",
            "smpp_password": SecretStr(PASSWORD),
            "smpp_timeout_seconds": timeout,
            "smpp_tls": False,
        }
    )
    with TestClient(create_app(configured, seeded_store), raise_server_exceptions=False) as client:
        first = client.post("/config", data=base_query(SMS_port=37273))
        second = client.post("/config", data=base_query(SMS_port=37273))
    actual = (first.status_code, second.status_code, seeded_store.get_otp(TEST_MSISDN) is not None)
    assert actual == (503, 503, False), f"first_status, retry_status, pending_challenge={actual}"
