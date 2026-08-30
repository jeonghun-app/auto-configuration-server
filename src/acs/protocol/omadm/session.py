"""OMA-DM session handling.

The server drives a three-stage session:

``init``
    Package 1 arrived (client ``Alert`` 1200/1201 plus ``./DevInfo``). The server
    acknowledges everything, stores the reported device information and asks for
    the ``DevDetail`` inventory with ``Get``.

``devinfo``
    ``Results`` for those ``Get`` commands arrive. The server records them and
    pushes the configuration it owns with ``Replace``.

``configure``
    ``Status`` for the ``Replace`` commands arrives. Nothing is left to do, so
    the server answers with statuses and ``Final`` only, which ends the session.

Session state lives in the shared store, not in process memory, so a session can
survive being load balanced across ECS tasks mid-flow.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

from acs.config import Settings
from acs.domain.models import Device, DmSession
from acs.observability import get_logger
from acs.protocol.identity import derive_identity
from acs.protocol.omadm import auth as dm_auth
from acs.protocol.omadm import motree
from acs.protocol.omadm.syncml import (
    ALERT_CLIENT_INITIATED_MGMT,
    ALERT_END_OF_SESSION,
    ALERT_SERVER_INITIATED_MGMT,
    AUTH_BASIC,
    AUTH_MD5,
    CONTENT_TYPE_XML,
    STATUS_AUTH_ACCEPTED,
    STATUS_INVALID_CREDENTIALS,
    STATUS_NOT_FOUND,
    STATUS_OK,
    SyncMlBuilder,
    SyncMlMessage,
    SyncMlParseError,
)
from acs.protocol.omadm.syncml import parse as parse_syncml
from acs.store.base import Store

log = get_logger(__name__)


@dataclasses.dataclass(slots=True)
class DmResponse:
    status_code: int
    body: bytes = b""
    content_type: str = CONTENT_TYPE_XML
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    metric: str = ""
    detail: str = ""
    session_finished: bool = False


class DmService:
    """OMA-DM (SyncML DM 1.2) server."""

    def __init__(self, settings: Settings, store: Store, tree: motree.MoTree | None = None) -> None:
        self._settings = settings
        self._store = store
        self._tree = tree or motree.get_tree()

    # ------------------------------------------------------------------ API
    @property
    def tree(self) -> motree.MoTree:
        return self._tree

    def handle(self, payload: bytes, content_type: str = "") -> DmResponse:
        """Process one SyncML DM package."""
        if not self._settings.dm_enabled:
            return DmResponse(status_code=404, metric="DmDisabled", detail="dm_disabled")

        if len(payload) > self._settings.dm_max_msg_size * 4:
            return DmResponse(status_code=413, metric="DmTooLarge", detail="payload_too_large")

        if content_type and "wbxml" in content_type.lower():
            # Refusing explicitly is better than replying with XML the client
            # cannot decode.
            return DmResponse(
                status_code=415,
                metric="DmUnsupportedEncoding",
                detail="wbxml_not_supported",
            )

        try:
            message = parse_syncml(payload)
        except SyncMlParseError as exc:
            log.warning("dm parse failure", extra={"error": str(exc)})
            return DmResponse(status_code=400, metric="DmProtocolError", detail=str(exc))

        header = message.header
        if not header.session_id or not header.msg_id:
            return DmResponse(
                status_code=400, metric="DmProtocolError", detail="missing SessionID or MsgID"
            )

        device_id = _device_id_from(header.source)
        session = self._store.get_dm_session(header.session_id) or DmSession(
            session_id=header.session_id,
            device_id=device_id,
            expires_at=int(time.time()) + self._settings.dm_session_ttl_seconds,
        )
        if device_id:
            session.device_id = device_id

        auth_result = self._authenticate(message, session)
        if not auth_result.authenticated:
            session.nonce = auth_result.challenge_nonce
            session.authenticated = False
            self._store.put_dm_session(session)
            return self._unauthorised(message, session, auth_result)

        session.authenticated = True
        if auth_result.username:
            session.imsi = auth_result.username
        session.last_msg_id = int(header.msg_id) if header.msg_id.isdigit() else 0

        if message.has_alert(ALERT_END_OF_SESSION):
            self._store.delete_dm_session(session.session_id)
            return DmResponse(
                status_code=200,
                body=self._ack_only(message, session).build(final=True),
                metric="DmSessionEnded",
                detail="client_ended",
                session_finished=True,
            )

        if session.phase == "init":
            return self._handle_init(message, session)
        if session.phase == "devinfo":
            return self._handle_devinfo(message, session)
        return self._handle_finish(message, session)

    # --------------------------------------------------------------- phases
    def _handle_init(self, message: SyncMlMessage, session: DmSession) -> DmResponse:
        if not (
            message.has_alert(ALERT_CLIENT_INITIATED_MGMT)
            or message.has_alert(ALERT_SERVER_INITIATED_MGMT)
        ):
            return DmResponse(
                status_code=400,
                metric="DmProtocolError",
                detail="first package must carry Alert 1200 or 1201",
            )

        self._absorb_device_values(message, session)

        builder = self._ack_only(message, session)
        uris = self._tree.device_query_uris()
        builder.get(uris)
        session.phase = "devinfo"
        session.server_cmd_id = builder.command_count
        self._store.put_dm_session(session)

        log.info(
            "dm session init",
            extra={"session": session.session_id, "device": session.device_id, "gets": len(uris)},
        )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmInventoryRequested",
            detail=f"get:{len(uris)}",
        )

    def _handle_devinfo(self, message: SyncMlMessage, session: DmSession) -> DmResponse:
        self._absorb_device_values(message, session)

        builder = self._ack_only(message, session)
        values = self._configuration_values(session)
        builder.replace(values)
        session.phase = "configure"
        self._store.put_dm_session(session)

        log.info(
            "dm configuration pushed",
            extra={
                "session": session.session_id,
                "device": session.device_id,
                "nodes": len(values),
            },
        )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmConfigPushed",
            detail=f"replace:{len(values)}",
        )

    def _handle_finish(self, message: SyncMlMessage, session: DmSession) -> DmResponse:
        failures = [
            command
            for command in message.of("Status")
            if command.data and not command.data.startswith("2")
        ]
        builder = self._ack_only(message, session)
        self._store.delete_dm_session(session.session_id)
        if failures:
            log.warning(
                "dm client reported command failures",
                extra={
                    "session": session.session_id,
                    "codes": [c.data for c in failures],
                },
            )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmSessionComplete" if not failures else "DmSessionCompleteWithErrors",
            detail=f"failures:{len(failures)}",
            session_finished=True,
        )

    # -------------------------------------------------------------- helpers
    def _authenticate(self, message: SyncMlMessage, session: DmSession) -> dm_auth.DmAuthResult:
        scheme = self._settings.dm_auth_scheme
        credentials = message.header.credentials

        if scheme == "none":
            return dm_auth.authenticate(credentials, "none", lambda _u: None)

        if credentials is not None and credentials.type == AUTH_MD5:
            username = message.header.source_name or session.imsi
            expected = self._lookup_password(username)
            return dm_auth.authenticate_md5(credentials, username, expected, session.nonce)

        return dm_auth.authenticate(credentials, AUTH_BASIC, self._lookup_password, session.nonce)

    def _lookup_password(self, username: str) -> str | None:
        if not username:
            return None
        subscriber = self._store.get_subscriber(username)
        if subscriber is None or not subscriber.dm_password:
            return None
        return subscriber.dm_password

    def _unauthorised(
        self,
        message: SyncMlMessage,
        session: DmSession,
        auth_result: dm_auth.DmAuthResult,
    ) -> DmResponse:
        """Answer with SyncML ``Status`` 401 and a ``Chal``.

        The HTTP status stays 200: in OMA-DM the authentication outcome is carried
        inside the SyncML body, and returning HTTP 401 makes many DM clients abort
        the session instead of retrying with credentials.
        """
        scheme = AUTH_MD5 if self._settings.dm_auth_scheme == "md5" else AUTH_BASIC
        builder = self._new_builder(message, session)
        builder.status(
            cmd="SyncHdr",
            msg_ref=message.header.msg_id,
            cmd_ref="0",
            code=STATUS_INVALID_CREDENTIALS,
            target_ref=message.header.target,
            challenge=(scheme, auth_result.challenge_nonce),
        )
        log.info(
            "dm authentication rejected",
            extra={"session": session.session_id, "reason": auth_result.reason},
        )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmAuthRejected",
            detail=auth_result.reason,
        )

    def _new_builder(self, message: SyncMlMessage, session: DmSession) -> SyncMlBuilder:
        return SyncMlBuilder(
            session_id=message.header.session_id,
            msg_id=int(message.header.msg_id) if message.header.msg_id.isdigit() else 1,
            target=message.header.source or session.device_id,
            source=self._settings.dm_account_uri,
            max_msg_size=self._settings.dm_max_msg_size,
        )

    def _ack_only(self, message: SyncMlMessage, session: DmSession) -> SyncMlBuilder:
        """Acknowledge the header and every command in the incoming package."""
        builder = self._new_builder(message, session)
        builder.status(
            cmd="SyncHdr",
            msg_ref=message.header.msg_id,
            cmd_ref="0",
            code=STATUS_AUTH_ACCEPTED if session.authenticated else STATUS_OK,
            target_ref=message.header.target,
        )
        for command in message.commands:
            if command.name in ("Status",):
                continue
            code = STATUS_OK
            if command.name == "Results":
                code = STATUS_OK
            elif (
                command.name in ("Get", "Replace")
                and command.first_uri
                and (self._tree.node(command.first_uri) is None)
            ):
                code = STATUS_NOT_FOUND
            builder.status(
                cmd=command.name,
                msg_ref=message.header.msg_id,
                cmd_ref=str(command.cmd_id),
                code=code,
                target_ref=command.first_uri,
            )
        return builder

    def _absorb_device_values(self, message: SyncMlMessage, session: DmSession) -> None:
        """Record ``Replace``/``Results`` values the device reported about itself."""
        device_id = session.device_id or "unknown"
        device = self._store.get_device(device_id) or Device(device_id=device_id)
        if session.imsi:
            device.imsi = session.imsi

        for command in message.of("Replace", "Results", "Add"):
            for item in command.items:
                uri = item.uri
                if not uri or not item.data:
                    continue
                node = self._tree.node(uri)
                if node is None or node.source != "device":
                    continue
                device.mo_values[uri] = item.data

        device.manufacturer = device.mo_values.get("./DevInfo/Man", device.manufacturer)
        device.model = device.mo_values.get("./DevInfo/Mod", device.model)
        device.dm_client_version = device.mo_values.get("./DevInfo/DmV", device.dm_client_version)
        device.sw_version = device.mo_values.get("./DevDetail/SwV", device.sw_version)
        device.last_seen_at = int(time.time())
        self._store.put_device(device)

    def _configuration_values(self, session: DmSession) -> list[tuple[str, str, str, str]]:
        """Build the ``Replace`` payload for the nodes the server owns."""
        subscriber = self._store.get_subscriber(session.imsi) if session.imsi else None
        features: list[str] = ["rcs"]
        if subscriber is None or subscriber.volte_enabled:
            features.append("volte")

        imsi = subscriber.imsi if subscriber else (session.imsi or "001010000000000")
        msisdn = subscriber.msisdn if subscriber else ""
        identity = derive_identity(imsi, msisdn or None)
        context = motree.build_context(
            imsi=imsi,
            msisdn=msisdn,
            device_id=session.device_id,
            ims_domain=identity.ims_domain,
            impi=identity.impi,
            impu=identity.impu,
            extra={
                "provisioning_version": str(subscriber.provisioning_version if subscriber else 0)
            },
        )
        overrides = subscriber.overrides if subscriber else {}

        values: list[tuple[str, str, str, str]] = []
        for node in self._tree.server_nodes(features):
            rendered = self._tree.render(node, context, overrides)
            if not rendered:
                continue
            values.append((node.uri, rendered, node.format, node.type))
        return values


def _device_id_from(source: str) -> str:
    """Normalise the SyncHdr source LocURI into a device id.

    Clients send ``IMEI:35...``, ``urn:gsma:imei:35...`` or a bare identifier.
    """
    if not source:
        return ""
    value = source.strip()
    for prefix in ("urn:gsma:imei:", "urn:imei:", "IMEI:", "imei:"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def password_lookup_for(store: Store) -> Callable[[str], str | None]:
    """Expose the DM password lookup for tests and tooling."""

    def lookup(username: str) -> str | None:
        subscriber = store.get_subscriber(username)
        return subscriber.dm_password if subscriber and subscriber.dm_password else None

    return lookup
