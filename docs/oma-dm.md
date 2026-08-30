# OMA-DM device management plane

The second plane of the service. Once a device has been provisioned for RCS it can
be managed over OMA-DM: VoLTE settings pushed, device inventory collected, and any
future management object added as YAML.

## Why it is in the same service

The OMA-CP configuration document contains the `w7` APPLICATION characteristic,
which is the standard OMA bootstrap for a DM account:

```xml
<characteristic type="APPLICATION">
  <parm name="AppID" value="w7"/>
  <parm name="PROVIDER-ID" value="ACS-DM"/>
  <parm name="ADDR" value="https://acs.example.com/dm"/>
  <parm name="AAUTHTYPE" value="BASIC"/>
  <parm name="AAUTHNAME" value="001010000000001"/>
  <parm name="AAUTHSECRET" value="…"/>
  <parm name="INIT" value="1"/>
</characteristic>
```

The ACS generates a per-subscriber DM password at RCS provisioning time, stores it
on the subscriber record and emits it here. The device then has everything it
needs to open a DM session. `tests/test_e2e_simulators.py` walks exactly that
chain: provision over RCC.14, read `AAUTHSECRET` from the document, and use it to
authenticate a real DM session.

Set `ACS_DM_BOOTSTRAP_IN_CP=false` to stop emitting it, or `ACS_DM_ENABLED=false`
to remove the DM plane entirely.

## Session flow

```
device                                            ACS
  │  POST /dm  package 1                            │
  │  SyncHdr(SessionID, MsgID=1, Cred)              │
  │  Alert 1201 + Replace ./DevInfo/*               │
  ├────────────────────────────────────────────────►│  authenticate
  │                                                 │  record DevInfo
  │◄────────────────────────────────────────────────┤  Status 212 (SyncHdr)
  │  package 2: Status … + Get ./DevInfo, ./DevDetail│  Status 200 per command
  │                                          + Final│  Get device-owned nodes
  │                                                 │
  │  POST /dm  package 3                            │
  │  Status + Results for every Get                 │
  ├────────────────────────────────────────────────►│  update inventory
  │◄────────────────────────────────────────────────┤  Replace server-owned nodes
  │  package 4: Status … + Replace ./3GPP_IMS/…      │  (IMS, VoLTE, RCS)
  │                                          + Final│
  │                                                 │
  │  POST /dm  package 5                            │
  │  Status for each Replace                        │
  ├────────────────────────────────────────────────►│  session complete
  │◄────────────────────────────────────────────────┤  Status only + Final
```

Server phases are `init` → `devinfo` → `configure`, held in `DmSession` in the
shared store — not in process memory, so a session survives being load balanced
across ECS tasks mid-flow. Sessions carry a TTL and DynamoDB expires them.

`Alert 1226` from the client ends the session at any point. A first package
without `Alert` 1200 or 1201 is a protocol error (`400`).

## Authentication

`ACS_DM_AUTH_SCHEME` selects `basic`, `md5` or `none` (local testing only).

- `syncml:auth-basic` — `Data` is `base64(username:password)`. The username is the
  IMSI, matching `AAUTHNAME`.
- `syncml:auth-md5` — `Data` is
  `base64(MD5(base64(MD5(user:password)) + ":" + nonce))`. The server issues the
  nonce in a `Chal` on the rejected first message and stores it on the session.
  MD5 is what the OMA-DM specification mandates; it is used for protocol
  compatibility only, the password is never stored in the clear, and the transport
  is expected to be TLS.

An authentication failure returns SyncML `Status` `401` with a `Chal` and
`NextNonce`, inside **HTTP 200**. Returning HTTP 401 makes many DM clients abort
the session instead of retrying with credentials.

An unknown username and a wrong password are answered identically, so the DM
endpoint cannot be used to enumerate subscribers.

## The management object tree

Definitions live in `src/acs/catalog/omadm/`, loaded and validated at startup. A
malformed definition fails the container rather than degrading to an empty tree.

| File | URN | Root | Nodes |
| --- | --- | --- | --- |
| `01-devinfo.yaml` | `urn:oma:mo:oma-dm-devinfo:1.0` | `./DevInfo` | `DevId`, `Man`, `Mod`, `DmV`, `Lang` |
| `02-devdetail.yaml` | `urn:oma:mo:oma-dm-devdetail:1.0` | `./DevDetail` | `DevTyp`, `OEM`, `FwV`, `SwV`, `HwV`, `LrgObj`, `URI/*` |
| `03-3gpp-ims.yaml` | `urn:oma:mo:ext-3gpp-ims:1.0` | `./3GPP_IMS` | IMS identity, P-CSCF, timers, and the VoLTE set |
| `04-rcs-ext.yaml` | `urn:acs:mo:rcs-ext:1.0` | `./RCS` | RCS service switches |

