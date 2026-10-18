"""SMPP over TLS to an SMSC whose certificate an operator private CA issued.

``test_qa_smpp_tls.py`` trusts a self-signed certificate that is its own CA. An
operator SMSC presents a leaf certificate, often with an issuing CA under a root,
so these build that hierarchy and pass the bundle the way ECS does: as the text of
``ACS_SMPP_TLS_CA_PEM`` in the environment, read through :class:`Settings`.
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from tests.test_qa_smpp_tls import TlsSmsc
from tests.test_smpp import PASSWORD, port_request

from acs.config import Settings
from acs.sms import build_sms_sender
from acs.sms.smpp import BIND_TRANSCEIVER, SmppError
from acs.store.memory import MemoryStore

Issued = tuple[x509.Certificate, ec.EllipticCurvePrivateKey]


def issue(cn: str, issuer: Issued | None = None, *, ca: bool = True) -> Issued:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    signer_cert, signer_key = issuer if issuer else (None, key)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer_cert.subject if signer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
    return builder.sign(signer_key, hashes.SHA256()), key


def pem(*certs: x509.Certificate) -> str:
    return "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in certs)


@pytest.fixture(scope="module")
def hierarchy() -> dict[str, Issued]:
    root = issue("Operator Root CA")
    issuing = issue("Operator Issuing CA", root)
    return {
        "root": root,
        "issuing": issuing,
        "leaf": issue("smsc", issuing, ca=False),
        "unrelated": issue("Unrelated CA"),
    }


def smsc(tmp_path: Path, chain: str, key: ec.EllipticCurvePrivateKey) -> TlsSmsc:
    chain_path, key_path = tmp_path / "chain.pem", tmp_path / "smsc.key"
    chain_path.write_text(chain)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return TlsSmsc(chain_path, key_path)


def send(monkeypatch: pytest.MonkeyPatch, server: TlsSmsc, bundle: str) -> str:
    monkeypatch.setenv("ACS_SMPP_TLS_CA_PEM", bundle)
    settings = Settings(
        env="prod",
        store_backend="dynamodb",
        sms_provider="smpp",
        smpp_host="127.0.0.1",
        smpp_port=server.port,
        smpp_system_id="acs",
        smpp_password=SecretStr(PASSWORD),
        smpp_timeout_seconds=2.0,
    )
    assert settings.validate_startup() == []
    return build_sms_sender(settings, MemoryStore()).send(port_request()).message_id


@pytest.mark.parametrize(
    ("served", "bundle"),
    [
        (("leaf", "issuing"), ("root",)),
        (("leaf",), ("root", "issuing")),
        (("leaf", "issuing"), ("unrelated", "root")),
    ],
    ids=[
        "chain-served-root-trusted",
        "leaf-served-root-and-issuing-trusted",
        "ca-second-in-bundle",
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_an_smsc_certificate_from_the_operator_ca_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hierarchy: dict[str, Issued],
    served: tuple[str, ...],
    bundle: tuple[str, ...],
    newline: str,
) -> None:
    leaf_key = hierarchy["leaf"][1]
    server = smsc(tmp_path, pem(*(hierarchy[n][0] for n in served)), leaf_key)
    text = pem(*(hierarchy[n][0] for n in bundle)).replace("\n", newline)
    assert send(monkeypatch, server, text).startswith("smpp-")
    assert server.received[0].command_id == BIND_TRANSCEIVER


def test_an_issuing_ca_without_its_root_starts_but_cannot_verify_the_smsc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hierarchy: dict[str, Issued]
) -> None:
    # The bundle must reach a self-signed root: the startup check only proves that
    # ssl can load it, so a bundle with the issuing CA alone fails every send.
    server = smsc(
        tmp_path, pem(hierarchy["leaf"][0], hierarchy["issuing"][0]), hierarchy["leaf"][1]
    )
    with pytest.raises(SmppError, match="connection failed"):
        send(monkeypatch, server, pem(hierarchy["issuing"][0]))
    server.close()
    assert server.received == []
