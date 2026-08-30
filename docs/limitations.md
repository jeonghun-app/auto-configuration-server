# Limitations

An ACS that pretends to do something it cannot is worse than one that says so: a
silent failure surfaces as handsets that never provision, and nobody knows why.

## 1. Port-addressed (silent) OTP SMS cannot be sent from AWS

RCC.14 lets a client supply `SMS_port`. When it does, the OTP must arrive as a
binary SMS carrying a User Data Header with that destination port, so the client
reads it without the user seeing anything.

Neither Amazon SNS nor AWS End User Messaging SMS can send a UDH. Only an
operator SMSC over SMPP can.

What this repository does instead:

- `SMS_port` is parsed, validated, stored on the challenge and passed through the
  `SmsSender` interface end to end.
- `SnsSmsSender` and `EndUserMessagingSender` raise `UnsupportedDelivery`. The
  service answers `503` with `Retry-After` and deletes the pending challenge,
  rather than sending a text message the client will never read and leaving it
  waiting forever.
- `SmppSmsSender.build_udh()` implements the 16-bit application port addressing
  header (`06 05 04 <dest> <src>`) and is unit tested, because that is the part
  implementers most often get wrong. `send()` raises `NotImplementedError`.

To make it work you need an SMSC account and an SMPP client in
`src/acs/sms/smpp.py`. Text OTP works today.

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

## 7. WBXML is not supported

SyncML DM can be encoded as WBXML (`application/vnd.syncml.dm+wbxml`). Only the
XML encoding is implemented. A WBXML request is refused with `415` rather than
answered with XML the client cannot decode. Some production DM clients use WBXML
exclusively, so this would need adding for those.

## 8. No server-initiated DM session

Waking a device for a management session needs a WAP Push or a trigger SMS
through an operator SMSC — the same dependency as item 1. The server accepts an
`Alert` 1200 (server-initiated) if something else started the session, but cannot
originate the trigger.

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
