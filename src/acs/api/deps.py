"""Dependency container shared by the routers."""

from __future__ import annotations

import dataclasses

from fastapi import Request

from acs.auth.gba import BsfClient, build_bsf_client
from acs.config import Settings, get_settings
from acs.domain.service import ProvisioningService
from acs.observability import Metrics, get_metrics
from acs.protocol.omacp.catalog import get_catalog
from acs.protocol.omadm.motree import get_tree
from acs.protocol.omadm.session import DmService
from acs.sms import build_sms_sender
from acs.sms.base import SmsSender
from acs.store import build_store
from acs.store.base import Store


@dataclasses.dataclass(slots=True)
class AppState:
    """Everything constructed once at startup."""

    settings: Settings
    store: Store
    sms: SmsSender
    metrics: Metrics
    provisioning: ProvisioningService
    dm: DmService
    bsf: BsfClient | None

    @classmethod
    def build(cls, settings: Settings | None = None, store: Store | None = None) -> AppState:
        settings = settings or get_settings()
        store = store if store is not None else build_store(settings)
        sms = build_sms_sender(settings, store)
        bsf = build_bsf_client(settings.gba_enabled, settings.is_prod)
        return cls(
            settings=settings,
            store=store,
            sms=sms,
            metrics=get_metrics(settings),
            provisioning=ProvisioningService(settings, store, sms, bsf),
            dm=DmService(settings, store, get_tree()),
            bsf=bsf,
        )

    def warm_catalogues(self) -> tuple[int, int]:
        """Load and validate both catalogues, returning their sizes.

        Called at startup so a malformed catalogue fails the container rather than
        silently degrading to an empty configuration document — an empty document
        would switch RCS off on every real device that received it.
        """
        catalog = get_catalog(self.settings.default_rcs_profile)
        tree = get_tree()
        return len(catalog.entries), len(tree.all_nodes())


def state(request: Request) -> AppState:
    return request.app.state.acs  # type: ignore[no-any-return]
