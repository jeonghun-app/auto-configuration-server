"""Server packages are split into messages no larger than the client's MaxMsgSize.

The client's limit is on the bytes it receives, so every assertion here measures
the encoded body that would go on the wire, in XML and in WBXML.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_conformance_protocol import cred, package

from acs.config import Settings
from acs.domain.models import Subscriber
from acs.protocol.omadm import package as omadm_package
from acs.protocol.omadm import syncml, wbxml
from acs.protocol.omadm.session import DmResponse, DmService
from acs.store.memory import MemoryStore

DM_PASSWORD = "dm-secret-123"
SESSION_KEY = f"{TEST_IMEI}:42"
INIT_BODY = (
    "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n"
    "    <Replace><CmdID>2</CmdID><Item><Source><LocURI>./DevInfo/Man</LocURI></Source>"
    "<Data>SimCorp</Data></Item></Replace>"
)
ENCODINGS = pytest.mark.parametrize("binary", [False, True], ids=["xml", "wbxml"])
#: Limits that split the init and configuration packages. WBXML needs a smaller
#: one: the same package is about a quarter of the size of its XML form.
SMALL: dict[bool, int] = {False: 1500, True: 400}
#: Below the smallest message any stage needs (833 bytes of XML, 208 of WBXML at
#: the time of writing), so no message the server could send would fit.
TOO_SMALL: dict[bool, int] = {False: 600, True: 150}


def next_message_body(msg_ref: int, extra: str = "") -> str:
    return (
        f"    <Status><CmdID>1</CmdID><MsgRef>{msg_ref}</MsgRef><CmdRef>0</CmdRef>"
        "<Cmd>SyncHdr</Cmd><Data>200</Data></Status>\n"
        f"{extra}"
        "    <Alert><CmdID>9</CmdID><Data>1222</Data></Alert>"
    )


def closing_body(msg_ref: int, code: str = "200") -> str:
    """The client's last package: the Status for the configuration Replace."""
    return (
        f"    <Status><CmdID>1</CmdID><MsgRef>{msg_ref}</MsgRef><CmdRef>2</CmdRef>"
        f"<Cmd>Replace</Cmd><Data>{code}</Data></Status>"
    )


class Client:
    """A DM client that honours its own MaxMsgSize and follows split packages."""

    def __init__(self, dm: DmService, limit: int, binary: bool) -> None:
        self.dm = dm
        self.limit = limit
        self.binary = binary
        self.msg_id = 0
        self.sizes: list[int] = []

    def send(self, body: str, authenticated: bool = True) -> DmResponse:
        self.msg_id += 1
        credential = cred() if authenticated else None
        xml = package(self.msg_id, body, credential=credential, max_msg_size=self.limit)
        if self.binary:
            return self.dm.handle(wbxml.encode(xml), syncml.CONTENT_TYPE_WBXML)
        return self.dm.handle(xml, syncml.CONTENT_TYPE_XML)

    def parse(self, outcome: DmResponse) -> syncml.SyncMlMessage:
        self.sizes.append(len(outcome.body))
        return read(outcome, self.binary, self.limit)

    def exchange(self, body: str) -> list[syncml.SyncMlMessage]:
        """Send *body*, then ask with Alert 1222 until the server's package is final."""
        messages = [self.parse(self.send(body))]
        while not messages[-1].final:
            messages.append(self.parse(self.send(next_message_body(self.msg_id))))
        return messages


def read(outcome: DmResponse, binary: bool, limit: int) -> syncml.SyncMlMessage:
    assert outcome.status_code == 200, outcome.detail
    assert len(outcome.body) <= limit
    return syncml.parse(wbxml.decode(outcome.body) if binary else outcome.body)


def items_of(messages: list[syncml.SyncMlMessage], name: str) -> list[tuple[str, str]]:
    return [(item.uri, item.data) for m in messages for c in m.of(name) for item in c.items]


