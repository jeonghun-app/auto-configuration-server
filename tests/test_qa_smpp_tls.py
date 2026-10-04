"""SMPP over TLS: a session completes with a trusted SMSC and refuses an untrusted one.

``ACS_SMPP_TLS`` defaults to on because SMPP sends the password in clear, and the
other SMPP tests run with TLS off. These run a TLS fake SMSC with a throwaway
certificate for 127.0.0.1, so the success path, the CA bundle setting and
certificate verification are all exercised.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from tests.test_smpp import PASSWORD, default_behaviour, port_request, read_pdu

from acs.sms.smpp import BIND_TRANSCEIVER, SUBMIT_SM, UNBIND, Pdu, SmppError, SmppSmsSender


def write_certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-smsc")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "smsc.pem"
    key_path = directory / "smsc.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TlsSmsc:
    """One TLS connection answering with the default fake SMSC behaviour."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.received: list[Pdu] = []
        self.handshake_failed = False
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            raw, _ = self._listener.accept()
        except OSError:
            return
        raw.settimeout(5)
        try:
            conn = self.context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            self.handshake_failed = True
            raw.close()
            return
        with conn:
            try:
                while True:
                    pdu, _ = read_pdu(conn)
                    self.received.append(pdu)
                    if not default_behaviour(None, conn, pdu):  # type: ignore[arg-type]
                        return
            except (ConnectionError, OSError):
                return

    def close(self) -> None:
        self._thread.join(timeout=5)
        self._listener.close()


@pytest.fixture
def certificate(tmp_path: Path) -> tuple[Path, Path]:
    return write_certificate(tmp_path)


@pytest.fixture
def tls_smsc(certificate: tuple[Path, Path]) -> Iterator[TlsSmsc]:
    fake = TlsSmsc(*certificate)
    yield fake
    fake.close()


def tls_sender(smsc: TlsSmsc, ca_file: str) -> SmppSmsSender:
    return SmppSmsSender(
        host="127.0.0.1",
        port=smsc.port,
        system_id="acs",
        password=SecretStr(PASSWORD),
        use_tls=True,
        tls_ca_file=ca_file,
        timeout=2.0,
    )


def test_a_session_over_tls_completes_with_an_smsc_the_ca_bundle_trusts(
    tls_smsc: TlsSmsc, certificate: tuple[Path, Path]
) -> None:
    result = tls_sender(tls_smsc, str(certificate[0])).send(port_request())
    assert result.message_id.startswith("smpp-")
    assert result.binary is True
    assert [pdu.command_id for pdu in tls_smsc.received] == [BIND_TRANSCEIVER, SUBMIT_SM, UNBIND]


def test_an_smsc_certificate_outside_the_trusted_cas_fails_the_send(
    tls_smsc: TlsSmsc, caplog: pytest.LogCaptureFixture
) -> None:
    # No CA bundle: the system store does not trust the throwaway certificate, so
    # the password must never be sent.
    with pytest.raises(SmppError, match="connection failed"):
        tls_sender(tls_smsc, "").send(port_request())
    tls_smsc.close()
    assert tls_smsc.received == []
    assert PASSWORD not in caplog.text
