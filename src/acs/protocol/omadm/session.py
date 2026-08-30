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
    ALERT_SESSION_ABORT,
    AUTH_BASIC,
    AUTH_MD5,
    CONTENT_TYPE_XML,
    STATUS_ALREADY_EXISTS,
    STATUS_AUTH_ACCEPTED,
    STATUS_INVALID_CREDENTIALS,
    STATUS_MISSING_CREDENTIALS,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED,
    Command,
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
        # SessionID is chosen by the device and is commonly a small integer, so
        # keying on it alone lets two handsets share one server-side session.
        session_key = _session_key(device_id, header.session_id)
        session = self._store.get_dm_session(session_key) or DmSession(
            session_id=session_key,
            device_id=device_id,
            expires_at=int(time.time()) + self._settings.dm_session_ttl_seconds,
        )
        session.wire_session_id = header.session_id
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

        if message.has_alert(ALERT_SESSION_ABORT):
            # The client is abandoning the session; acknowledge and drop the
            # state rather than continuing to push commands at it.
            builder = self._ack_only(message, session)
            self._store.delete_dm_session(session.session_id)
            log.info("dm session aborted by client", extra={"session": session.session_id})
            return DmResponse(
                status_code=200,
                body=builder.build(final=True),
                metric="DmSessionAborted",
                detail="client_abort",
                session_finished=True,
            )

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
            extra={
                "session": session.wire_session_id,
                "device_id": session.device_id,
                "gets": len(uris),
            },
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

        # Create the interior nodes first. A Replace on ./3GPP_IMS/1/Timer_T1 gets
        # 404 on a device where the ./3GPP_IMS/1 instance does not exist yet, which
        # would silently abandon the whole configuration push.
        interiors = self._interior_nodes_for(values)
        builder.add([(uri, "", "node", "node") for uri in interiors])
        builder.replace(values)
        session.phase = "configure"
        self._store.put_dm_session(session)

        log.info(
            "dm configuration pushed",
            extra={
                "session": session.wire_session_id,
                "device_id": session.device_id,
                "nodes": len(values),
                "interior_nodes": len(interiors),
            },
        )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmConfigPushed",
            detail=f"add:{len(interiors)} replace:{len(values)}",
        )

    def _interior_nodes_for(self, values: list[tuple[str, str, str, str]]) -> list[str]:
        """Interior nodes that must exist before the given leaves can be written.

        Returned parent-first, so ``./3GPP_IMS`` precedes ``./3GPP_IMS/1``. Only
        nodes the catalogue declares as interior are created: inventing a node the
        management object does not define would itself be a protocol error.
        """
        needed: set[str] = set()
        for uri, _value, _fmt, _type in values:
            parts = uri.split("/")
            for depth in range(2, len(parts)):
                needed.add("/".join(parts[:depth]))
        declared = {n.uri for n in self._tree.all_nodes() if n.is_interior}
        return sorted(needed & declared, key=lambda u: (u.count("/"), u))

    def _handle_finish(self, message: SyncMlMessage, session: DmSession) -> DmResponse:
        # 418 means the node was already there, which is the expected answer to an
        # Add of an interior node that the device already has. It is not a failure.
        tolerated = {STATUS_ALREADY_EXISTS}
        failures = [
            command
            for command in message.of("Status")
            if command.data and not command.data.startswith("2") and command.data not in tolerated
        ]
        builder = self._ack_only(message, session)
        self._store.delete_dm_session(session.session_id)
        if failures:
            log.warning(
                "dm client reported command failures",
                extra={
                    "session": session.wire_session_id,
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
        # 407 means "you sent none", 401 means "the ones you sent are wrong".
        # Collapsing both into 401 tells a client its credentials are bad when
        # it simply had not been challenged yet.
        code = (
            STATUS_MISSING_CREDENTIALS
            if auth_result.reason == "missing_credentials"
            else STATUS_INVALID_CREDENTIALS
        )
        builder.status(
            cmd="SyncHdr",
            msg_ref=message.header.msg_id,
            cmd_ref="0",
            code=code,
            target_ref=message.header.target,
            challenge=(scheme, auth_result.challenge_nonce),
        )
        log.info(
            "dm authentication rejected",
            extra={"session": session.wire_session_id, "reason": auth_result.reason},
        )
        return DmResponse(
            status_code=200,
            body=builder.build(final=True),
            metric="DmAuthRejected",
            detail=auth_result.reason,
        )

    def _new_builder(self, message: SyncMlMessage, session: DmSession) -> SyncMlBuilder:
        # Never advertise more than the client said it can accept. The client
        # value was previously parsed and thrown away.
        negotiated = self._settings.dm_max_msg_size
        if message.header.max_msg_size:
            negotiated = min(negotiated, message.header.max_msg_size)
        return SyncMlBuilder(
            session_id=message.header.session_id,
            msg_id=int(message.header.msg_id) if message.header.msg_id.isdigit() else 1,
            target=message.header.source or session.device_id,
            source=self._settings.dm_account_uri,
            max_msg_size=negotiated,
        )

    #: Commands this server does not execute. Answering them with 200 would be a
    #: false claim of success — the client would believe a Delete or an Atomic
    #: block had been applied when nothing happened.
    UNSUPPORTED_COMMANDS: frozenset[str] = frozenset(
        {"Copy", "Delete", "Sequence", "Atomic", "Exec"}
    )

    def _status_for(self, command: Command) -> str:
        """Decide the SyncML Status code for one received command."""
        if command.name in self.UNSUPPORTED_COMMANDS:
            return STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED
        if (
            command.name in ("Get", "Replace", "Add")
            and command.first_uri
            and self._tree.node(command.first_uri) is None
        ):
            return STATUS_NOT_FOUND
        if command.name in ("Alert", "Results", "Get", "Replace", "Add", "Put"):
            return STATUS_OK
        # An unrecognised command name is not something we performed either.
        return STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED

    def _ack_only(self, message: SyncMlMessage, session: DmSession) -> SyncMlBuilder:
        """Acknowledge the header and every command in the incoming package."""
        builder = self._new_builder(message, session)
        challenge = None
        if session.authenticated and self._settings.dm_auth_scheme == "md5" and session.nonce:
            # Rotate the nonce on every successful authenticated message,
            # otherwise the previous credential stays replayable for the whole
            # session.
            challenge = (AUTH_MD5, session.nonce)
        builder.status(
            cmd="SyncHdr",
            msg_ref=message.header.msg_id,
            cmd_ref="0",
            code=STATUS_AUTH_ACCEPTED if session.authenticated else STATUS_OK,
            target_ref=message.header.target,
            challenge=challenge,
        )
        for command in message.commands:
            if command.name == "Status":
                continue
            builder.status(
                cmd=command.name,
                msg_ref=message.header.msg_id,
                cmd_ref=str(command.cmd_id),
                code=self._status_for(command),
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


def _session_key(device_id: str, wire_session_id: str) -> str:
    """Namespace the client-chosen SessionID by device."""
    return f"{device_id or 'unknown'}:{wire_session_id}"


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