def results_for(uris: list[str]) -> str:
    items = "".join(
        f"<Item><Source><LocURI>{uri}</LocURI></Source><Data>v-{uri.rsplit('/', 1)[-1]}</Data>"
        "</Item>"
        for uri in uris
    )
    return f"    <Results><CmdID>2</CmdID><MsgRef>1</MsgRef><CmdRef>1</CmdRef>{items}</Results>\n"


def run_session(client: Client) -> tuple[list[list[syncml.SyncMlMessage]], DmResponse]:
    """Drive init, inventory and configuration, then close; return each package."""
    inventory = client.exchange(INIT_BODY)
    uris = [uri for uri, _ in items_of(inventory, "Get")]
    configuration = client.exchange(next_message_body(client.msg_id, results_for(uris)))
    closing = client.send(closing_body(client.msg_id))
    return [inventory, configuration, [client.parse(closing)]], closing


@pytest.fixture
def dm_store(store: MemoryStore) -> MemoryStore:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    return store


@pytest.fixture
def dm(settings: Settings, dm_store: MemoryStore) -> DmService:
    return DmService(settings, dm_store)


def fresh_service(settings: Settings, dm_store: MemoryStore) -> DmService:
    store = MemoryStore()
    subscriber = dm_store.get_subscriber(TEST_IMSI)
    assert subscriber is not None
    store.put_subscriber(deepcopy(subscriber))
    return DmService(settings, store)


# ------------------------------------------------------ OMADM-SIZE-SPLITTING
@pytest.mark.spec
@pytest.mark.parametrize(
    ("binary", "limit"),
    [(False, 1500), (False, 4096), (True, 400), (True, 1024)],
    ids=["xml-1500", "xml-4096", "wbxml-400", "wbxml-1024"],
)
def test_no_message_of_a_whole_session_exceeds_the_client_max_msg_size(
    settings: Settings, dm_store: MemoryStore, binary: bool, limit: int
) -> None:
    whole = Client(fresh_service(settings, dm_store), 16384, binary)
    expected, _ = run_session(whole)
    assert [len(p) for p in expected] == [1, 1, 1], "the reference session must not split"

    dm = DmService(settings, dm_store)
    client = Client(dm, limit, binary)
    packages, closing = run_session(client)

    assert max(client.sizes) <= limit
    assert len(packages[1]) > 1, "the configuration push must have been split"
    for messages in packages:
        assert [m.final for m in messages] == [False] * (len(messages) - 1) + [True]
    # The split package carries the same commands, in the same order.
    for name in ("Get", "Add", "Replace"):
        assert items_of(packages[0] + packages[1], name) == items_of(
            expected[0] + expected[1], name
        )
    assert closing.metric == "DmSessionComplete"
    assert dm_store.get_dm_session(SESSION_KEY) is None
    device = dm_store.get_device(TEST_IMEI)
    assert device is not None and device.sw_version == "v-SwV"


@pytest.mark.spec
@ENCODINGS
def test_every_received_command_is_acknowledged_once_across_a_split_package(
    dm: DmService, binary: bool
) -> None:
    body = "    <Alert><CmdID>1</CmdID><Data>1201</Data></Alert>\n" + "".join(
        f"    <Replace><CmdID>{i}</CmdID></Replace>\n" for i in range(2, 202)
    )
    client = Client(dm, 2048, binary)
    messages = client.exchange(body)

    assert len(messages) > 2
    acknowledged = [
        s.cmd_ref
        for m in messages
        for s in m.of("Status")
        if s.msg_ref == "1" and s.cmd != "SyncHdr"
    ]
    assert acknowledged == [str(i) for i in range(1, 202)]
    # Each continuation request is itself acknowledged, in the reply to it.
    for msg_id, message in enumerate(messages[1:], start=2):
        refs = {(s.msg_ref, s.cmd) for s in message.of("Status")}
        assert {(str(msg_id), "SyncHdr"), (str(msg_id), "Alert")} <= refs
    # CmdID is unique per message, so every message numbers its commands afresh.
    for message in messages:
        assert [c.cmd_id for c in message.commands] == list(range(1, len(message.commands) + 1))


