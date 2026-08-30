"""IMS identity derivation from the IMSI.

An RCS client locates its ACS at
``config.rcs.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org`` and the IMS identities are
derived from the same MCC/MNC pair, so this is shared logic between the OMA-CP
and OMA-DM planes.

MNC length is 2 or 3 digits depending on the country and cannot be inferred from
the IMSI alone. The table below lists the countries that use 3-digit MNCs; the
default is 2. It can be overridden per subscriber.
"""

from __future__ import annotations

import dataclasses
from typing import Final

#: MCCs whose operators use 3-digit MNCs (North America and a few others).
THREE_DIGIT_MNC_MCC: Final[frozenset[str]] = frozenset(
    {
        "302",  # Canada
        "310",  # United States
        "311",
        "312",
        "313",
        "314",
        "315",
        "316",
        "334",  # Mexico
        "338",  # Jamaica
        "342",  # Barbados
        "344",  # Antigua and Barbuda
        "346",  # Cayman Islands
        "348",  # British Virgin Islands
        "365",  # Anguilla
        "374",  # Trinidad and Tobago
        "708",  # Honduras
        "722",  # Argentina
        "732",  # Colombia
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class ImsIdentity:
    """The derived IMS identity set for a subscriber."""

    imsi: str
    mcc: str
    mnc: str
    ims_domain: str
    impi: str
    impu: str
    acs_fqdn: str

    def as_context(self) -> dict[str, str]:
        return {
            "imsi": self.imsi,
            "mcc": self.mcc,
            "mnc": self.mnc,
            "ims_domain": self.ims_domain,
            "impi": self.impi,
            "impu": self.impu,
            "acs_fqdn": self.acs_fqdn,
        }


def split_imsi(imsi: str, mnc_length: int | None = None) -> tuple[str, str]:
    """Split an IMSI into (MCC, MNC), both zero-padded to three digits.

    ``mnc_length`` overrides the country table when an operator's actual MNC
    length is known.
    """
    if len(imsi) < 5 or not imsi.isdigit():
        raise ValueError("IMSI must be at least 5 digits")
    mcc = imsi[:3]
    length = mnc_length if mnc_length in (2, 3) else (3 if mcc in THREE_DIGIT_MNC_MCC else 2)
    mnc = imsi[3 : 3 + length]
    return mcc, mnc.zfill(3)


def derive_identity(
    imsi: str,
    msisdn: str | None = None,
    mnc_length: int | None = None,
) -> ImsIdentity:
    """Derive IMPI, IMPU, home domain and ACS FQDN from the IMSI."""
    mcc, mnc = split_imsi(imsi, mnc_length)
    ims_domain = f"ims.mnc{mnc}.mcc{mcc}.3gppnetwork.org"
    impi = f"{imsi}@{ims_domain}"
    impu = f"sip:{msisdn}@{ims_domain}" if msisdn else f"sip:{impi}"
    acs_fqdn = f"config.rcs.mnc{mnc}.mcc{mcc}.pub.3gppnetwork.org"
    return ImsIdentity(
        imsi=imsi,
        mcc=mcc,
        mnc=mnc,
        ims_domain=ims_domain,
        impi=impi,
        impu=impu,
        acs_fqdn=acs_fqdn,
    )
