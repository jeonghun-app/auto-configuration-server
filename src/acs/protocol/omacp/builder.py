"""Build OMA-CP provisioning documents from the catalogue.

The builder is pure: it takes a subscriber, a parsed request and settings, and
returns a :class:`acs.protocol.omacp.document.ProvisioningDoc`. No I/O, no
randomness, no clock — which makes it the most heavily unit-tested component and
lets golden-file tests detect any accidental change to the wire format.
"""

from __future__ import annotations

import dataclasses
import string
from collections.abc import Iterable

from acs.config import Settings
from acs.protocol.identity import derive_identity
from acs.protocol.omacp.catalog import Catalog, CatalogEntry, get_catalog
from acs.protocol.omacp.document import (
    APP_ID_DM_ACCOUNT,
    APP_ID_IMS,
    APP_ID_RCS,
    Characteristic,
    ProvisioningDoc,
)
from acs.protocol.request import ConfigQuery


class _SafeFormatter(string.Formatter):
    """``str.format`` that leaves unknown placeholders untouched."""

    def get_value(  # noqa: ARG002
        self,
        key: object,
        args: object,  # noqa: ARG002 - fixed stdlib signature
        kwargs: object,
    ) -> object:
        if isinstance(key, str) and isinstance(kwargs, dict):
            return kwargs.get(key, "{" + key + "}")
        return ""  # pragma: no cover - positional placeholders are not used


_FORMATTER = _SafeFormatter()


@dataclasses.dataclass(frozen=True, slots=True)
class BuildContext:
    """Everything needed to resolve catalogue placeholders."""

    imsi: str
    msisdn: str
    device_id: str
    profile: str
    acs_host: str
    values: dict[str, str]

    @classmethod
    def create(
        cls,
        imsi: str,
        msisdn: str,
        device_id: str,
        profile: str,
        acs_host: str,
    ) -> BuildContext:
        identity = derive_identity(imsi, msisdn)
        values = identity.as_context()
        values.update(
            {
                "msisdn": msisdn,
                "msisdn_national": msisdn.lstrip("+"),
                "device_id": device_id,
                "profile": profile,
                "acs_host": acs_host,
            }
        )
        return cls(
            imsi=imsi,
            msisdn=msisdn,
            device_id=device_id,
            profile=profile,
            acs_host=acs_host,
            values=values,
        )

    def render(self, template: str) -> str:
        if "{" not in template:
            return template
        return _FORMATTER.vformat(template, (), self.values)


def _ensure_path(doc: ProvisioningDoc, path: str) -> Characteristic:
    """Resolve (creating if needed) the characteristic addressed by ``path``."""
    segments = path.split("/")
    head = segments[0]
    if head.startswith("APPLICATION:"):
        app_id = head.split(":", 1)[1]
        node = doc.application(app_id)
        if node is None:
            node = doc.add(Characteristic("APPLICATION"))
            node.add_parm("AppID", app_id)
    else:
        node = None
        for existing in doc.characteristics:
            if existing.type == head:
                node = existing
                break
        if node is None:
            node = doc.add(Characteristic(head))
    for segment in segments[1:]:
        node = node.child(segment)
    return node


def _coerce_value(entry: CatalogEntry, raw: str) -> str:
    if entry.type == "bool01":
        return "1" if raw in ("1", "true", "True", "yes") else "0"
    return raw


def vers_characteristic(version: int, validity: int) -> Characteristic:
    """The mandatory ``VERS`` characteristic.

    ``version`` is the configuration revision (or a disable/dormant value) and
    ``validity`` is the lifetime in seconds after which the client re-queries.
    """
    node = Characteristic("VERS")
    node.add_parm("version", str(version))
    node.add_parm("validity", str(validity))
    return node


def token_characteristic(token: str) -> Characteristic:
    node = Characteristic("TOKEN")
    node.add_parm("token", token)
    return node


def msg_characteristic(
    title: str,
    message: str,
    accept_button: bool = True,
    reject_button: bool = False,
) -> Characteristic:
    """The ``MSG`` characteristic: text the client shows to the user."""
    node = Characteristic("MSG")
    node.add_parm("title", title)
    node.add_parm("message", message)
    node.add_parm("Accept_btn", "1" if accept_button else "0")
    node.add_parm("Reject_btn", "1" if reject_button else "0")
    return node


