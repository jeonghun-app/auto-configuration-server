"""OMA Client Provisioning document model.

A ``wap-provisioningdoc`` is a tree of ``characteristic`` elements holding
``parm`` name/value pairs. The model is deliberately a plain tree with no XML
dependency so that the builder can be unit tested without serialising, and so
element order is explicit and deterministic (some clients are order sensitive).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

WAP_PROVISIONINGDOC_VERSION = "1.1"


@dataclasses.dataclass(slots=True)
class Parm:
    """A ``<parm name= value=/>`` leaf."""

    name: str
    value: str


@dataclasses.dataclass(slots=True)
class Characteristic:
    """A ``<characteristic type=...>`` node."""

    type: str
    parms: list[Parm] = dataclasses.field(default_factory=list)
    children: list[Characteristic] = dataclasses.field(default_factory=list)

    def add_parm(self, name: str, value: str) -> Characteristic:
        self.parms.append(Parm(name, value))
        return self

    def child(self, type_: str) -> Characteristic:
        """Return the existing child of that type, or create it."""
        for existing in self.children:
            if existing.type == type_:
                return existing
        created = Characteristic(type_)
        self.children.append(created)
        return created

    def find_parm(self, name: str) -> Parm | None:
        for parm in self.parms:
            if parm.name == name:
                return parm
        return None

    def walk(self, prefix: str = "") -> Iterator[tuple[str, Parm]]:
        """Yield ``(dotted path, parm)`` for every parm in the subtree."""
        here = f"{prefix}/{self.type}" if prefix else self.type
        for parm in self.parms:
            yield here, parm
        for child in self.children:
            yield from child.walk(here)

    def parm_count(self) -> int:
        return len(self.parms) + sum(c.parm_count() for c in self.children)


@dataclasses.dataclass(slots=True)
class ProvisioningDoc:
    """The root ``wap-provisioningdoc``."""

    version: str = WAP_PROVISIONINGDOC_VERSION
    characteristics: list[Characteristic] = dataclasses.field(default_factory=list)

    def add(self, characteristic: Characteristic) -> Characteristic:
        self.characteristics.append(characteristic)
        return characteristic

    def application(self, app_id: str) -> Characteristic | None:
        """Return the APPLICATION characteristic carrying ``AppID=app_id``."""
        for characteristic in self.characteristics:
            if characteristic.type != "APPLICATION":
                continue
            parm = characteristic.find_parm("AppID")
            if parm is not None and parm.value == app_id:
                return characteristic
        return None

    def parm_count(self) -> int:
        return sum(c.parm_count() for c in self.characteristics)

    def paths(self) -> list[str]:
        """Every ``characteristic/.../parm`` path, for coverage reporting."""
        out: list[str] = []
        for characteristic in self.characteristics:
            for path, parm in characteristic.walk():
                out.append(f"{path}/{parm.name}")
        return out


# --- Well-known application identifiers -----------------------------------
APP_ID_IMS = "ap2001"
"""IMS application (3GPP IMS Management Object, TS 24.167)."""

APP_ID_RCS = "ap2002"
"""RCS application (GSMA RCC.07 / Universal Profile settings)."""

APP_ID_IMS_MO_URN = "urn:oma:mo:ext-3gpp-ims:1.0"
"""Alternative AppID some RCC.14 revisions use for the IMS MO subtree."""

APP_ID_DM_ACCOUNT = "w7"
"""OMA Device Management account bootstrap (OMA-WAP-ProvCont ``w7``).

This is the bridge from the OMA-CP configuration document to the OMA-DM plane:
the ACS provisions the DM server account, after which the device can run DM
sessions for VoLTE and general device management.
"""
