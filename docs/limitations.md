# Limitations

An ACS that pretends to do something it cannot is worse than one that says so: a
silent failure surfaces as handsets that never provision, and nobody knows why.

## 1. Port-addressed (silent) OTP SMS needs an operator SMSC, and is unproven

RCC.14 lets a client supply `SMS_port`. When it does, the OTP must arrive as a
binary SMS carrying a User Data Header with that destination port, so the client
reads it without the user seeing anything.

Neither Amazon SNS nor AWS End User Messaging SMS can send a UDH. Only an
operator SMSC can, and since 1.4.0 this repository can talk to one over SMPP 3.4
(`ACS_SMS_PROVIDER=smpp`, `src/acs/sms/smpp.py`):

- One `bind_transceiver` / `submit_sm` / `unbind` session per message. With
  `SMS_port` the message is 8-bit data (`data_coding` `0x04`) with UDHI set
  (`esm_class` `0x40`) and the 16-bit application port header
  (`06 05 04 <dest> <src>`) ahead of the OTP template.
- Every failure — bind refused, an error status, `generic_nack`, a timeout, a
  connection or TLS failure — answers `503` with `Retry-After: 60`, the metric
  `OtpDeliveryFailed`, and the pending challenge deleted.
- `SnsSmsSender` and `EndUserMessagingSender` still raise `UnsupportedDelivery`
  for `SMS_port`: `503` with `Retry-After`, challenge deleted, rather than a text
  message the client will never read.

What is still not known or not done:

- **It has been exercised only against the in-process fake SMSC** in
  `tests/test_smpp.py`, which asserts the PDUs octet for octet. It has never
  bound to an operator SMSC or delivered to a handset. SMSCs differ in TLS
  support, `system_type`, TON/NPI expectations and how they treat 8-bit data, so
  expect configuration work with the operator.
- **The user data after the UDH is the configured OTP template**
  (`ACS_SMS_OTP_TEMPLATE`). The format an RCS client expects in a port-addressed
  OTP message is not taken from a pinned RCC.14 edition, so a client may receive
  the message and fail to recognise the code.
- **No concatenation.** A message longer than one SMS (133 octets of user data
  with the port header, 160 GSM or 70 UCS-2 characters without) is refused rather
  than split.
- `RCC14-AUTH-OTP-PORT` is therefore `partial` in
  [conformance.md](conformance.md), not implemented.

Text OTP works with every provider.

## 2. GBA / AKA is an interface, not an implementation

3GPP TS 33.220 GBA needs a USIM performing AKA, a Bootstrapping Server Function
reachable over Ub, an HSS holding the subscriber key, and a Zn interface from this
server to the BSF.

Implemented: the `401` challenge with `WWW-Authenticate: Digest …
algorithm=AKAv1-MD5`, `Authorization` parsing, RFC 2617 digest computation with
`Ks_NAF` as the password, a `BsfClient` protocol, and a deterministic
`MockBsfClient` with a fixed test vector.

Not implemented: anything that actually talks to a BSF. `ACS_GBA_ENABLED` is
`false` by default, and when it is enabled in staging or production the service
installs `UnconfiguredBsfClient`, which raises rather than fake a successful
bootstrap.

## 3. Header enrichment is simulated

Real enrichment means an operator packet gateway inserting a subscriber identity
header on the cellular data path. Here it is a header the ACS will read **only**
when `ACS_TRUSTED_PROXY_CIDRS` is set and the peer address falls inside it.

Empty (the default) disables the mechanism entirely. Without the IP gate an
identity header is a free authentication bypass: anyone can send one.

Behind an ALB the peer is the load balancer, so the evaluated address comes from
the right-most `X-Forwarded-For` entry — the ALB appends, so earlier entries are
caller-controlled and forgeable.

## 4. The ACS is not reachable at its real name

An RCS client derives its ACS from the SIM:
`config.rcs.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org`. That zone is controlled by
the operator and the GSMA. Nobody outside can publish a record in it.

