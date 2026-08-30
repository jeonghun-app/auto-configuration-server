"""SMPP sender — interface only.

This is the only delivery path that can satisfy RCC.14's port-addressed OTP
requirement, because it can set the User Data Header that carries the destination
port. Implementing it needs an SMSC account (host, port, system_id, password,
bind type), which is an operator commercial arrangement.

The class is deliberately left unimplemented rather than approximated: a silent
downgrade to text SMS would make the ACS look like it worked while the RCS client
waited forever for a port-addressed message that never arrives.
"""

from __future__ import annotations

from acs.sms.base import SmsRequest, SmsResult


class SmppSmsSender:
    """Placeholder for an operator SMSC binding over SMPP 3.4."""

    name = "smpp"

    def __init__(self, host: str = "", port: int = 2775, system_id: str = "") -> None:
        self._host = host
        self._port = port
        self._system_id = system_id

    @staticmethod
    def build_udh(destination_port: int, source_port: int = 0) -> bytes:
        """Build the 16-bit application port addressing UDH (IEI 0x05).

        Provided because it is the piece implementers most often get wrong, and
        it is pure data so it can be unit tested without an SMSC::

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

    def send(self, request: SmsRequest) -> SmsResult:
        raise NotImplementedError(
            "SMPP delivery requires an operator SMSC binding. Configure "
            "ACS_SMS_PROVIDER=eum for text OTP, or implement this class against "
            "your SMSC to support port-addressed (silent) OTP."
        )
