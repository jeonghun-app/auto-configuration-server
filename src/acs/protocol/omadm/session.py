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

Each answer is a package, and a package larger than the client's ``MaxMsgSize``
goes out as several messages (see :mod:`acs.protocol.omadm.package`). While a
package is incomplete the session stays in its phase and the remainder is kept
in a :class:`~acs.domain.models.DmPendingPackage` beside it; the client's next
messages, normally ``Alert`` 1222, are answered from the remainder until the
message that carries ``Final``. The remainder is read and written only for
authenticated messages.

Session state lives in the shared store, not in process memory, so a session can
survive being load balanced across ECS tasks mid-flow.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Final

from acs.config import Settings
from acs.domain.models import Device, DmPendingPackage, DmSession
from acs.observability import get_logger
from acs.protocol.identity import derive_identity
from acs.protocol.omadm import auth as dm_auth
from acs.protocol.omadm import motree, package, wbxml
from acs.protocol.omadm.syncml import (
    ALERT_CLIENT_INITIATED_MGMT,
    ALERT_END_OF_SESSION,
    ALERT_SERVER_INITIATED_MGMT,
    ALERT_SESSION_ABORT,
    AUTH_BASIC,
    AUTH_MD5,
    CONTENT_TYPE_WBXML,
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

Encoder = Callable[[bytes], bytes]
"""Turns a SyncML XML message into the bytes sent to the client."""

#: Phases whose client statuses answer server Add/Replace commands, so a failure
#: among them is a configuration the device did not apply.
_CONFIGURATION_PHASES: Final[frozenset[str]] = frozenset({"configure", "done"})


@dataclasses.dataclass(slots=True)
class DmResponse:
    status_code: int
    body: bytes = b""
    content_type: str = CONTENT_TYPE_XML
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    metric: str = ""
    detail: str = ""
    session_finished: bool = False


@dataclasses.dataclass(slots=True)
class _DmChanges:
    session: DmSession | None = None
    device: Device | None = None
    pending: DmPendingPackage | None = None
    delete_pending_id: str = ""
    delete_session_id: str = ""
    progress_in_remainder: bool = False
    """True while a package continues: the session's phase stays put and the
    remainder alone records how far the package has got."""

    def commit(self, store: Store) -> None:
        """Save the changes, writing last the record that holds the progress.

        The two records are separate writes, so either can fail after the other
        succeeded. Whatever records progress goes last, so a failure leaves it
        where it was and the client's retry gets the same answer again. When a
        package starts, the session's phase is the progress: if only the
        remainder was saved, its phase does not match the session's and the
        retry discards it. While a package continues, the remainder is the
        progress: if only the session was saved, the retry is sent the same
        message again.
        """
        if self.device is not None:
            store.put_device(self.device)
        if self.progress_in_remainder:
            self._save_session(store)
            self._save_remainder(store)
        else:
            self._save_remainder(store)
            self._save_session(store)

    def _save_remainder(self, store: Store) -> None:
        if self.delete_pending_id:
            store.delete_dm_pending(self.delete_pending_id)
        if self.pending is not None:
            store.put_dm_pending(self.pending)
        if self.delete_session_id:
            store.delete_dm_pending(self.delete_session_id)

    def _save_session(self, store: Store) -> None:
        if self.session is not None:
            store.put_dm_session(self.session)
        if self.delete_session_id:
            store.delete_dm_session(self.delete_session_id)


class _ChallengeTooLarge(package.MessageTooLarge):
    """An authentication challenge does not fit; reachable without credentials."""


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

        media_type = content_type.partition(";")[0].strip().lower()
        binary = media_type == CONTENT_TYPE_WBXML
        if "wbxml" in media_type and not binary:
            return DmResponse(
                status_code=415,
                metric="DmUnsupportedEncoding",
                detail="unsupported_content_type",
            )

        try:
            message = parse_syncml(wbxml.decode(payload) if binary else payload)
        except (SyncMlParseError, wbxml.WbxmlError) as exc:
            log.warning("dm parse failure", extra={"error": str(exc)})
            return DmResponse(status_code=400, metric="DmProtocolError", detail=str(exc))

        version = payload[0]

        def encode(xml: bytes) -> bytes:
            return wbxml.encode(xml, version=version) if binary else xml

        changes = _DmChanges()
        # Nothing reaches the store until a message has been encoded and fits, so
        # either failure leaves the request retryable.
        try:
            outcome = self._handle_message(message, changes, encode)
        except wbxml.WbxmlError as exc:
            log.error("dm response encoding failure", extra={"error": str(exc)})
            return DmResponse(
                status_code=500, metric="DmEncodingError", detail="response_encoding_failed"
            )
        except package.PackageTooLarge as exc:
            # What does not fit the next message has to be stored until it does.
            # Only very long client URIs or override values get here.
            log.warning("dm package remainder exceeds the session budget", extra={"size": exc.size})
            return DmResponse(status_code=413, metric="DmTooLarge", detail="package_too_large")
        except _ChallengeTooLarge as exc:
            # Kept apart from DmMessageTooLarge: anyone can cause this one without
            # credentials, so an alarm on DmMessageTooLarge must not count it.
            log.warning(
                "dm challenge does not fit the client MaxMsgSize",
                extra={"limit": exc.limit, "size": exc.size},
            )
            return DmResponse(
                status_code=400,
                metric="DmChallengeTooLarge",
                detail="challenge_exceeds_max_msg_size",
            )
        except package.MessageTooLarge as exc:
            # 4xx, not 5xx: the limit comes from the request, and a client must not
            # be able to set off the 5xx alarm by advertising a tiny MaxMsgSize.
            log.warning(
                "dm message does not fit the client MaxMsgSize",
                extra={"limit": exc.limit, "size": exc.size},
            )
            return DmResponse(
                status_code=400, metric="DmMessageTooLarge", detail="message_exceeds_max_msg_size"
            )
        if binary and outcome.body:
            outcome.content_type = CONTENT_TYPE_WBXML
        changes.commit(self._store)
        return outcome

    def _handle_message(
        self, message: SyncMlMessage, changes: _DmChanges, encode: Encoder
    ) -> DmResponse:
        header = message.header
        if not header.session_id or not header.msg_id:
            return DmResponse(
                status_code=400, metric="DmProtocolError", detail="missing SessionID or MsgID"
            )

        device_id = _device_id_from(header.source)
        # SessionID is chosen by the device and is commonly a small integer, so
        # keying on it alone lets two handsets share one server-side session.
        session_key = _session_key(device_id, header.session_id)
        # MemoryStore returns live objects. Keep mutations private until the
        # response is encoded so a failed response leaves the request retryable.
        session = deepcopy(self._store.get_dm_session(session_key)) or DmSession(
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
            changes.session = session
            return self._unauthorised(message, session, auth_result, encode)

        session.authenticated = True
        if auth_result.username:
            session.imsi = auth_result.username
        session.last_msg_id = int(header.msg_id) if header.msg_id.isdigit() else 0

        commands = len(self._acks(message))
        if commands > package.MAX_ACKNOWLEDGED_COMMANDS:
            # Every acknowledgement that does not fit the next message is stored
            # and rewritten on each continuation, so the count is capped.
            log.warning(
                "dm message carries too many commands",
                extra={"session": session.wire_session_id, "commands": commands},
            )
            return DmResponse(status_code=413, metric="DmTooLarge", detail="too_many_commands")

        # Read only now: a message that failed authentication never loads or
        # rewrites the remainder.
        pending = deepcopy(self._store.get_dm_pending(session.session_id))
        if pending is not None and pending.phase != session.phase:
            # A task running an older release answered in between and moved the
            # phase on without knowing about the remainder. It belongs to a
            # package that is over.
            log.info(
                "dm stale package remainder discarded",
                extra={"session": session.wire_session_id, "phase": pending.phase},
            )
            changes.delete_pending_id = session.session_id
            pending = None

        if message.has_alert(ALERT_SESSION_ABORT):
            # The client is abandoning the session; acknowledge and drop the
            # state, including any unsent part of a package, rather than
            # continuing to push commands at it.
            body = self._last_message(message, session, encode)
            changes.delete_session_id = session.session_id
            log.info("dm session aborted by client", extra={"session": session.session_id})
            return DmResponse(
                status_code=200,
                body=body,
                metric="DmSessionAborted",
                detail="client_abort",
                session_finished=True,
            )

        if message.has_alert(ALERT_END_OF_SESSION):
            body = self._last_message(message, session, encode)
            changes.delete_session_id = session.session_id
            return DmResponse(
                status_code=200,
                body=body,
                metric="DmSessionEnded",
                detail="client_ended",
                session_finished=True,
            )

        if pending is not None:
            return self._continue_package(message, session, pending, changes, encode)
        if session.phase == "init":
            return self._handle_init(message, session, changes, encode)
        if session.phase == "devinfo":
            return self._handle_devinfo(message, session, changes, encode)
        return self._handle_finish(message, session, changes, encode)

    # --------------------------------------------------------------- phases
    def _handle_init(
        self, message: SyncMlMessage, session: DmSession, changes: _DmChanges, encode: Encoder
    ) -> DmResponse:
        if not (
            message.has_alert(ALERT_CLIENT_INITIATED_MGMT)
            or message.has_alert(ALERT_SERVER_INITIATED_MGMT)
        ):
            return DmResponse(
                status_code=400,
                metric="DmProtocolError",
                detail="first package must carry Alert 1200 or 1201",
            )

        changes.device = self._absorb_device_values(message, session)

        uris = self._tree.device_query_uris()
        queue = self._acks(message) + [package.Item("Get", uri) for uri in uris]
        session.phase = "devinfo"
        body, remaining = self._start_package(message, session, queue, encode, changes)
        changes.session = session

        log.info(
            "dm session init",
            extra={
                "session": session.wire_session_id,
                "device_id": session.device_id,
                "gets": len(uris),
                "pending": remaining,
            },
        )
        return DmResponse(
            status_code=200,
            body=body,
            metric="DmInventoryRequested",
            detail=f"get:{len(uris)}",
        )

    def _handle_devinfo(
        self, message: SyncMlMessage, session: DmSession, changes: _DmChanges, encode: Encoder
    ) -> DmResponse:
        changes.device = self._absorb_device_values(message, session)

        values = self._configuration_values(session)

        # Create the interior nodes first. A Replace on ./3GPP_IMS/1/Timer_T1 gets
        # 404 on a device where the ./3GPP_IMS/1 instance does not exist yet, which
        # would silently abandon the whole configuration push.
        interiors = self._interior_nodes_for(values)
        queue = (
            self._acks(message)
            + [package.Item("Add", uri, "", "node", "node") for uri in interiors]
            + [package.Item("Replace", *value) for value in values]
        )
        session.phase = "configure"
        body, remaining = self._start_package(message, session, queue, encode, changes)
        changes.session = session

        log.info(
            "dm configuration pushed",
            extra={
                "session": session.wire_session_id,
                "device_id": session.device_id,
                "nodes": len(values),
                "interior_nodes": len(interiors),
                "pending": remaining,
            },
        )
        return DmResponse(
            status_code=200,
            body=body,
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

    def _handle_finish(
        self, message: SyncMlMessage, session: DmSession, changes: _DmChanges, encode: Encoder
    ) -> DmResponse:
        self._record_failures(message, session)
        session.phase = "done"
        body, remaining = self._start_package(
            message, session, self._acks(message), encode, changes
        )
        if remaining:
            changes.session = session
            return self._continued(body, remaining)
        return self._close(body, session, changes)

    def _continue_package(
        self,
        message: SyncMlMessage,
        session: DmSession,
        pending: DmPendingPackage,
        changes: _DmChanges,
        encode: Encoder,
    ) -> DmResponse:
        """Answer from the unsent part of the package, without changing phase.

        The client asks with ``Alert`` 1222. Anything else it sends meanwhile is
        answered the same way: until the package is complete the server has
        nothing else it may send. Results and statuses that arrive with the
        request are still recorded, so splitting cannot lose them.

        A package that needs more than
        :data:`~acs.protocol.omadm.package.MAX_MESSAGES_PER_PACKAGE` messages is
        abandoned with ``413`` and the session deleted: a legitimate client never
        needs that many, and each one rewrites the stored remainder.
        """
        if pending.messages >= package.MAX_MESSAGES_PER_PACKAGE:
            log.warning(
                "dm package exceeded the message limit",
                extra={"session": session.wire_session_id, "messages": pending.messages},
            )
            changes.delete_session_id = session.session_id
            return DmResponse(
                status_code=413,
                metric="DmTooLarge",
                detail="too_many_messages",
                session_finished=True,
            )
        changes.device = self._absorb_device_values(message, session)
        changes.progress_in_remainder = True
        if session.phase in _CONFIGURATION_PHASES:
            self._record_failures(message, session)
        acks = self._acks(message)
        # One queued entry beyond the acknowledgements is the progress that stops
        # a package from stalling while every message only answers the last one.
        body, rest = self._next_message(
            message,
            session,
            acks + package.from_stored(pending.entries),
            encode,
            required=len(acks) + 1,
        )
        if rest:
            pending.entries = package.to_stored(rest)
            pending.messages += 1
            changes.pending = pending
        else:
            changes.delete_pending_id = session.session_id
            if session.phase == "done":
                return self._close(body, session, changes)
        changes.session = session
        return self._continued(body, len(rest))

    def _record_failures(self, message: SyncMlMessage, session: DmSession) -> None:
        # 418 means the node was already there, which is the expected answer to an
        # Add of an interior node that the device already has. It is not a failure.
        tolerated = {STATUS_ALREADY_EXISTS}
        failures = [
            command
            for command in message.of("Status")
            if command.data and not command.data.startswith("2") and command.data not in tolerated
        ]
        if not failures:
            return
        session.reported_failures += len(failures)
        log.warning(
            "dm client reported command failures",
            extra={
                "session": session.wire_session_id,
                "codes": [c.data for c in failures],
            },
        )

    @staticmethod
    def _continued(body: bytes, remaining: int) -> DmResponse:
        return DmResponse(
            status_code=200,
            body=body,
            metric="DmPackageContinued",
            detail=f"pending:{remaining}",
        )

    @staticmethod
    def _close(body: bytes, session: DmSession, changes: _DmChanges) -> DmResponse:
        changes.delete_session_id = session.session_id
        failures = session.reported_failures
        return DmResponse(
            status_code=200,
            body=body,
            metric="DmSessionComplete" if not failures else "DmSessionCompleteWithErrors",
            detail=f"failures:{failures}",
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
        encode: Encoder,
    ) -> DmResponse:
        """Answer with SyncML ``Status`` 401 and a ``Chal``.

        The HTTP status stays 200: in OMA-DM the authentication outcome is carried
        inside the SyncML body, and returning HTTP 401 makes many DM clients abort
        the session instead of retrying with credentials.
        """
        scheme = AUTH_MD5 if self._settings.dm_auth_scheme == "md5" else AUTH_BASIC
        # 407 means "you sent none", 401 means "the ones you sent are wrong".
        # Collapsing both into 401 tells a client its credentials are bad when
        # it simply had not been challenged yet.
        code = (
            STATUS_MISSING_CREDENTIALS
            if auth_result.reason == "missing_credentials"
            else STATUS_INVALID_CREDENTIALS
        )
        try:
            body, _ = self._next_message(
                message,
                session,
                [],
                encode,
                required=0,
                header=(code, (scheme, auth_result.challenge_nonce)),
            )
        except package.MessageTooLarge as exc:
            raise _ChallengeTooLarge(exc.size, exc.limit) from exc
        log.info(
            "dm authentication rejected",
            extra={"session": session.wire_session_id, "reason": auth_result.reason},
        )
        return DmResponse(
            status_code=200,
            body=body,
            metric="DmAuthRejected",
            detail=auth_result.reason,
        )

    def _max_msg_size(self, message: SyncMlMessage) -> int:
        """The largest message the server may send in reply to *message*.

        The client's ``MaxMsgSize`` is what it can receive. Without one, the
        server's own setting still bounds what it sends.
        """
        negotiated = self._settings.dm_max_msg_size
        if message.header.max_msg_size:
            negotiated = min(negotiated, message.header.max_msg_size)
        return negotiated

    def _new_builder(self, message: SyncMlMessage, session: DmSession) -> SyncMlBuilder:
        # Never advertise more than the client said it can accept. The client
        # value was previously parsed and thrown away.
        return SyncMlBuilder(
            session_id=message.header.session_id,
            msg_id=int(message.header.msg_id) if message.header.msg_id.isdigit() else 1,
            target=message.header.source or session.device_id,
            source=self._settings.dm_account_uri,
            max_msg_size=self._max_msg_size(message),
        )

    def _next_message(
        self,
        message: SyncMlMessage,
        session: DmSession,
        queue: Sequence[package.Entry],
        encode: Encoder,
        *,
        required: int,
        header: tuple[str, tuple[str, str] | None] | None = None,
    ) -> tuple[bytes, Sequence[package.Entry]]:
        """Encode the next message of a package and return it with what is left.

        The message opens with the ``Status`` for the client's ``SyncHdr``, then
        holds as much of *queue* as fits the negotiated ``MaxMsgSize``, and never
        fewer than *required* entries. It carries ``Final`` only when nothing of
        *queue* is left over. *header* overrides the ``SyncHdr`` status code and
        challenge, which otherwise come from :meth:`_header_status`.
        """
        code, challenge = header or self._header_status(session)

        def build(count: int) -> bytes:
            builder = self._new_builder(message, session)
            builder.status(
                cmd="SyncHdr",
                msg_ref=message.header.msg_id,
                cmd_ref="0",
                code=code,
                target_ref=message.header.target,
                challenge=challenge,
            )
            package.apply(builder, queue[:count])
            return encode(builder.build(final=count == len(queue)))

        body, sent = package.fill(build, len(queue), self._max_msg_size(message), required)
        return body, queue[sent:]

    def _start_package(
        self,
        message: SyncMlMessage,
        session: DmSession,
        queue: Sequence[package.Entry],
        encode: Encoder,
        changes: _DmChanges,
    ) -> tuple[bytes, int]:
        """Send the first message of a new package and keep the rest.

        The remainder is recorded against the phase *session* is now in, which
        is the phase the client's next message will find. Returns the message
        and the number of entries left for later messages.
        """
        body, rest = self._next_message(
            message, session, queue, encode, required=min(1, len(queue))
        )
        if rest:
            changes.pending = DmPendingPackage(
                session_id=session.session_id,
                phase=session.phase,
                entries=package.to_stored(rest),
                expires_at=session.expires_at,
            )
        return body, len(rest)

    def _last_message(self, message: SyncMlMessage, session: DmSession, encode: Encoder) -> bytes:
        """Acknowledge *message* in one final message; the session ends with it."""
        acks = self._acks(message)
        body, _ = self._next_message(message, session, acks, encode, required=len(acks))
        return body

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

    def _header_status(self, session: DmSession) -> tuple[str, tuple[str, str] | None]:
        """The ``SyncHdr`` status code and challenge for an authenticated message."""
        challenge = None
        if session.authenticated and self._settings.dm_auth_scheme == "md5" and session.nonce:
            # Rotate the nonce on every successful authenticated message,
            # otherwise the previous credential stays replayable for the whole
            # session.
            challenge = (AUTH_MD5, session.nonce)
        return STATUS_AUTH_ACCEPTED if session.authenticated else STATUS_OK, challenge

    def _acks(self, message: SyncMlMessage) -> list[package.Status]:
        """A ``Status`` for every command in *message*, which ``Status`` itself does not get."""
        return [
            package.Status(
                cmd=command.name,
                msg_ref=message.header.msg_id,
                cmd_ref=str(command.cmd_id),
                code=self._status_for(command),
                target_ref=command.first_uri,
            )
            for command in message.commands
            if command.name != "Status"
        ]

    def _absorb_device_values(self, message: SyncMlMessage, session: DmSession) -> Device:
        """Record ``Replace``/``Results`` values the device reported about itself."""
        device_id = session.device_id or "unknown"
        device = deepcopy(self._store.get_device(device_id)) or Device(device_id=device_id)
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
        return device

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
