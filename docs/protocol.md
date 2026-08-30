# RCC.14 protocol behaviour

Wire-level reference for the RCS configuration plane. The authority for the code
is `src/acs/protocol/`; this document explains the decisions.

## Endpoint

Registered on every path in `ACS_CONFIG_PATHS`, default `/`, `/config`,
`/rcs/config`, because deployed clients disagree about which one to call. `GET`
carries the flow; `POST` on the same paths is accepted for the OTP step, since a
query-string OTP is written into every proxy and load balancer log on the way.

## Request parameters

Casing is *not* normalised: `IMSI`, `IMEI`, `SMS_port` and `OTP` are upper case in
the specification, while `msisdn`, `token` and the `terminal_*`/`client_*` family
are lower case. Lower-case aliases are additionally accepted because real clients
send them. The full alias table is `PARAMETER_ALIASES` in
`src/acs/protocol/request.py`.

| Parameter | Type | Notes |
| --- | --- | --- |
| `vers` | int | Version the client currently holds. `0` = unprovisioned. Negative values accepted: clients echo back the disable value they were previously given |
| `IMSI` | 5–15 digits | Kept as a **string**. Parsing as an integer would drop leading zeros and change the MCC |
| `IMEI` / `IMEISV` | 14–16 digits | Shape only. Field-test devices carry non-Luhn IMEIs, and rejecting them would lock real handsets out |
| `msisdn` | E.164 | Normalised. A badly encoded `+` arrives as a space, which is recovered |
| `token` | opaque | Previously issued provisioning token |
| `OTP` | alphanumeric | Second request of the challenge flow |
| `SMS_port` | 0–65535 | Non-zero requests port-addressed delivery — see [limitations.md](limitations.md) |
| `SMS_format` | string | Recorded |
| `default_sms_app` | int | `0` suppresses the messaging authorisations |
| `terminal_vendor`, `terminal_model`, `terminal_sw_version` | string | Length-capped, fed into the device inventory |
| `client_vendor`, `client_version`, `rcs_version` | string | Recorded |
| `rcs_profile` | string | Selects the catalogue overlay |
| `rcs_state` | int | Client-reported state |
| `provisioning_version` | string | Document/protocol capability. Distinct from `vers` |
| `device_type`, `device_id`, `friendly_device_name` | string | Secondary (non-SIM) device provisioning |
| `app` | repeatable | AppIDs the client wants; other APPLICATION blocks are omitted |

Rejections, all producing `ACS_MALFORMED_REQUEST_STATUS` (default `400`):

- non-numeric or out-of-range `IMSI`, `IMEI`, `SMS_port`;
- a value longer than the per-field cap;
- a repeated identity parameter (`IMSI`, `IMEI`, `msisdn`, `token`, `OTP`, `vers`)
  — a duplicate is how an attacker smuggles a second identity past a proxy that
  only inspects the first;
- a non-alphanumeric `OTP`.

Unknown parameters are recorded and logged by name, never echoed back.

## Identity resolution

Ordered strongest-evidence-first, in `ProvisioningService.resolve_identity`:

1. **Token** — verified, IMSI/IMEI bound, unexpired, unrevoked. An *invalid* token
   yields `511` rather than being ignored, so the client rebootstraps.
2. **Header enrichment** — only from a peer inside `ACS_TRUSTED_PROXY_CIDRS`.
3. **GBA** — if enabled and an `Authorization` header is present.
4. **Claimed identity** — `IMSI` or `msisdn` matching a subscriber. This is a
   claim, not a credential, so it never authenticates on its own.
5. **OTP** — with a claimed identity plus a valid `OTP=` for the pending
   challenge, the subscriber is authenticated.
6. **Nothing usable** — `511`, or a GBA `401` when GBA is enabled.

A bare `msisdn=` parameter authenticating the request would let anyone pull another
subscriber's IMS credentials. It does not.

## Responses