The deploy output prints the CNAME to create. For testing, override DNS or send a
`Host` header. `--config-path` on the simulator covers clients that use `/` or
`/rcs/config` instead of `/config`.

## 5. No real handset has ever talked to this

CI has no device. Verification is two protocol-correct simulators
(`tools/rcs_client_sim.py`, `tools/dm_client_sim.py`) that assert the server's
behaviour and exit non-zero on a violation. They prove the server is
self-consistent and specification-shaped. They do not prove that a particular
vendor's RCS client parses the document the way its documentation implies.

Known places where real clients diverge from any specification: exact `parm`
casing, sensitivity to element order, whether an empty `value=""` is treated as a
setting, and the content type they will accept. The generated document is
deterministic and ordered by the catalogue for exactly this reason.

## 6. Specification coverage is partial and says so

25 of 116 OMA-CP parameters and 23 of 47 OMA-DM nodes are marked `verified`. The
rest are implemented from public descriptions of RCC.07/RCC.14 and from operator
configurations widely deployed in the field.

Consequences to be honest about:

- A parameter name could be misspelled relative to the licensed text. Clients
  match exactly, so a wrong name is silently ignored by the handset.
- Default values are reasonable, not authoritative. Operators must set their own.
- The `-1` to `-4` version semantics are the documented baseline
  (`src/acs/protocol/vers.py`), and differ between RCC.14 releases. Getting these
  wrong can disable RCS on a fleet, which is why they are one table with a
  citation per row rather than scattered conditionals.

This repository makes **no claim of GSMA certification**.

## 7. WBXML is supported for text-valued SyncML only

SyncML DM can be encoded as WBXML (`application/vnd.syncml.dm+wbxml`). Since
1.4.0 `POST /dm` accepts it and answers in the same encoding: WBXML 1.2 and 1.3,
UTF-8, the SyncML and MetInf code pages, inline and string-table strings. See
[oma-dm.md](oma-dm.md#wbxml-encoding).

What it does not do:

- **`OPAQUE` must hold UTF-8 text.** Binary opaque data, which a management object
  with binary leaf values would need, is refused with `400`.
- Attributes, literal tags, extension tokens, `ENTITY`, processing instructions
  and other code pages are refused with `400` rather than guessed at.
- It has been tested against fixed byte vectors, the repository's DM simulator
  (`--wbxml`) and generated and mutated input, not against a real handset's DM
  client.

## 8. No server-initiated DM session

Waking a device for a management session needs an OMA-DM notification — a WAP
Push or trigger SMS carrying the notification message — delivered to the handset.
The SMPP client added in 1.4.0 can reach an operator SMSC, but it sends only the
OTP: nothing builds the notification message, nothing addresses it to the WAP Push
port, and there is no API or console action to start a session. The server accepts
an `Alert` 1200 (server-initiated) if something else started the session, but
cannot originate the trigger.

## 9. Operational limits

- **In-memory store is single-task only.** It is refused in staging and
  production at startup, because an OTP issued by one task would be invisible to
  the next.
- **Tasks run in public subnets with public IPs** in the default CloudFormation
  stack, so images can be pulled from ECR without a NAT gateway. No inbound rule
  admits anything but the load balancer. The private-subnet variant is described
  in [aws-deployment.md](aws-deployment.md) and costs more.
- **No WAF by default.** A rate-based rule is recommended before opening the
  service to the internet; the OTP endpoint costs money to abuse.
- **HTTP without a certificate.** `scripts/deploy.sh` warns loudly. Serving RCC.14
  over cleartext exposes IMSI, IMEI, MSISDN, OTP and tokens on the wire.

## 10. DM responses are not split to fit `MaxMsgSize`

The client's `MaxMsgSize` is read, and the server never advertises more than the
client accepts, but a response is not split across messages to stay under it: a
package with many commands is sent whole, in XML and in WBXML. A device that
negotiates a `MaxMsgSize` of 16384 has been seen to receive a 231,588-byte XML
response, which a client enforcing its limit may reject. The WBXML encoder's output limits are resource bounds,
not message splitting. Tracked in #20.