def dm_account_characteristic(
    settings: Settings,
    context: BuildContext,
    dm_password: str,
) -> Characteristic:
    """Build the OMA-CP ``w7`` APPLICATION characteristic (DM account bootstrap).

    This is the bridge from the RCS ACS to the OMA-DM plane: once the device
    stores this DM account it can run SyncML DM sessions against the same server
    for VoLTE and general device management. Defined by OMA Provisioning Content
    (``w7`` = OMA DM bootstrap) and used by 3GPP/GSMA device management profiles.
    """
    node = Characteristic("APPLICATION")
    node.add_parm("AppID", APP_ID_DM_ACCOUNT)
    node.add_parm("PROVIDER-ID", settings.dm_server_id)
    node.add_parm("NAME", f"{settings.service_name} device management")
    node.add_parm("ADDR", settings.dm_account_uri)
    node.add_parm("AAUTHTYPE", "DIGEST" if settings.dm_auth_scheme == "md5" else "BASIC")
    node.add_parm("AAUTHNAME", context.imsi)
    node.add_parm("AAUTHSECRET", dm_password)
    node.add_parm("AAUTHDATA", settings.dm_server_id)
    node.add_parm("INIT", "1")
    return node


def build_document(
    *,
    settings: Settings,
    query: ConfigQuery,
    imsi: str,
    msisdn: str,
    version: int,
    validity: int,
    profile: str = "",
    token: str | None = None,
    overrides: dict[str, str] | None = None,
    catalog: Catalog | None = None,
    dm_password: str = "",
    acs_host: str = "",
) -> ProvisioningDoc:
    """Build a full configuration document.

    Order of precedence for every parameter value:
    subscriber override > catalogue default (profile overlay > base).

    Request-driven suppression rules:

    * ``default_sms_app=0`` — the client is not the default SMS application, so
      the messaging services it must not offer are switched off.
    * ``app=`` — when the client enumerates the AppIDs it wants, other
      APPLICATION blocks are omitted.
    """
    resolved_profile = profile or query.rcs_profile or settings.default_rcs_profile
    catalog = catalog or get_catalog(resolved_profile)
    context = BuildContext.create(
        imsi=imsi,
        msisdn=msisdn,
        device_id=query.device_key,
        profile=resolved_profile,
        acs_host=acs_host or settings.dm_account_uri,
    )

    doc = ProvisioningDoc()
    doc.add(vers_characteristic(version, validity))
    if token:
        doc.add(token_characteristic(token))

    wanted_apps = set(query.apps)
    override_map = overrides or {}

    for entry in _selected_entries(catalog, resolved_profile, wanted_apps):
        raw = override_map.get(entry.key, entry.default)
        if raw == "" and not entry.required:
            # An empty, non-mandatory value is omitted rather than emitted as
            # value="" — some clients treat the empty string as a real setting.
            continue
        value = _coerce_value(entry, context.render(raw))
        _ensure_path(doc, entry.path).add_parm(entry.parm, value)

    _apply_default_sms_app_policy(doc, query)

    if settings.dm_bootstrap_in_cp and settings.dm_enabled and dm_password:
        doc.add(dm_account_characteristic(settings, context, dm_password))

    return doc


def _selected_entries(
    catalog: Catalog, profile: str, wanted_apps: set[str]
) -> Iterable[CatalogEntry]:
    for entry in catalog.for_profile(profile):
        app_id = entry.app_id
        if wanted_apps and app_id is not None and app_id not in wanted_apps:
            continue
        yield entry


def _apply_default_sms_app_policy(doc: ProvisioningDoc, query: ConfigQuery) -> None:
    """Disable messaging authorisations when the client is not the default SMS app.

    Offering standalone messaging to a client that is not the default SMS
    application produces duplicate message delivery on the handset, so RCC.07
    ties these authorisations to ``default_sms_app``.
    """
    if query.default_sms_app is None or query.default_sms_app != 0:
        return
    rcs = doc.application(APP_ID_RCS)
    if rcs is None:
        return
    services = next((c for c in rcs.children if c.type == "SERVICES"), None)
    if services is None:
        return
    for parm in services.parms:
        if parm.name in ("standaloneMsgAuth", "ChatAuth", "GroupChatAuth"):
            parm.value = "0"


def build_vers_only_document(version: int, validity: int) -> ProvisioningDoc:
    """A document containing only ``VERS``.

    RCC.14 allows this when the client already holds the current configuration:
    it saves re-sending ~150 parameters on every validity refresh. Naive ACS
    implementations omit this optimisation and re-provision unnecessarily.
    """
    doc = ProvisioningDoc()
    doc.add(vers_characteristic(version, validity))
    return doc


def build_message_document(
    version: int,
    validity: int,
    title: str,
    message: str,
    accept_button: bool = True,
    reject_button: bool = False,
) -> ProvisioningDoc:
    """A ``MSG``-only document (terms and conditions, error text, MSISDN prompt)."""
    doc = ProvisioningDoc()
    doc.add(vers_characteristic(version, validity))
    doc.add(msg_characteristic(title, message, accept_button, reject_button))
    return doc


__all__ = [
    "APP_ID_IMS",
    "APP_ID_RCS",
    "BuildContext",
    "build_document",
    "build_message_document",
    "build_vers_only_document",
    "dm_account_characteristic",
    "msg_characteristic",
    "token_characteristic",
    "vers_characteristic",
]
