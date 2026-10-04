"""SMPP 3.4 sender against an in-process fake SMSC.

The fake speaks just enough SMPP to drive every branch of the client, and records
the exact bytes it received so the PDUs can be asserted octet by octet. It is not
a real SMSC: passing here proves the encoding and the session handling, not
interoperability with an operator's SMSC.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from pydantic import SecretStr, ValidationError
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN

from acs.config import Settings
from acs.domain.service import ProvisioningService
from acs.observability import JsonFormatter
from acs.protocol.request import ConfigQuery
from acs.sms import build_sms_sender
from acs.sms.base import SmsRequest, UnsupportedDelivery
from acs.sms.smpp import (
    BIND_TRANSCEIVER,
    BIND_TRANSCEIVER_RESP,
    DELIVER_SM,
    DELIVER_SM_RESP,
    ENQUIRE_LINK,
    ENQUIRE_LINK_RESP,
    ESME_RINVCMDID,
    ESME_RX_T_APPN,
    GENERIC_NACK,
    SUBMIT_SM,
    SUBMIT_SM_RESP,
    UNBIND,
    UNBIND_RESP,
    Pdu,
    SmppError,
    SmppSmsSender,
    describe_status,
    encode_short_message,
)
from acs.store.memory import MemoryStore

PASSWORD = "pw-4242"
OTP = "735190"
BODY = f"RCS activation code: {OTP}"
DEST = TEST_MSISDN.lstrip("+").encode()
HEADER = struct.Struct(">IIII")

ESME_RBINDFAIL = 0x0000000D
ESME_RTHROTTLED = 0x00000058


def recv_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError
        data.extend(chunk)
    return bytes(data)


def read_pdu(conn: socket.socket) -> tuple[Pdu, bytes]:
    header = recv_exact(conn, HEADER.size)
    length, command_id, status, sequence = HEADER.unpack(header)
    body = recv_exact(conn, length - HEADER.size)
    return Pdu(command_id, status, sequence, body), header + body


Behaviour = Callable[["FakeSmsc", socket.socket, Pdu], bool]
"""Answers one received PDU; returns False to end the session."""


@dataclasses.dataclass
class FakeSmsc:
    """A single-connection SMPP peer driven by per-command behaviours."""

    behaviours: dict[int, Behaviour] = dataclasses.field(default_factory=dict)
    received: list[Pdu] = dataclasses.field(default_factory=list)
    raw: list[bytes] = dataclasses.field(default_factory=list)
    connections: int = 0

    def __post_init__(self) -> None:
        self.stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self.connections += 1
        with conn:
            conn.settimeout(5)
            try:
                while True:
                    pdu, raw = read_pdu(conn)
                    self.received.append(pdu)
                    self.raw.append(raw)
                    handler = self.behaviours.get(pdu.command_id, default_behaviour)
                    if not handler(self, conn, pdu):
                        return
            except (ConnectionError, OSError):
                return

    def ids(self) -> list[int]:
        return [pdu.command_id for pdu in self.received]

    def close(self) -> None:
        self.stop.set()
        if self.connections == 0:
            # Wake the accept() so the thread ends now rather than at its timeout.
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        self._thread.join(timeout=5)
        self._listener.close()


def respond(status: int = 0, body: bytes = b"") -> Behaviour:
    def handler(_smsc: FakeSmsc, conn: socket.socket, pdu: Pdu) -> bool:
        conn.sendall(Pdu(pdu.command_id | 0x80000000, status, pdu.sequence_number, body).encode())
        return pdu.command_id != UNBIND

    return handler


def silent(smsc: FakeSmsc, _conn: socket.socket, _pdu: Pdu) -> bool:
    smsc.stop.wait(5)
    return False


def default_behaviour(smsc: FakeSmsc, conn: socket.socket, pdu: Pdu) -> bool:
    if pdu.command_id == BIND_TRANSCEIVER:
        return respond(body=b"FAKESMSC\x00")(smsc, conn, pdu)
    if pdu.command_id == SUBMIT_SM:
        return respond(body=b"msg-0001\x00")(smsc, conn, pdu)
    return respond()(smsc, conn, pdu)


def smsc_request_first(request: Pdu, then: Behaviour) -> Behaviour:
    """Send ``request`` to the client, record its answer, then run ``then``."""

    def handler(smsc: FakeSmsc, conn: socket.socket, pdu: Pdu) -> bool:
        conn.sendall(request.encode())
        answer, raw = read_pdu(conn)
        smsc.received.append(answer)
        smsc.raw.append(raw)
        return then(smsc, conn, pdu)

    return handler


@pytest.fixture
def smsc() -> Iterator[FakeSmsc]:
    fake = FakeSmsc()
    yield fake
    fake.close()


def sender_for(smsc: FakeSmsc, store: MemoryStore | None = None, **kwargs: object) -> SmppSmsSender:
    options: dict[str, object] = {
        "host": "127.0.0.1",
        "port": smsc.port,
        "system_id": "acs",
        "password": SecretStr(PASSWORD),
        "system_type": "OTP",
        "use_tls": False,
        "timeout": 0.5,
        "store": store,
    }
    options.update(kwargs)
    return SmppSmsSender(**options)  # type: ignore[arg-type]


def port_request() -> SmsRequest:
    return SmsRequest(msisdn=TEST_MSISDN, body=BODY, sms_port=37273, sender_id="RCS")


def text_request(body: str = BODY) -> SmsRequest:
    return SmsRequest(msisdn=TEST_MSISDN, body=body, sender_id="RCS")


# ---------------------------------------------------------------- encoding
@pytest.mark.spec
def test_bind_transceiver_pdu_is_encoded_octet_for_octet(smsc: FakeSmsc) -> None:
    sender_for(smsc).send(text_request())
    body = b"acs\x00" + PASSWORD.encode() + b"\x00" + b"OTP\x00" + bytes([0x34, 0, 0]) + b"\x00"
    expected = struct.pack(">IIII", 16 + len(body), 0x00000009, 0, 1) + body
    assert smsc.raw[0] == expected


@pytest.mark.spec
def test_port_addressed_submit_sm_carries_the_udh_with_udhi_and_8bit_coding(
    smsc: FakeSmsc,
) -> None:
    result = sender_for(smsc).send(port_request())

    user_data = bytes([0x06, 0x05, 0x04, 0x91, 0x99, 0x00, 0x00]) + BODY.encode()
    body = (
        b"\x00"  # service_type
        + bytes([5, 0])
        + b"RCS\x00"
        + bytes([1, 1])
        + DEST
        + b"\x00"
        + bytes([0x40, 0, 0])  # esm_class UDHI, protocol_id, priority_flag
        + b"\x00\x00"  # schedule_delivery_time, validity_period
        + bytes([0, 0, 0x04, 0])  # registered_delivery, replace, data_coding 8-bit, default id
        + bytes([len(user_data)])
        + user_data
    )
    assert smsc.raw[1] == struct.pack(">IIII", 16 + len(body), 0x00000004, 0, 2) + body
    assert result.message_id.startswith("smpp-")
    assert result.binary is True


def test_a_session_is_bind_submit_unbind_with_increasing_sequence_numbers(
    smsc: FakeSmsc,
) -> None:
    sender_for(smsc).send(text_request())
    assert smsc.ids() == [BIND_TRANSCEIVER, SUBMIT_SM, UNBIND]
    assert [pdu.sequence_number for pdu in smsc.received] == [1, 2, 3]


def test_text_otp_uses_the_default_alphabet_when_it_maps_to_ascii() -> None:
    esm_class, data_coding, short_message = encode_short_message(text_request())
    assert (esm_class, data_coding, short_message) == (0x00, 0x00, BODY.encode("ascii"))


def test_text_otp_falls_back_to_ucs2_for_characters_outside_the_safe_set() -> None:
    body = f"RCS 인증번호: {OTP}"
    esm_class, data_coding, short_message = encode_short_message(text_request(body))
    assert (esm_class, data_coding, short_message) == (0x00, 0x08, body.encode("utf-16-be"))


def test_an_ascii_character_with_a_different_gsm_code_is_not_sent_as_default_alphabet() -> None:
    # "@" is 0x00 in the GSM 7-bit default alphabet, not 0x40.
    _, data_coding, _ = encode_short_message(text_request(f"code@{OTP}"))
    assert data_coding == 0x08


def test_the_source_address_overrides_the_sender_id(smsc: FakeSmsc) -> None:
    sender_for(smsc, source_addr="15550100", source_addr_ton=1, source_addr_npi=1).send(
        text_request()
    )
    assert smsc.received[1].body[1:14] == bytes([1, 1]) + b"15550100\x00" + bytes([1, 1])


@pytest.mark.parametrize(
    "request_",
    [
        SmsRequest(msisdn=TEST_MSISDN, body="1" * 134, sms_port=37273),
        SmsRequest(msisdn=TEST_MSISDN, body="1" * 161),
        SmsRequest(msisdn=TEST_MSISDN, body="인" * 71),
    ],
    ids=["binary-over-140-octets", "gsm-over-160-characters", "ucs2-over-140-octets"],
)
def test_a_message_longer_than_one_sms_is_refused_before_connecting(
    smsc: FakeSmsc, request_: SmsRequest
) -> None:
    with pytest.raises(SmppError, match="concatenation is not supported"):
        sender_for(smsc).send(request_)
    assert smsc.connections == 0


def test_a_message_of_exactly_one_sms_is_accepted() -> None:
    _, _, binary = encode_short_message(SmsRequest(msisdn="+1", body="1" * 133, sms_port=1))
    assert len(binary) == 140
    _, _, text = encode_short_message(SmsRequest(msisdn="+1", body="1" * 160))
    assert len(text) == 160


def test_an_out_of_range_port_is_a_delivery_failure() -> None:
    with pytest.raises(SmppError, match="16 bits"):
        encode_short_message(SmsRequest(msisdn=TEST_MSISDN, body="x", sms_port=70000))


def test_an_unencodable_destination_is_a_delivery_failure(smsc: FakeSmsc) -> None:
    with pytest.raises(SmppError, match="destination_addr"):
        sender_for(smsc).send(SmsRequest(msisdn="+" + "1" * 21, body="x"))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"system_id": "s" * 16}, "system_id must be at most 15"),
        ({"password": SecretStr("p" * 9)}, "password must be at most 8"),
        ({"system_type": "t" * 13}, "system_type must be at most 12"),
        ({"system_id": "système"}, "system_id must be ASCII"),
        ({"source_addr": "1" * 21}, "source_addr must be at most 20"),
        ({"dest_addr_ton": 256}, "dest_addr_ton must fit in one octet"),
    ],
)
def test_invalid_bind_parameters_fail_at_construction(
    kwargs: dict[str, object], match: str
) -> None:
    options: dict[str, object] = {"host": "127.0.0.1", "system_id": "acs"}
    options.update(kwargs)
    with pytest.raises(ValueError, match=match):
        SmppSmsSender(**options)  # type: ignore[arg-type]


# ----------------------------------------------------------------- failures
def test_a_refused_bind_is_a_delivery_failure_and_nothing_is_submitted(
    smsc: FakeSmsc,
) -> None:
    smsc.behaviours[BIND_TRANSCEIVER] = respond(ESME_RBINDFAIL, b"\x00")
    with pytest.raises(SmppError, match=r"refused the bind, status ESME_RBINDFAIL \(0x0000000D\)"):
        sender_for(smsc).send(port_request())
    assert SUBMIT_SM not in smsc.ids()


def test_a_submit_sm_error_status_is_a_delivery_failure_after_unbinding(
    smsc: FakeSmsc,
) -> None:
    smsc.behaviours[SUBMIT_SM] = respond(ESME_RTHROTTLED, b"\x00")
    with pytest.raises(
        SmppError, match=r"refused submit_sm, status ESME_RTHROTTLED \(0x00000058\)"
    ):
        sender_for(smsc).send(port_request())
    assert smsc.ids()[-1] == UNBIND


def test_an_smsc_that_never_answers_times_out(smsc: FakeSmsc) -> None:
    smsc.behaviours[BIND_TRANSCEIVER] = silent
    started = time.monotonic()
    with pytest.raises(SmppError, match="did not answer within 0.3s"):
        sender_for(smsc, timeout=0.3).send(port_request())
    assert time.monotonic() - started < 2


def test_an_unanswered_unbind_does_not_fail_an_accepted_message(smsc: FakeSmsc) -> None:
    smsc.behaviours[UNBIND] = silent
    assert sender_for(smsc, timeout=0.3).send(port_request()).message_id.startswith("smpp-")


def test_a_generic_nack_is_a_delivery_failure(smsc: FakeSmsc) -> None:
    def nack(_smsc: FakeSmsc, conn: socket.socket, pdu: Pdu) -> bool:
        conn.sendall(Pdu(GENERIC_NACK, ESME_RINVCMDID, pdu.sequence_number).encode())
        return True

    smsc.behaviours[SUBMIT_SM] = nack
    with pytest.raises(SmppError, match=r"generic_nack, status ESME_RINVCMDID \(0x00000003\)"):
        sender_for(smsc).send(port_request())


def test_a_response_for_another_sequence_is_a_protocol_error(smsc: FakeSmsc) -> None:
    def wrong_sequence(_smsc: FakeSmsc, conn: socket.socket, pdu: Pdu) -> bool:
        conn.sendall(Pdu(SUBMIT_SM_RESP, 0, pdu.sequence_number + 7, b"x\x00").encode())
        return True

    smsc.behaviours[SUBMIT_SM] = wrong_sequence
    with pytest.raises(SmppError, match="unexpected SMPP response"):
        sender_for(smsc).send(port_request())


def test_an_invalid_command_length_is_refused_with_a_generic_nack(smsc: FakeSmsc) -> None:
    def garbage(smsc: FakeSmsc, conn: socket.socket, _pdu: Pdu) -> bool:
        conn.sendall(struct.pack(">IIII", 8, BIND_TRANSCEIVER_RESP, 0, 1))
        answer, _ = read_pdu(conn)
        smsc.received.append(answer)
        return False

    smsc.behaviours[BIND_TRANSCEIVER] = garbage
    with pytest.raises(SmppError, match="invalid command_length$"):
        sender_for(smsc).send(port_request())
    assert smsc.received[-1].command_id == GENERIC_NACK


def test_a_closed_connection_is_a_delivery_failure(smsc: FakeSmsc) -> None:
    smsc.behaviours[BIND_TRANSCEIVER] = lambda _s, _c, _p: False
    with pytest.raises(SmppError, match="closed the connection"):
        sender_for(smsc).send(port_request())


def test_an_unbind_from_the_smsc_is_answered_and_fails_the_send(smsc: FakeSmsc) -> None:
    smsc.behaviours[SUBMIT_SM] = smsc_request_first(Pdu(UNBIND, 0, 900), silent)
    with pytest.raises(SmppError, match="unbound the session"):
        sender_for(smsc).send(port_request())
    assert smsc.received[-1] == Pdu(UNBIND_RESP, 0, 900)


def test_an_unreachable_smsc_is_a_delivery_failure() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sender = SmppSmsSender(host="127.0.0.1", port=port, system_id="acs", use_tls=False)
    with pytest.raises(SmppError, match="connection failed: ConnectionRefusedError"):
        sender.send(port_request())


def test_a_failed_tls_handshake_is_a_delivery_failure(smsc: FakeSmsc) -> None:
    with pytest.raises(SmppError):
        sender_for(smsc, use_tls=True, timeout=0.3).send(port_request())


def test_an_unconfigured_host_is_a_delivery_failure() -> None:
    with pytest.raises(SmppError, match="host is not configured"):
        SmppSmsSender().send(port_request())


def test_every_smpp_failure_honours_the_undeliverable_contract() -> None:
    # The service turns UnsupportedDelivery into 503 + Retry-After and deletes the
    # challenge; an SMSC failure must take the same path.
    assert issubclass(SmppError, UnsupportedDelivery)


@pytest.mark.parametrize(
    ("status", "described"),
    [
        (0x0000000E, "ESME_RINVPASWD (0x0000000E)"),
        (0x00000400, "an SMSC vendor-specific status"),
        (0x00735190, "an unlisted status"),
    ],
)
def test_a_status_is_named_from_the_table_or_reported_by_class_only(
    status: int, described: str
) -> None:
    # An unlisted value is the SMSC's free choice and may spell the OTP.
    assert describe_status(status) == described


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_a_sender_built_with_an_unusable_timeout_fails_into_the_503_path(
    smsc: FakeSmsc, timeout: float
) -> None:
    with pytest.raises(SmppError, match="positive finite"):
        sender_for(smsc, timeout=timeout).send(port_request())
    assert smsc.connections == 0


@pytest.mark.parametrize("error", [ValueError, OverflowError])
def test_an_argument_the_socket_layer_refuses_is_a_delivery_failure(
    smsc: FakeSmsc, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> socket.socket:
        raise error(f"refused argument {TEST_MSISDN}")

    monkeypatch.setattr("acs.sms.smpp.socket.create_connection", refuse)
    with pytest.raises(SmppError, match=f"transport refused: {error.__name__}$") as caught:
        sender_for(smsc).send(port_request())
    # The exception text may quote the argument, so it is not carried over.
    assert TEST_MSISDN not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("smpp_port", 0), ("smpp_port", 65536), ("smpp_timeout_seconds", 61.0)],
)
def test_out_of_range_transport_settings_are_refused(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(env="test", **{field: value})  # type: ignore[arg-type]


# -------------------------------------------------------- SMSC-originated PDUs
@pytest.mark.spec
def test_an_enquire_link_from_the_smsc_is_answered_while_waiting(smsc: FakeSmsc) -> None:
    smsc.behaviours[SUBMIT_SM] = smsc_request_first(
        Pdu(ENQUIRE_LINK, 0, 777), respond(body=b"msg-0001\x00")
    )
    assert sender_for(smsc).send(port_request()).message_id.startswith("smpp-")
    assert Pdu(ENQUIRE_LINK_RESP, 0, 777) in smsc.received


def test_a_deliver_sm_is_left_with_the_smsc_for_redelivery(smsc: FakeSmsc) -> None:
    smsc.behaviours[SUBMIT_SM] = smsc_request_first(
        Pdu(DELIVER_SM, 0, 778, b"\x00" * 17), respond(body=b"msg-0001\x00")
    )
    sender_for(smsc).send(port_request())
    assert Pdu(DELIVER_SM_RESP, ESME_RX_T_APPN, 778, b"\x00") in smsc.received


def test_an_unknown_smsc_request_is_refused_with_a_generic_nack(smsc: FakeSmsc) -> None:
    smsc.behaviours[SUBMIT_SM] = smsc_request_first(
        Pdu(0x00000103, 0, 779), respond(body=b"msg-0001\x00")
    )
    sender_for(smsc).send(port_request())
    assert Pdu(GENERIC_NACK, ESME_RINVCMDID, 779) in smsc.received


# ------------------------------------------------------------------ privacy
@pytest.fixture
def smpp_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test", "mask", ""))
    logger = logging.getLogger("acs.sms.smpp")
    saved = (logger.handlers, logger.propagate, logger.level)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    yield stream
    logger.handlers, logger.propagate, logger.level = saved


@pytest.mark.parametrize(
    "behaviour",
    [None, respond(ESME_RTHROTTLED, b"\x00"), silent],
    ids=["accepted", "refused", "timeout"],
)
def test_neither_the_password_nor_the_otp_nor_the_msisdn_is_logged(
    smsc: FakeSmsc, smpp_log: io.StringIO, behaviour: Behaviour | None
) -> None:
    if behaviour is not None:
        smsc.behaviours[SUBMIT_SM] = behaviour
    sender = sender_for(smsc, timeout=0.3)
    error = ""
    try:
        sender.send(port_request())
    except SmppError as exc:
        error = str(exc)
    output = smpp_log.getvalue() + error + repr(sender)
    assert "smpp" in output
    for secret in (PASSWORD, OTP, TEST_MSISDN, DEST.decode()):
        assert secret not in output


def test_the_audit_record_never_holds_the_otp(smsc: FakeSmsc) -> None:
    store = MemoryStore()
    result = sender_for(smsc, store=store).send(port_request())
    record = store.list_sms(TEST_MSISDN)[0]
    assert record.body == f"<redacted:{result.message_id}>"
    assert record.binary is True
    assert record.sms_port == 37273


@pytest.mark.parametrize(
    ("message_id", "malformed"),
    [
        (b"x" * 64 + b"\x00", False),
        (b"x" * 65 + b"\x00", True),
        (b"x" * 300, True),
        (b"x\x1b[2Jx" * 4 + b"\x00", True),
        ("xé".encode() * 4 + b"\x00", True),
    ],
    ids=["64-characters", "65-characters", "unterminated", "control-octets", "non-ascii"],
)
def test_a_malformed_message_id_is_noted_without_failing_the_accepted_send(
    smsc: FakeSmsc, smpp_log: io.StringIO, message_id: bytes, malformed: bool
) -> None:
    smsc.behaviours[SUBMIT_SM] = respond(body=message_id)
    # Accepted by the SMSC, so the SMS is on its way: a 503 here would delete the
    # challenge whose code the user is about to receive.
    result = sender_for(smsc).send(port_request())
    assert result.message_id.startswith("smpp-")
    output = smpp_log.getvalue()
    assert ("smpp message_id is malformed" in output) is malformed
    if malformed:
        length = min(len(message_id.rstrip(b"\x00")), 65)
        assert f'"message_id_length": {length}' in output
        printable = all(0x20 <= octet <= 0x7E for octet in message_id.rstrip(b"\x00"))
        assert f'"printable": {str(printable).lower()}' in output
    assert "x" * 20 not in output
    assert "\x1b" not in output


def test_an_smsc_message_id_carrying_subscriber_data_is_never_recorded(
    smsc: FakeSmsc, smpp_log: io.StringIO
) -> None:
    hostile = f"{TEST_MSISDN}:{OTP}".encode()
    smsc.behaviours[SUBMIT_SM] = respond(body=hostile + b"\x00")
    store = MemoryStore()
    result = sender_for(smsc, store=store).send(port_request())
    recorded = smpp_log.getvalue() + result.message_id + store.list_sms(TEST_MSISDN)[0].body
    for secret in (TEST_MSISDN, DEST.decode(), OTP):
        assert secret not in recorded
    # The same message_id maps to the same reference, so log lines still correlate.
    assert result.message_id in smpp_log.getvalue()


# ------------------------------------------------------------ configuration
def test_the_factory_builds_the_smpp_sender(store: MemoryStore) -> None:
    settings = Settings(env="test", sms_provider="smpp", smpp_host="smsc.example.net")
    sender = build_sms_sender(settings, store)
    assert isinstance(sender, SmppSmsSender)


def test_the_smpp_password_is_not_exposed_by_the_settings() -> None:
    settings = Settings(env="test", sms_provider="smpp", smpp_password=SecretStr(PASSWORD))
    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings.model_dump())


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_smpp_without_a_host_or_credentials_refuses_to_start(env: str) -> None:
    settings = Settings(env=env, store_backend="dynamodb", sms_provider="smpp")  # type: ignore[arg-type]
    problems = " ".join(settings.validate_startup())
    assert "sms_provider=smpp requires smpp_host, smpp_system_id, smpp_password" in problems


def test_a_fully_configured_smpp_provider_starts_in_production() -> None:
    settings = Settings(
        env="prod",
        store_backend="dynamodb",
        sms_provider="smpp",
        smpp_host="smsc.example.net",
        smpp_system_id="acs",
        smpp_password=SecretStr(PASSWORD),
    )
    assert settings.validate_startup() == []


# ----------------------------------------------------------- service contract
def port_query() -> ConfigQuery:
    return ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, vers=0, sms_port=37273)


def test_an_smsc_failure_answers_503_and_deletes_the_challenge(
    settings: Settings, seeded_store: MemoryStore, smsc: FakeSmsc
) -> None:
    smsc.behaviours[BIND_TRANSCEIVER] = respond(ESME_RBINDFAIL, b"\x00")
    service = ProvisioningService(settings, seeded_store, sender_for(smsc))
    outcome = service.handle(port_query())
    assert outcome.status_code == 503
    assert "Retry-After" in outcome.headers
    assert seeded_store.get_otp(TEST_MSISDN) is None


def test_a_port_addressed_otp_reaches_the_smsc_through_the_service(
    settings: Settings, seeded_store: MemoryStore, smsc: FakeSmsc
) -> None:
    service = ProvisioningService(settings, seeded_store, sender_for(smsc, store=seeded_store))
    outcome = service.handle(port_query())
    assert outcome.status_code == 200
    short_message = smsc.received[1].body[-len(BODY) - 7 :]
    assert short_message.startswith(bytes([0x06, 0x05, 0x04, 0x91, 0x99, 0x00, 0x00]))
    assert seeded_store.get_otp(TEST_MSISDN) is not None