@pytest.mark.spec
@ENCODINGS
def test_a_message_exactly_at_max_msg_size_is_sent_whole_and_one_byte_less_is_split(
    settings: Settings, dm_store: MemoryStore, binary: bool
) -> None:
    def first_message(limit: int) -> DmResponse:
        return Client(fresh_service(settings, dm_store), limit, binary).send(INIT_BODY)

    # The header advertises the limit, so the whole message's size depends on how
    # many digits the limit has. Converge on the size that advertises itself.
    size = len(first_message(16384).body)
    for _ in range(3):
        size = len(first_message(size).body)
    whole = first_message(size)
    assert len(whole.body) == size
    assert read(whole, binary, size).final

    split = first_message(size - 1)
    assert not read(split, binary, size - 1).final
    assert len(split.body) < size


@pytest.mark.spec
@ENCODINGS
def test_a_continuation_does_not_advance_the_session_phase(
    dm: DmService, dm_store: MemoryStore, binary: bool
) -> None:
    client = Client(dm, SMALL[binary], binary)
    first = client.parse(client.send(INIT_BODY))
    assert not first.final
    session = dm_store.get_dm_session(SESSION_KEY)
    assert session is not None and session.phase == "devinfo" and session.pending

    while True:
        message = client.parse(client.send(next_message_body(client.msg_id)))
        assert not message.of("Add", "Replace"), "configuration must wait for the inventory"
        session = dm_store.get_dm_session(SESSION_KEY)
        assert session is not None and session.phase == "devinfo"
        if message.final:
            break
    assert not session.pending
    pushed = client.exchange(next_message_body(client.msg_id, results_for(["./DevDetail/SwV"])))
    assert items_of(pushed, "Replace")


# --------------------------------------------------------- OMADM-ALERT-1222
@pytest.mark.spec
@ENCODINGS
def test_statuses_and_results_sent_with_alert_1222_are_not_lost(
    dm: DmService, dm_store: MemoryStore, binary: bool
) -> None:
    client = Client(dm, SMALL[binary], binary)
    inventory = client.exchange(INIT_BODY)
    uris = [uri for uri, _ in items_of(inventory, "Get")]
    first = client.parse(client.send(next_message_body(client.msg_id, results_for(uris))))
    assert not first.final
    failed = (
        f"    <Status><CmdID>2</CmdID><MsgRef>{client.msg_id}</MsgRef><CmdRef>2</CmdRef>"
        "<Cmd>Replace</Cmd><Data>500</Data></Status>\n"
        "    <Results><CmdID>3</CmdID><MsgRef>1</MsgRef><CmdRef>1</CmdRef><Item><Source>"
        "<LocURI>./DevDetail/FwV</LocURI></Source><Data>FW-2</Data></Item></Results>\n"
    )
    message = client.parse(client.send(next_message_body(client.msg_id, failed)))
    while not message.final:
        message = client.parse(client.send(next_message_body(client.msg_id)))
    closing = client.send(closing_body(client.msg_id))

    assert closing.metric == "DmSessionCompleteWithErrors"
    assert closing.detail == "failures:1"
    device = dm_store.get_device(TEST_IMEI)
    assert device is not None and device.mo_values["./DevDetail/FwV"] == "FW-2"


@pytest.mark.spec
@pytest.mark.parametrize("code", ["1223", "1226"])
def test_ending_a_session_mid_package_discards_the_remainder(
    dm: DmService, dm_store: MemoryStore, code: str
) -> None:
    client = Client(dm, 1500, False)
    assert not client.parse(client.send(INIT_BODY)).final
    outcome = client.send(f"    <Alert><CmdID>1</CmdID><Data>{code}</Data></Alert>")
    message = client.parse(outcome)
    assert message.final and outcome.session_finished
    assert not message.of("Get")
    assert dm_store.get_dm_session(SESSION_KEY) is None


