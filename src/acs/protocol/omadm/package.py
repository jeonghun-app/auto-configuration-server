"""Fit a server package into messages no larger than the client's ``MaxMsgSize``.

A DM package is everything one side has to say before it waits for the other.
When the server's package does not fit in one message, OMA-DM lets it send the
package as several messages: each but the last omits ``Final``, and the client
asks for the next one with ``Alert`` 1222 (OMA-TS-DM_Protocol, multiple messages
in a package). This module decides how much of a package goes into the next
message; :mod:`acs.protocol.omadm.session` keeps the rest on the session.

The package is held as a flat list of :data:`Entry` values rather than SyncML
elements, for two reasons. The remainder has to be stored between HTTP requests,
so it must be plain data. And one entry per ``Get``/``Add``/``Replace`` item, not
per command, lets a long ``Replace`` continue in the next message instead of
being an indivisible block that a small ``MaxMsgSize`` could never carry.

The client's limit is on the bytes it receives, so a candidate message is
measured after encoding it, XML or WBXML, never estimated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Final, NamedTuple

from acs.protocol.omadm.syncml import SyncMlBuilder

STATUS: Final = "Status"

#: Client commands one message may ask the server to acknowledge. A device
#: sends a handful per message (Alert, Replace, Results); acknowledgements are
#: the only part of a package a client controls, so this bounds what a request
#: can make the server store.
MAX_ACKNOWLEDGED_COMMANDS: Final = 100

#: Messages one package may take. Every continuation rewrites the stored
#: remainder, so this bounds that work per package. The longest legitimate
#: package measured, MAX_ACKNOWLEDGED_COMMANDS acknowledgements plus the
#: inventory Get at the smallest workable XML MaxMsgSize (about 1.3 KB), took 55.
MAX_MESSAGES_PER_PACKAGE: Final = 128

#: Budget for the stored remainder, measured as compact JSON, which over-counts
#: DynamoDB's size for lists of strings. The largest legitimate remainders
#: measured are about 5.5 KB (MAX_ACKNOWLEDGED_COMMANDS acknowledgements and the
#: inventory Get) and 2.5 KB (the shipped catalogue), so this leaves room for a
#: larger catalogue while bounding what each continuation rewrites.
MAX_STORED_BYTES: Final = 32 * 1024


class Status(NamedTuple):
    """A queued ``Status`` for command *cmd_ref* of client message *msg_ref*."""

    cmd: str
    msg_ref: str
    cmd_ref: str
    code: str
    target_ref: str = ""


class Item(NamedTuple):
    """One item of a server ``Get``, ``Add`` or ``Replace``."""

    command: str
    uri: str
    data: str = ""
    fmt: str = ""
    type_: str = ""


Entry = Status | Item


class MessageTooLarge(Exception):
    """Even the smallest message the server must send exceeds the client's limit."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"{size} bytes needed, {limit} allowed")
        self.size = size
        self.limit = limit


class PackageTooLarge(Exception):
    """The unsent part of a package is too large to keep on the session."""

    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes to store, {MAX_STORED_BYTES} allowed")
        self.size = size


def to_stored(entries: Sequence[Entry]) -> list[list[str]]:
    """Return *entries* in the form kept in ``DmSession.pending``.

    Rows are positional lists rather than maps because field names repeated on
    every acknowledgement would multiply the stored size several times over.

    :raises PackageTooLarge: if the rows exceed :data:`MAX_STORED_BYTES`.
    """
    rows = [[STATUS, *entry] if isinstance(entry, Status) else list(entry) for entry in entries]
    size = len(json.dumps(rows, separators=(",", ":")))
    if size > MAX_STORED_BYTES:
        raise PackageTooLarge(size)
    return rows


def from_stored(rows: Sequence[Sequence[str]]) -> list[Entry]:
    """Inverse of :func:`to_stored`."""
    return [Status(*row[1:]) if row[0] == STATUS else Item(*row) for row in rows]


def apply(builder: SyncMlBuilder, entries: Sequence[Entry]) -> None:
    """Add *entries* to *builder* as SyncML commands.

    Adjacent items of the same command travel as one command, so a package that
    fits in one message is the same on the wire as it was before splitting
    existed.
    """
    index = 0
    while index < len(entries):
        entry = entries[index]
        if isinstance(entry, Status):
            builder.status(
                cmd=entry.cmd,
                msg_ref=entry.msg_ref,
                cmd_ref=entry.cmd_ref,
                code=entry.code,
                target_ref=entry.target_ref,
            )
            index += 1
            continue
        group: list[Item] = []
        for following in entries[index:]:
            if not isinstance(following, Item) or following.command != entry.command:
                break
            group.append(following)
        values = [(i.uri, i.data, i.fmt, i.type_) for i in group]
        if entry.command == "Get":
            builder.get(i.uri for i in group)
        elif entry.command == "Add":
            builder.add(values)
        elif entry.command == "Replace":
            builder.replace(values)
        else:
            raise ValueError(f"unknown queued command {entry.command!r}")
        index += len(group)


def fill(build: Callable[[int], bytes], count: int, limit: int, required: int) -> tuple[bytes, int]:
    """Return the next message and how many of the *count* queued entries it holds.

    ``build(n)`` must return the encoded message carrying the first *n* entries,
    marked ``Final`` when ``n == count``. Its size must never shrink as *n* grows,
    which holds because entries are only ever appended.

    The message holds at least *required* entries. If even that does not fit,
    :class:`MessageTooLarge` is raised: sending it anyway would break the limit,
    and sending fewer would let the package stall.

    Most packages fit, so the whole queue is tried first and costs one build.
    Otherwise the search gallops up from *required* and then bisects, so a long
    queue is serialised only around the size that fits.
    """
    if count > required:
        whole = build(count)
        if len(whole) <= limit:
            return whole, count
    best = build(required)
    if len(best) > limit:
        raise MessageTooLarge(len(best), limit)
    fits = required
    # The smallest count known not to fit. Reaching here with count > required
    # means the whole queue did not.
    too_many = count if count > required else count + 1
    step = 1
    while fits + step < too_many:
        probe = fits + step
        body = build(probe)
        if len(body) > limit:
            too_many = probe
            break
        fits, best = probe, body
        step *= 2
    while too_many - fits > 1:
        probe = (fits + too_many) // 2
        body = build(probe)
        if len(body) <= limit:
            fits, best = probe, body
        else:
            too_many = probe
    return best, fits
