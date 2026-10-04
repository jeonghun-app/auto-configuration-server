"""SMPP 3.4 sender for an operator SMSC.

This is the only delivery path that can satisfy RCC.14's port-addressed OTP
requirement, because it can set the User Data Header that carries the destination
port. AWS SMS services send text only.

One short session per message: ``bind_transceiver`` -> ``submit_sm`` ->
``unbind``. An OTP is sent rarely and must not depend on a long-lived bind
surviving across ECS tasks, so the connection is not pooled. While waiting for a
response the session answers an ``enquire_link`` from the SMSC, because some SMSCs
drop a bind that ignores one.

Deliberate limits:

* No concatenation. A message whose user data exceeds one SMS (140 octets, or 160
  GSM 7-bit characters) is refused rather than split; an OTP never needs more.
* Only the standard library is used (``socket``/``ssl``), synchronously, to match
  the :class:`~acs.sms.base.SmsSender` interface.
* Verified against the in-process fake SMSC in ``tests/test_smpp.py`` only, not
  against a real operator SMSC.

Every failure raises :class:`SmppError`, an :class:`~acs.sms.base.UnsupportedDelivery`,
so the service answers ``503`` with ``Retry-After`` and deletes the challenge. Error
messages and log fields never contain the password, the message body or the
destination number.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
import socket
import ssl
import struct
import time
from typing import Final

from pydantic import SecretStr

from acs.domain.models import SmsMessage
from acs.observability import get_logger
from acs.sms.base import SmsDeliveryFailed, SmsRequest, SmsResult
from acs.store.base import Store

log = get_logger(__name__)

# ---- SMPP 3.4 command identifiers -----------------------------------------
GENERIC_NACK: Final = 0x80000000
SUBMIT_SM: Final = 0x00000004
SUBMIT_SM_RESP: Final = 0x80000004
DELIVER_SM: Final = 0x00000005
DELIVER_SM_RESP: Final = 0x80000005
UNBIND: Final = 0x00000006
UNBIND_RESP: Final = 0x80000006
BIND_TRANSCEIVER: Final = 0x00000009
BIND_TRANSCEIVER_RESP: Final = 0x80000009
ENQUIRE_LINK: Final = 0x00000015
ENQUIRE_LINK_RESP: Final = 0x80000015

_RESPONSE_BIT: Final = 0x80000000

# ---- SMPP 3.4 command_status values used here -----------------------------
ESME_ROK: Final = 0x00000000
ESME_RINVCMDLEN: Final = 0x00000002
ESME_RINVCMDID: Final = 0x00000003
# Temporary application error: the SMSC keeps the deliver_sm and retries it on a
# later bind, instead of treating a message this sender never reads as delivered.
ESME_RX_T_APPN: Final = 0x00000064

INTERFACE_VERSION: Final = 0x34

# ---- submit_sm field values -----------------------------------------------
ESM_CLASS_DEFAULT: Final = 0x00
ESM_CLASS_UDHI: Final = 0x40
DATA_CODING_DEFAULT: Final = 0x00
DATA_CODING_8BIT: Final = 0x04
DATA_CODING_UCS2: Final = 0x08

_HEADER: Final = struct.Struct(">IIII")
# Generous for any response this sender expects; refuses a garbage length before
# trying to read it, which would otherwise hang or allocate without bound.
_MAX_PDU: Final = 64 * 1024

# One SMS carries 140 octets of user data (3GPP TS 23.040).
MAX_USER_DATA_OCTETS: Final = 140
MAX_GSM7_CHARACTERS: Final = 160

# Characters whose GSM 7-bit default alphabet code equals their ASCII code, so
# sending them as one octet each under data_coding 0 is unambiguous. "@", "$",
# "_", the brackets and the backtick are not in this set because their GSM codes
# differ from ASCII.
_GSM7_ASCII_SAFE: Final = frozenset(
    " !\"#%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)

# C-Octet String maximum lengths, including the terminating NUL.
_MAX_SYSTEM_ID: Final = 16
_MAX_PASSWORD: Final = 9
_MAX_SYSTEM_TYPE: Final = 13
_MAX_ADDR: Final = 21
_MAX_MESSAGE_ID: Final = 65


class SmppError(SmsDeliveryFailed):
    """The SMSC could not be reached, refused the bind, or refused the message."""


@dataclasses.dataclass(frozen=True, slots=True)
class Pdu:
    command_id: int
    command_status: int
    sequence_number: int
    body: bytes = b""

    def encode(self) -> bytes:
        header = _HEADER.pack(
            _HEADER.size + len(self.body),
            self.command_id,
            self.command_status,
            self.sequence_number,
        )
        return header + self.body


def c_octet(value: str, max_length: int, field: str) -> bytes:
    """Encode a C-Octet String: ASCII, NUL-terminated, ``max_length`` including NUL."""
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"SMPP {field} must be ASCII") from None
    if b"\x00" in raw or len(raw) + 1 > max_length:
        raise ValueError(f"SMPP {field} must be at most {max_length - 1} characters")
    return raw + b"\x00"


def encode_short_message(request: SmsRequest, source_port: int = 0) -> tuple[int, int, bytes]:
    """Return ``(esm_class, data_coding, short_message)`` for ``request``.

    Port-addressed: 8-bit data with the application port UDH and UDHI set, so the
    handset routes it to the application listening on ``sms_port`` rather than to
    the inbox. Text: GSM 7-bit default alphabet when every character maps to the
    same code as ASCII, otherwise UCS-2.
    """
    if request.sms_port:
        try:
            udh = SmppSmsSender.build_udh(request.sms_port, source_port)
        except ValueError as exc:
            raise SmppError(str(exc)) from None
        user_data = udh + request.body.encode("utf-8")
        if len(user_data) > MAX_USER_DATA_OCTETS:
            raise SmppError(
                f"port-addressed message is {len(user_data)} octets; one SMS carries "
                f"{MAX_USER_DATA_OCTETS} and concatenation is not supported"
            )
        return ESM_CLASS_UDHI, DATA_CODING_8BIT, user_data
    if all(ch in _GSM7_ASCII_SAFE for ch in request.body):
        if len(request.body) > MAX_GSM7_CHARACTERS:
            raise SmppError(
                f"text message is {len(request.body)} characters; one SMS carries "
                f"{MAX_GSM7_CHARACTERS} and concatenation is not supported"
            )
        return ESM_CLASS_DEFAULT, DATA_CODING_DEFAULT, request.body.encode("ascii")
    ucs2 = request.body.encode("utf-16-be")
    if len(ucs2) > MAX_USER_DATA_OCTETS:
        raise SmppError(
            f"UCS-2 message is {len(ucs2)} octets; one SMS carries "
            f"{MAX_USER_DATA_OCTETS} and concatenation is not supported"
        )
    return ESM_CLASS_DEFAULT, DATA_CODING_UCS2, ucs2


class _Session:
    """One bound connection. Owns the sequence numbers and the read loop."""

    def __init__(self, sock: socket.socket, timeout: float) -> None:
        self._sock = sock
        self._timeout = timeout
        self._sequence = 0

    def next_sequence(self) -> int:
        # Valid sequence numbers are 0x00000001 to 0x7FFFFFFF.
        self._sequence = self._sequence % 0x7FFFFFFF + 1
        return self._sequence

    def write(self, pdu: Pdu) -> None:
        self._sock.sendall(pdu.encode())

    def request(self, command_id: int, body: bytes) -> Pdu:
        """Send a request and return its response, answering SMSC requests meanwhile."""
        sequence = self.next_sequence()
        self.write(Pdu(command_id, ESME_ROK, sequence, body))
        deadline = time.monotonic() + self._timeout
        expected = command_id | _RESPONSE_BIT
        while True:
            pdu = self._read(deadline)
            if pdu.command_id == GENERIC_NACK:
                raise SmppError(f"SMSC answered generic_nack, status 0x{pdu.command_status:08X}")
            if not pdu.command_id & _RESPONSE_BIT:
                self._answer(pdu)
                continue
            if pdu.command_id != expected or pdu.sequence_number != sequence:
                raise SmppError(
                    f"unexpected SMPP response 0x{pdu.command_id:08X} "
                    f"for sequence {pdu.sequence_number}"
                )
            return pdu

    def _answer(self, pdu: Pdu) -> None:
        if pdu.command_id == ENQUIRE_LINK:
            self.write(Pdu(ENQUIRE_LINK_RESP, ESME_ROK, pdu.sequence_number))
            return
        if pdu.command_id == UNBIND:
            self.write(Pdu(UNBIND_RESP, ESME_ROK, pdu.sequence_number))
            raise SmppError("SMSC unbound the session before responding")
        if pdu.command_id == DELIVER_SM:
            # A message_id C-Octet String, empty, is the whole deliver_sm_resp body.
            self.write(Pdu(DELIVER_SM_RESP, ESME_RX_T_APPN, pdu.sequence_number, b"\x00"))
            return
        self.write(Pdu(GENERIC_NACK, ESME_RINVCMDID, pdu.sequence_number))

    def _read(self, deadline: float) -> Pdu:
        header = self._read_exact(_HEADER.size, deadline)
        length, command_id, status, sequence = _HEADER.unpack(header)
        if length < _HEADER.size or length > _MAX_PDU:
            self.write(Pdu(GENERIC_NACK, ESME_RINVCMDLEN, sequence))
            raise SmppError(f"SMSC sent an invalid command_length {length}")
        body = self._read_exact(length - _HEADER.size, deadline)
        return Pdu(command_id, status, sequence, body)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(size - len(chunks))
            if not chunk:
                raise SmppError("SMSC closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)


class SmppSmsSender:
    """Send one SMS per SMPP 3.4 transceiver session."""

    name = "smpp"

    def __init__(
        self,
        host: str = "",
        port: int = 2775,
        system_id: str = "",
        password: SecretStr | None = None,
        *,
        system_type: str = "",
        source_addr: str = "",
        source_addr_ton: int = 5,
        source_addr_npi: int = 0,
        dest_addr_ton: int = 1,
        dest_addr_npi: int = 1,
        use_tls: bool = True,
        tls_ca_file: str = "",
        timeout: float = 10.0,
        store: Store | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password or SecretStr("")
        # Encoded once so an over-long credential fails at startup, not on the
        # first OTP.
        self._bind_body = (
            c_octet(system_id, _MAX_SYSTEM_ID, "system_id")
            + c_octet(self._password.get_secret_value(), _MAX_PASSWORD, "password")
            + c_octet(system_type, _MAX_SYSTEM_TYPE, "system_type")
            + bytes([INTERFACE_VERSION, 0, 0])
            + b"\x00"  # address_range: empty
        )
        for label, value in (
            ("source_addr_ton", source_addr_ton),
            ("source_addr_npi", source_addr_npi),
            ("dest_addr_ton", dest_addr_ton),
            ("dest_addr_npi", dest_addr_npi),
        ):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"SMPP {label} must fit in one octet")
        if source_addr:
            c_octet(source_addr, _MAX_ADDR, "source_addr")
        self._source_addr = source_addr
        self._source_ton = source_addr_ton
        self._source_npi = source_addr_npi
        self._dest_ton = dest_addr_ton
        self._dest_npi = dest_addr_npi
        self._use_tls = use_tls
        self._tls_ca_file = tls_ca_file
        self._timeout = timeout
        self._store = store
        self._reference_key = secrets.token_bytes(32)

    def __repr__(self) -> str:
        return f"SmppSmsSender(host={self._host!r}, port={self._port}, tls={self._use_tls})"

    @staticmethod
    def build_udh(destination_port: int, source_port: int = 0) -> bytes:
        """Build the 16-bit application port addressing UDH (IEI 0x05).

        It is the piece implementers most often get wrong, and it is pure data so
        it is unit tested on its own::

            05 04 <dest hi> <dest lo> <src hi> <src lo>

        preceded by the UDH length byte (0x06).
        """
        if not 0 <= destination_port <= 0xFFFF or not 0 <= source_port <= 0xFFFF:
            raise ValueError("ports must fit in 16 bits")
        body = bytes(
            [
                0x05,
                0x04,
                (destination_port >> 8) & 0xFF,
                destination_port & 0xFF,
                (source_port >> 8) & 0xFF,
                source_port & 0xFF,
            ]
        )
        return bytes([len(body)]) + body

    def submit_sm_body(self, request: SmsRequest) -> bytes:
        """Encode the submit_sm body for ``request``."""
        esm_class, data_coding, short_message = encode_short_message(request)
        source = self._source_addr or request.sender_id
        try:
            source_field = c_octet(source, _MAX_ADDR, "source_addr")
            dest_field = c_octet(request.msisdn.lstrip("+"), _MAX_ADDR, "destination_addr")
        except ValueError as exc:
            raise SmppError(str(exc)) from None
        return (
            b"\x00"  # service_type: SMSC default
            + bytes([self._source_ton, self._source_npi])
            + source_field
            + bytes([self._dest_ton, self._dest_npi])
            + dest_field
            + bytes([esm_class, 0, 0])  # esm_class, protocol_id, priority_flag
            + b"\x00\x00"  # schedule_delivery_time, validity_period: immediate, default
            + bytes([0, 0, data_coding, 0])  # registered_delivery, replace, coding, default_msg
            + bytes([len(short_message)])
            + short_message
        )

    def send(self, request: SmsRequest) -> SmsResult:
        if not self._host:
            raise SmppError("SMPP host is not configured")
        body = self.submit_sm_body(request)
        try:
            with self._connect() as sock:
                session = _Session(sock, self._timeout)
                bind = session.request(BIND_TRANSCEIVER, self._bind_body)
                if bind.command_status != ESME_ROK:
                    raise SmppError(f"SMSC refused the bind, status 0x{bind.command_status:08X}")
                submit = session.request(SUBMIT_SM, body)
                self._unbind(session)
                if submit.command_status != ESME_ROK:
                    raise SmppError(f"SMSC refused submit_sm, status 0x{submit.command_status:08X}")
                reference = self._message_reference(submit.body)
        except SmppError as exc:
            log.error("smpp send failed", extra={"error": str(exc), "smsc": self._host})
            raise
        except TimeoutError:
            log.error("smpp send timed out", extra={"smsc": self._host})
            raise SmppError(f"SMSC did not answer within {self._timeout:g}s") from None
        except (OSError, ssl.SSLError) as exc:
            # The OS message names the peer at most, never the PDU contents.
            log.error("smpp connection failed", extra={"error": str(exc), "smsc": self._host})
            raise SmppError(f"SMSC connection failed: {exc.__class__.__name__}") from None

        log.info(
            "smpp submit accepted",
            extra={"message_ref": reference, "binary": request.requires_binary},
        )
        self._audit(request, reference)
        return SmsResult(self.name, reference, request.requires_binary)

    def _message_reference(self, submit_sm_resp_body: bytes) -> str:
        """Return a log-safe reference for the SMSC's message_id.

        The message_id is whatever the SMSC chose to send, so it is never logged
        or stored: it could carry the MSISDN or the OTP. A keyed digest still lets
        log lines and the audit record of one send be matched up. The key lives
        only in this process, so the digest of a guessable value such as an MSISDN
        cannot be reversed offline.

        A malformed message_id does not fail the send. The SMSC answered ESME_ROK,
        so the SMS is on its way; answering 503 would delete the challenge whose
        code the user is about to receive.
        """
        end = submit_sm_resp_body.find(b"\x00")
        raw = submit_sm_resp_body if end < 0 else submit_sm_resp_body[:end]
        printable = all(0x20 <= octet <= 0x7E for octet in raw)
        if end < 0 or end + 1 > _MAX_MESSAGE_ID or not printable:
            log.warning(
                "smpp message_id is malformed",
                extra={
                    "message_id_length": len(raw),
                    "terminated": end >= 0,
                    "printable": printable,
                    "smsc": self._host,
                },
            )
        digest = hmac.new(self._reference_key, raw, hashlib.sha256).hexdigest()
        return f"smpp-{digest[:16]}"

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        if not self._use_tls:
            return sock
        context = ssl.create_default_context(cafile=self._tls_ca_file or None)
        try:
            return context.wrap_socket(sock, server_hostname=self._host)
        except BaseException:
            sock.close()
            raise

    def _unbind(self, session: _Session) -> None:
        # The submit_sm outcome is already known; a failed unbind must not change
        # it, least of all turn an accepted message into a 503.
        try:
            session.request(UNBIND, b"")
        except (SmppError, OSError):
            log.warning("smpp unbind did not complete", extra={"smsc": self._host})

    def _audit(self, request: SmsRequest, reference: str) -> None:
        if self._store is None:
            return
        # Audit the fact of the send, never the OTP body.
        self._store.record_sms(
            SmsMessage(
                msisdn=request.msisdn,
                body=f"<redacted:{reference}>",
                sms_port=request.sms_port,
                provider=self.name,
                binary=request.requires_binary,
            )
        )