| Situation | Response |
| --- | --- |
| Authenticated, client's version stale | `200`, full `wap-provisioningdoc` |
| Authenticated, client already current | `200`, document with `VERS` only |
| Operator forced a disable value | `200`, `VERS` only with the negative version |
| Candidate known, no proof | OTP sent, `200` with `Content-Length: 0` |
| Challenge already outstanding within the cooldown | `200` with `Content-Length: 0`, no second SMS |
| OTP rate limit reached | `429` with `Retry-After` |
| Known subscriber, not entitled, or IMEI not allowlisted | `403`, empty body |
| Identity unresolved | `511`, empty body |
| GBA enabled and identity unresolved | `401` with `WWW-Authenticate: Digest … algorithm=AKAv1-MD5` |
| Port-addressed OTP requested but unsupported | `503` with `Retry-After`, challenge deleted |
| Malformed request | `400` (configurable to `403`) |
| Unhandled error | `500`, `{"error":"internal_error"}`, no stack trace |

Every response carries `Cache-Control: no-store`, `Pragma: no-cache` and
`X-Content-Type-Options: nosniff`, because the body contains IMS credentials and a
bearer token.

## Versions and validity

Request `vers` and response `VERS/version` are different things and conflating
them is a classic bug. The response version is an instruction:

| Version | Action | Deletes config | May re-query |
| --- | --- | --- | --- |
| `> 0` | apply | no | yes |
| `0` | disable, keep nothing valid | no | on a trigger only |
| `-1` | disable and delete | yes | at the next trigger |
| `-2` | disable and delete | yes | no, until factory reset or SIM swap |
| `-3` | dormant, keep configuration | no | after `validity` |
| `-4` | permanently barred | no | no |

`validity` is a lifetime in seconds (`0` = no expiry), not a version. Revisions
increase monotonically; `next_version()` restarts a non-positive stored value at
`1` because a decreasing version can wedge clients.

When `client_holds_current(request_vers, server_version)` is true the ACS sends a
`VERS`-only document. Skipping that optimisation means re-sending ~130 parameters
on every validity refresh across the whole fleet.

## Document shape

```xml
<?xml version='1.0' encoding='UTF-8'?>
<wap-provisioningdoc version="1.1">
  <characteristic type="VERS">
    <parm name="version" value="1"/>
    <parm name="validity" value="86400"/>
  </characteristic>
  <characteristic type="TOKEN">
    <parm name="token" value="…"/>
  </characteristic>
  <characteristic type="APPLICATION">
    <parm name="AppID" value="ap2001"/>
    <parm name="Private_User_Identity" value="001010000000001@ims.mnc001.mcc001.3gppnetwork.org"/>
    <parm name="Voice_Domain_Preference_E_UTRAN" value="3"/>
    <characteristic type="Public_User_Identity_List">…</characteristic>
    <characteristic type="LBO_P-CSCF_Address">…</characteristic>
  </characteristic>
  <characteristic type="APPLICATION">
    <parm name="AppID" value="ap2002"/>
    <parm name="AppRef" value="ap2001"/>
    <characteristic type="SERVICES">…</characteristic>
    <characteristic type="MESSAGING">
      <characteristic type="CHAT">…</characteristic>
      <characteristic type="FT">…</characteristic>
    </characteristic>
    <characteristic type="OTHER">
      <characteristic type="TRANSPORTPROTO">…</characteristic>
    </characteristic>
  </characteristic>
  <characteristic type="APPLICATION">
    <parm name="AppID" value="w7"/>          <!-- OMA-DM account bootstrap -->
    <parm name="ADDR" value="https://acs.example.com/dm"/>
    <parm name="AAUTHNAME" value="001010000000001"/>
    <parm name="AAUTHSECRET" value="…"/>
  </characteristic>
</wap-provisioningdoc>
```

Properties the implementation guarantees:

- built with `lxml`, never string templates, so attribute escaping is always
  correct — a `friendly_device_name` may legitimately contain `&` or `<`;
- deterministic element order, following catalogue order, because some clients are
  order sensitive;
- empty non-mandatory values are omitted rather than emitted as `value=""`, which
  some clients treat as a real setting;
- `default_sms_app=0` forces `standaloneMsgAuth`, `ChatAuth` and `GroupChatAuth`
  to `0`, avoiding duplicate message delivery on the handset;
- parsing (in tests and the simulators) runs with entity resolution and network
  access disabled.

## The MSISDN entry flow

After a `511` a client may open `/msisdn`. Server-rendered HTML, no inline
script, strict CSP, `<label>` elements, `inputmode` hints, `aria-describedby`
error text, and a CSRF token bound to an `HttpOnly`, `SameSite=Strict` cookie.

The response is identical whether or not the number is known, so the page cannot
be used to test which numbers exist on the network.