`GET /dm/mo` lists what is loaded, with node counts and verification status.

### Node ownership

```yaml
- uri: ./DevInfo/Man
  format: chr
  source: device        # server issues Get, records the answer
  access: [Get]
  spec: OMA-DM DevInfo Man
  verified: true

- uri: ./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN
  format: int
  source: server        # server issues Replace
  default: "3"
  access: [Get, Replace]
  feature: volte        # only pushed when the subscriber has VoLTE enabled
  spec: 3GPP TS 24.167
  verified: false
```

`source: device` builds the inventory. `source: server` is configuration the ACS
owns. `feature` gates a node on a subscriber capability: `volte` nodes are skipped
when `volte_enabled` is false, `rcs` nodes when RCS is not in play.

Placeholders (`{impi}`, `{impu}`, `{ims_domain}`, `{imsi}`, `{msisdn}`,
`{device_id}`, `{provisioning_version}`) are resolved per subscriber, and
`Subscriber.overrides` keyed by node URI beats the default.

### VoLTE parameters

In `03-3gpp-ims.yaml`:

| Node | Meaning |
| --- | --- |
| `Voice_Domain_Preference_E_UTRAN` | 1 CS only, 2 IMS PS preferred, 3 CS preferred, 4 IMS PS only |
| `SMS_Over_IP_Networks_Indication` | SMSoIP; required for a complete VoLTE profile |
| `ICSI_List/1/ICSI` | MMTEL communication service identifier |
| `Keep_Alive_Enabled` | SIP keep-alive |
| `Ext/RCS/rcsVolteSingleRegistration` | RCS shares the VoLTE IMS registration instead of registering twice |
| `Ext/VoLTE/AMRWB_Enabled` | Wideband codec |
| `Ext/VoLTE/VideoCallEnabled` | ViLTE |
| `Ext/VoLTE/EmergencyRegistration` | Emergency IMS registration |

The same VoLTE settings are also present in the OMA-CP `ap2001` application, so a
device can be configured either way: OMA-CP at RCS bootstrap, OMA-DM for ongoing
management.

## Adding a management object

No code changes. Drop a file into `src/acs/catalog/omadm/` — the numeric prefix
controls load order:

```yaml
# src/acs/catalog/omadm/05-fumo.yaml
meta:
  id: fumo
  urn: urn:oma:mo:oma-fumo:1.0
  root: ./FUMO
  title: Firmware update
  spec: OMA Firmware Update Management Object

nodes:
  - uri: ./FUMO
    format: node
    source: server
    access: [Get, Add]
    spec: OMA FUMO
  - uri: ./FUMO/PkgURL
    format: chr
    source: server
    default: "https://fw.{ims_domain}/latest"
    access: [Get, Replace]
    spec: OMA FUMO PkgURL
```

Restart and it appears in `GET /dm/mo`, is pushed to devices, and shows up in
`docs/spec-coverage.md` after `make coverage-doc`. Validation is strict: a node
outside the declared root, an unknown format or source, a duplicate URI, or a
`bool` default that is not `true`/`false` all fail startup.

`tests/test_dm_motree.py::test_a_new_management_object_needs_no_code` asserts this
contract, so it cannot silently regress.

## Verifying the DM plane

```bash
python tools/dm_client_sim.py \
  --base-url http://127.0.0.1:8080 \
  --imsi 001010000000001 \
  --imei 356938035643809 \
  --password "<AAUTHSECRET from the w7 characteristic>"
```

14 checks, covering authentication, `Status` codes, the inventory `Get`, the
configuration `Replace`, the VoLTE nodes, and clean session termination.
`scripts/verify_stack.py` does the whole chain including harvesting the password.

## What is missing

- **WBXML** (`application/vnd.syncml.dm+wbxml`) — refused with `415` rather than
  answered with XML the client cannot decode.
- **Server-initiated sessions** — needs a WAP Push or trigger SMS through an
  operator SMSC.
- **Large object handling** — `./DevDetail/LrgObj` is read but chunked transfer is
  not implemented.