@pytest.mark.spec
@ENCODINGS
def test_a_closing_package_that_does_not_fit_ends_the_session_with_its_last_message(
    dm: DmService, dm_store: MemoryStore, binary: bool
) -> None:
    client = Client(dm, SMALL[binary], binary)
    inventory = client.exchange(INIT_BODY)
    uris = [uri for uri, _ in items_of(inventory, "Get")]
    client.exchange(next_message_body(client.msg_id, results_for(uris)))
    # Sixty commands in the last client package are owed sixty statuses.
    extra = "".join(f"    <Replace><CmdID>{i}</CmdID></Replace>\n" for i in range(3, 63))

    first = client.send(closing_body(client.msg_id) + "\n" + extra)
    assert first.metric == "DmPackageContinued" and not first.session_finished
    messages = [client.parse(first)]
    session = dm_store.get_dm_session(SESSION_KEY)
    assert session is not None and session.phase == "done" and session.pending
    while not messages[-1].final:
        outcome = client.send(next_message_body(client.msg_id))
        messages.append(client.parse(outcome))

    assert outcome.metric == "DmSessionComplete" and outcome.session_finished
    assert dm_store.get_dm_session(SESSION_KEY) is None
    acknowledged = [s.cmd_ref for m in messages for s in m.of("Status") if s.cmd == "Replace"]
    assert acknowledged == [str(i) for i in range(3, 63)]
    assert not any(m.of("Get", "Add", "Replace") for m in messages)


# -------------------------------------------- a message that can never fit
@pytest.mark.spec
@ENCODINGS
@pytest.mark.parametrize("stage", ["challenge", "init", "continuation"])
def test_a_message_that_cannot_fit_fails_cleanly_and_changes_nothing(
    dm: DmService,
    dm_store: MemoryStore,
    caplog: pytest.LogCaptureFixture,
    binary: bool,
    stage: str,
) -> None:
    client = Client(dm, SMALL[binary], binary)
    body = INIT_BODY
    if stage == "continuation":
        assert not client.parse(client.send(INIT_BODY)).final
        body = next_message_body(client.msg_id)
    before_session = deepcopy(dm_store.get_dm_session(SESSION_KEY))
    before_device = deepcopy(dm_store.get_device(TEST_IMEI))

    client.limit = TOO_SMALL[binary]
    outcome = client.send(body, authenticated=stage != "challenge")

    assert outcome.status_code == 400
    assert outcome.metric == "DmMessageTooLarge"
    assert outcome.detail == "message_exceeds_max_msg_size"
    assert not outcome.body and not outcome.session_finished
    assert dm_store.get_dm_session(SESSION_KEY) == before_session
    assert dm_store.get_device(TEST_IMEI) == before_device
    assert "dm message does not fit the client MaxMsgSize" in caplog.messages

    client.limit = SMALL[binary]
    client.msg_id -= 1
    assert client.send(body, authenticated=stage != "challenge").status_code == 200


# ------------------------------------------------------ OMADM-SEC-BODY-LIMIT
def test_a_remainder_too_large_to_store_is_refused_and_changes_nothing(
    dm: DmService, dm_store: MemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    # 8 KB of WBXML asks for 8,000 acknowledgements. What the first message cannot
    # carry would have to be stored, and a DynamoDB item cannot hold that much.
    body = "<Alert><CmdID>1</CmdID><Data>1201</Data></Alert>" + "<Replace/>" * 8000
    wire = wbxml.encode(package(1, body, credential=cred()))
    assert len(wire) < 16384

    outcome = dm.handle(wire, syncml.CONTENT_TYPE_WBXML)

    assert outcome.status_code == 413
    assert outcome.metric == "DmTooLarge"
    assert outcome.detail == "package_too_large"
    assert not outcome.body
    assert dm_store.get_dm_session(SESSION_KEY) is None
    assert dm_store.get_device(TEST_IMEI) is None
    assert "dm package remainder exceeds the session budget" in caplog.messages


def test_an_unknown_queued_command_is_a_programming_error() -> None:
    builder = syncml.SyncMlBuilder(session_id="1", msg_id=1, target="IMEI:1", source="acs")
    with pytest.raises(ValueError, match="Exec"):
        omadm_package.apply(builder, [omadm_package.Item("Exec", "./DevInfo")])
