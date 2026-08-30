<!-- GENERATED FILE — edit src/acs/catalog/conformance/*.yaml, then run
     scripts/gen_conformance.py -->
# Conformance registry

Answers one question, requirement by requirement: **which parts of the OMA-DM and
RCC.14 specifications does this server accommodate, and which does it not?**

## Read this before reading the tables

Four things are kept deliberately separate, because collapsing them is how a
document like this becomes misleading:

| Column | What it means |
| --- | --- |
| **Level** | Whether the requirement is mandatory, optional or conditional *for a
  server*. **This is this project's engineering judgement, not a citation.** |
| **Status** | What the code does: implemented, partial, not implemented. |
| **Evidence** | *How we know*: a passing test that asserts the wire behaviour, or
  only a code review. |
| **Gap / impact** | For anything less than implemented, what is missing and what breaks. |

No specification edition is pinned. Nobody working on this repository holds
RCC.14, RCC.07 or the OMA DM conformance requirement tables, so the Level column
could be wrong. `level_verified` is `false` on every row and a test enforces that.

**Nothing here is certified.** "Implemented" means the message exchange is
implemented and a test asserts it. It does not mean GSMA or OMA has confirmed
anything, and it does not mean a real handset has been tried — no real device has
ever talked to this server. See [limitations.md](limitations.md).

Parameter *spelling* coverage is counted separately in
[spec-coverage.md](spec-coverage.md).

## How this document can fail

It is generated and gated, not written:

* every requirement claiming a status must name a test, and
  `tests/test_conformance_registry.py` checks those names against the tests pytest
  actually collected — a renamed test breaks the build;
* every named source symbol is resolved by parsing the file;
* the set of mandatory gaps is frozen in a constant, so neither a new gap nor a
  silent upgrade to "implemented" can pass unnoticed;
* compliance and certification wording is rejected by the loader;
* `--check` fails if this file is stale.

## Summary

| Specification | Requirements | Implemented | Partial | Not implemented | Mandatory gaps |
| --- | --- | --- | --- | --- | --- |
| OMA Device Management 1.2 (server role) | 58 | 37 | 5 | 16 | 10 |
| GSMA RCC.14 Service Provider Device Configuration (ACS role) | 55 | 43 | 9 | 3 | 7 |
| **Total** | **113** | **80** | **14** | **19** | **17** |

**Overall: not fully conformant.** 17 requirements this project classifies as mandatory are not fully implemented, and 3 further specification families could not be assessed at all because the documents are not held. Both are listed below, and `scripts/gen_conformance.py --strict` exits non-zero for each reason separately.

## Specification families not assessed

These were asked for and **cannot be assessed here, because the document is not held**. They deliberately carry no requirement rows: a row would imply a requirement had been read. Counting them separately keeps the numbers above meaningful.

### `kr-tta-omadm` — TTA standard for OMA-DM based terminal management

* Jurisdiction: Republic of Korea
* State: **not assessed**, document not held
* Why in scope: Requested directly: the server must match the TTA OMA-DM specification. The implementation here targets OMA DM 1.2 as published by the Open Mobile Alliance. A national standards body typically profiles such a specification — narrowing options, fixing values, and making optional features mandatory — so conformance to OMA DM 1.2 does not imply conformance to a national profile of it.
* How to obtain: The TTA standards portal at tta.or.kr. Many Korean standards are downloadable without charge, and are frequently published in Korean only. Search for the terminal management standard and record its exact identifier and publication date in docs/scope.md before any row is written.
* Knowable without the document:
  * This server implements the client-initiated, XML-encoded subset of OMA DM 1.2, and its own gaps are already enumerated in the conformance registry.
  * MCC 450 is the Republic of Korea and its operators use two-digit mobile network codes, which this server already resolves correctly.
* Only the document can answer:
  * Which TTA standard is meant, by exact identifier and publication date.
  * Whether it profiles OMA DM 1.2 or a different version.
  * Whether it makes the binary WBXML encoding mandatory.
  * Whether it requires server-initiated sessions, and over which bearer.
  * Whether it requires access control lists on the management tree.
  * Whether it requires server-to-client authentication.
  * Whether it requires the standardised DM account management object.
  * Whether it defines Korean management objects beyond the standardised set.
  * Which conformance test suite, if any, a device or server must pass.
* Existing gaps an assessment would most likely touch: `OMADM-ENC-WBXML`, `OMADM-FLOW-NOTIFICATION`, `OMADM-TREE-ACL`, `OMADM-AUTH-SERVER-TO-CLIENT`, `OMADM-MO-DMACC`, `OMADM-SIZE-SPLITTING`
* Note: The related_gaps list is where an assessment would most likely land, based on what national operator profiles usually require. That is expectation, not knowledge, and none of those gaps is currently closed.

### `kr-mno-interworking` — Korean three-operator RCS interworking specification

* Jurisdiction: Republic of Korea
* State: **not assessed**, document not held
* Why in scope: Requested directly: the server must match the three-operator interworking specification. The three Korean mobile network operators ran a joint RCS service, so an inter-operator agreement governing identities, service parameters, capability discovery and interworking almost certainly exists.
* How to obtain: One of the three operators, or the joint body that maintains it. This is very likely a private inter-operator document available only under a non-disclosure agreement, and it is not obtainable by this project independently. The customer or partner operator has to supply it.
* Knowable without the document:
  * The three Korean operators offered a joint RCS messaging service, first under the joyn brand and later under a Korean brand name.
  * Public statements have described the later Korean joint service as being built on the GSMA Universal Profile, which is the family this server already implements. Which Universal Profile release each operator deployed is not public.
  * MCC 450 with two-digit network codes, and the +82 country code with a national trunk prefix that must be stripped for E.164. Both are handled.
* Only the document can answer:
  * Which document, maintained by whom, at which revision.
  * Which Universal Profile release each operator requires.
  * Which provisioning parameter values are fixed by agreement rather than left to each operator.
  * How subscriber identity and the ACS address are resolved between operators.
  * What capability discovery and interworking rules apply across the three networks.
  * Which interoperability test cases must pass before a server is accepted.
  * Whether messaging, file transfer or chatbot behaviour is constrained beyond the Universal Profile.
* Existing gaps an assessment would most likely touch: `RCC14-REQ-PARAMETERS`, `RCC14-VERS-NEGATIVE`, `OMACP-DOC-SEMANTIC-VALIDATION`
* Note: Until this document is available, the honest position is that this server implements the GSMA family the Korean service was reportedly built on, and that agreement-specific values remain unknown. Values must not be guessed: a wrong default served to a real handset disables a feature silently.

### `kr-domestic-other` — Other Korean domestic specifications, not yet identified

* Jurisdiction: Republic of Korea
* State: **not assessed**, document not held
* Why in scope: Requested as a category rather than a document: the server must match the specifications provided in Korea. That phrase can cover national standards, regulatory obligations on handling subscriber data, or an individual operator's own device and service specification. Which of those is meant has to be settled before anything can be assessed.
* How to obtain: The requester, who must name the documents. National standards come from the TTA portal; regulatory obligations come from the relevant ministry and are a legal question rather than an engineering one; operator specifications come from the operator.
* Knowable without the document:
  * Korean law imposes obligations on the handling of personal and location data, and subscriber identifiers such as IMSI, IMEI and MSISDN fall within that. This server already masks or pseudonymises them in logs, keeps them out of metric dimensions, and leaves load balancer access logging off by default. Whether that satisfies a specific legal obligation is a question for a lawyer, not for this registry.
  * AWS operates a region in Seoul, so data residency inside Korea is achievable by changing the deployment region. The default in the deployment script is already ap-northeast-2.
* Only the document can answer:
  * Which documents are meant, by name.
  * Whether any of them constrain the wire protocol, as opposed to operations or data handling.
  * Whether data residency inside Korea is required.
  * Whether an accredited third party must certify the result, and against what.
* Note: This entry exists so the category is not silently dropped. It will stay not-assessed until the documents are named.

## Mandatory gaps

### `OMADM-HDR-VERPROTO-VALIDATE` — Reject a message whose VerProto is not DM/1.2

* Status: **not-implemented** (code-review-only)
* Reference: OMA-TS-DM_Protocol, protocol version negotiation
* Gap: _parse_header defaults a missing or unexpected VerProto to DM/1.2 instead of refusing the message, and no SyncML 513 is produced.
* Impact: A message claiming a different DM protocol version enters the session state machine and is answered as if it were DM/1.2.

### `OMADM-HDR-MSGID` — Require the MsgID and reference it from every Status

* Status: **partial** (behaviour-tested)
* Reference: OMA-TS-DM_RepPro, MsgID and MsgRef
* Gap: The MsgID is required and echoed as MsgRef, but ordering, duplicates and replay are not checked, and the server mirrors the client MsgID rather than keeping its own outbound counter.
* Impact: A replayed or out-of-order client message is processed as if it were new. Harmless in the strict request and response flow used today; it becomes wrong as soon as the server sends two messages for one client message.

### `OMADM-SIZE-SPLITTING` — Split a package that exceeds the negotiated MaxMsgSize

* Status: **not-implemented** (code-review-only)
* Reference: OMA-TS-DM_Protocol, large message handling
* Gap: The negotiated size is advertised but the server always emits its commands in a single package. DmSession.pending exists for this and is unused.
* Impact: A client advertising a small MaxMsgSize may reject or truncate the configuration push. This is the most likely remaining real-device failure.

### `OMADM-FLOW-CORRELATION` — Match incoming Status and Results to the commands that were sent

* Status: **partial** (behaviour-tested)
* Reference: OMA-TS-DM_RepPro, CmdRef and MsgRef correlation
* Gap: Non-2xx statuses are detected and logged, but CmdRef and MsgRef are not matched against the commands the server issued, and a phase advances even if the expected Results are absent.
* Impact: The server cannot tell which command a failure belongs to, so it cannot retry or repair selectively.

### `OMADM-STATUS-5XX` — Interpret a client 5xx status for a command the server issued

* Status: **partial** (behaviour-tested)
* Reference: OMA-TS-DM_RepPro, Status 500 and 51x
* Gap: A failure is counted, logged and reflected in the metric, but there is no retry, repair or alarm path.
* Impact: A device that rejects part of its configuration stays misconfigured until the next session. Visible in the logs and the DmSessionCompleteWithErrors metric.

### `OMADM-AUTH-NONCE-ROTATION` — Send a fresh nonce on each authenticated message

* Status: **partial** (behaviour-tested)
* Reference: OMA-TS-DM_Security, nonce reuse
* Gap: A Chal is now attached to successful authenticated messages, but it repeats the session's current nonce rather than generating a new one per message.
* Impact: The previous credential stays replayable for the lifetime of the session, bounded by ACS_DM_SESSION_TTL_SECONDS, 600 seconds by default.

### `OMADM-AUTH-SERVER-TO-CLIENT` — Authenticate the server to the client when the client challenges it

* Status: **not-implemented** (code-review-only)
* Reference: OMA-TS-DM_Security, server authentication
* Gap: SyncMlBuilder.build emits no Cred in the server SyncHdr, and a Chal received from the client is ignored.
* Impact: A client configured to require mutual DM authentication will abandon the session. Clients that only authenticate themselves are unaffected.

### `OMADM-TREE-ACL` — Represent and enforce node access control lists

* Status: **not-implemented** (code-review-only)
* Reference: OMA-TS-DM_TND, access control lists
* Gap: MoNode.access is declared on every node in every management object and is never read. No ACL is evaluated, emitted or queried.
* Impact: Any authenticated DM peer could operate on any declared node. Reduced in practice because the only peer is the device itself, authenticated with a per-subscriber credential, and the server never executes client commands.

### `OMADM-MO-DEVDETAIL-URI-LIMITS` — Honour the URI depth and length limits the device reports

* Status: **not-implemented** (code-review-only)
* Reference: OMA-TS-DM_StdObj, DevDetail URI limits
* Gap: MaxDepth, MaxTotLen and MaxSegLen are collected into the device inventory and never consulted when building command URIs.
* Impact: A device with unusually tight URI limits could reject the deeper nodes, for example ./3GPP_IMS/1/Ext/VoLTE/AMRWB_Enabled.

### `OMADM-ENC-WBXML` — Accept the WBXML SyncML encoding

* Status: **not-implemented** (behaviour-tested)
* Reference: OMA-TS-DM_RepPro, WBXML encoding
* Gap: Only the XML encoding is implemented. A WBXML request is refused with HTTP 415 rather than answered with XML the client cannot decode.
* Impact: A WBXML-only DM client cannot be managed at all. This is the largest remaining interoperability gap on the DM plane.

### `RCC14-REQ-PARAMETERS` — Parse the documented query parameter set with types and length limits

* Status: **partial** (behaviour-tested)
* Reference: RCC.14 configuration request parameters
* Gap: The parameter set is taken from public descriptions, not from a pinned RCC.14 edition, so completeness cannot be asserted. Several parsed fields (IMEISV, SMS_format, rcs_state, provisioning_version, device_type, friendly_device_name, alias) are validated and recorded but do not yet influence the document that is built.
* Impact: A newer client may send a parameter this server files under "unknown". It is logged by name and ignored rather than rejected, so provisioning still succeeds.

### `RCC14-RESP-503` — Return 503 with Retry-After when the request cannot be served now

* Status: **partial** (behaviour-tested)
* Reference: RCC.14 temporary failure
* Gap: 503 is produced only when the requested SMS delivery mode is unavailable. Store or dependency failures surface as 500 from the generic handler rather than as a retryable 503.
* Impact: During a DynamoDB outage clients see 500 and back off on their own schedule instead of following a server-supplied Retry-After.

### `RCC14-VERS-NEGATIVE` — Distinguish the disable, dormant and blocked meanings of -1 to -4

* Status: **partial** (behaviour-tested)
* Reference: RCC.14 configuration version values
* Gap: The mapping is implemented, centralised in one reviewable table and tested, but only the version 0 row has been cross-checked. The meanings of -1 to -4 differ between RCC.14 releases and between vendors.
* Impact: Serving the wrong negative value can switch RCS off across a fleet, in the -2 and -4 cases without the client asking again. This is the highest-stakes unverified interpretation in the project.

### `RCC14-AUTH-MSISDN-FLOW` — Recover from a 511 through the MSISDN entry web flow

* Status: **partial** (behaviour-tested)
* Reference: RCC.14 MSISDN entry
* Gap: The page verifies the OTP and consumes it, but mints no token and records no verified state that the configuration flow can observe. The client's next request therefore finds no pending challenge and is challenged again.
* Impact: The 511 recovery path does not actually recover. It is the most significant functional gap on the RCS plane, and the reason the README no longer presents this flow as complete.

### `OMACP-DOC-MNC-LENGTH` — Use the correct MNC length for the subscriber's country

* Status: **partial** (behaviour-tested)
* Reference: 3GPP TS 23.003 IMSI structure
* Gap: A country table selects 2 or 3 digits and the length can be overridden, but no Subscriber field carries an override, so the table is always used.
* Impact: For an operator whose MNC length the table gets wrong, every derived IMS domain and ACS FQDN would be wrong. Correcting it currently means editing THREE_DIGIT_MNC_MCC.

### `OMACP-DOC-SEMANTIC-VALIDATION` — Validate the document against the specification's own rules

* Status: **partial** (behaviour-tested)
* Reference: OMA Provisioning Content 1.1 and RCC.07 parameter tables
* Gap: validate_structure checks generic shape, the root version and the presence of VERS. It does not validate against the OMA-CP DTD, nor check mandatory parameters per application, enum membership or integer ranges. Subscriber overrides bypass type coercion beyond the boolean case.
* Impact: A malformed override, or a parameter absent from a profile that requires it, would be served to a handset without complaint.

### `RCC14-PRIV-TLS` — Serve the configuration endpoint over TLS

* Status: **partial** (code-review-only)
* Reference: RCC.14 transport security
* Gap: TLS is terminated at the load balancer and only when a certificate is supplied. The CloudFormation stack permits an HTTP-only deployment, and scripts/deploy.sh warns rather than refuses.
* Impact: An HTTP deployment exposes IMSI, IMEI, MSISDN, the OTP and the token on the wire. Acceptable for a demonstration, never for real handsets.

## OMA Device Management 1.2 (server role)

* Specification: OMA-TS-DM_Protocol / DM_RepPro / DM_TND / DM_Security / DM_StdObj, version 1.2
* Role audited: server
* Edition pinned: **no**

| Id | Requirement | Level | Status | Evidence | Tests | Gap or note |
| --- | --- | --- | --- | --- | --- | --- |
| `OMADM-HDR-STRUCTURE` | Accept SyncML with SyncHdr and SyncBody, reject anything else | mandatory | yes | behaviour-tested | `test_wrong_root_element_is_rejected`<br>`test_missing_header_is_rejected`<br>`test_missing_body_is_rejected`<br>`test_malformed_xml_is_rejected` | — |
| `OMADM-HDR-VERDTD` | Emit VerDTD 1.2 in every server message | mandatory | yes | behaviour-tested | `test_builder_emits_a_well_formed_header` | — |
| `OMADM-HDR-VERPROTO-VALIDATE` | Reject a message whose VerProto is not DM/1.2 | mandatory | no | code-review-only | — | _parse_header defaults a missing or unexpected VerProto to DM/1.2 instead of refusing the message, and no SyncML 513 is produced. |
| `OMADM-HDR-SESSIONID` | Require and echo the SessionID | mandatory | yes | behaviour-tested | `test_missing_session_id_returns_400` | — |
| `OMADM-HDR-SESSION-BINDING` | Bind server session state to the device, not to the SessionID alone | mandatory | yes | behaviour-tested | `test_two_devices_with_the_same_session_id_do_not_collide` | SessionID is chosen by the device and is commonly a small integer, so keying server state on it alone lets two handsets share one session. |
| `OMADM-HDR-MSGID` | Require the MsgID and reference it from every Status | mandatory | partial | behaviour-tested | `test_status_carries_the_references` | The MsgID is required and echoed as MsgRef, but ordering, duplicates and replay are not checked, and the server mirrors the client MsgID rather than keeping its own outbound counter. |
| `OMADM-HDR-ADDRESSING` | Set Target to the client source and Source to the DM server URI | mandatory | yes | behaviour-tested | `test_server_addresses_its_response_to_the_client_source` | — |
| `OMADM-HDR-MAXMSGSIZE` | Never advertise a message size larger than the client accepts | mandatory | yes | behaviour-tested | `test_client_max_msg_size_is_honoured` | — |
| `OMADM-SIZE-SPLITTING` | Split a package that exceeds the negotiated MaxMsgSize | mandatory | no | code-review-only | — | The negotiated size is advertised but the server always emits its commands in a single package. DmSession.pending exists for this and is unused. |
| `OMADM-ALERT-1222` | Continue a split package on Alert 1222 | conditional | no | code-review-only | — | The constant is defined; no handler exists. |
| `OMADM-FINAL` | Mark the last message of a package with Final | mandatory | yes | behaviour-tested | `test_non_final_package_omits_final`<br>`test_full_session_pushes_ims_and_volte_configuration` | — |
| `OMADM-FLOW-CLIENT-INIT` | Accept a client-initiated session opened with Alert 1201 | mandatory | yes | behaviour-tested | `test_valid_basic_credentials_are_accepted`<br>`test_first_package_without_an_alert_is_a_protocol_error` | — |
| `OMADM-FLOW-SERVER-INIT-ALERT` | Accept a session opened with Alert 1200 | conditional | yes | behaviour-tested | `test_server_initiated_alert_is_also_accepted` | — |
| `OMADM-FLOW-NOTIFICATION` | Originate a server-initiated session notification | optional | no | code-review-only | — | Waking a device requires a WAP Push or trigger SMS through an operator SMSC, the same dependency that blocks port-addressed OTP SMS. |
| `OMADM-FLOW-PACKAGES` | Drive the client-init, server-commands, client-results, close flow | mandatory | yes | behaviour-tested | `test_full_session_pushes_ims_and_volte_configuration`<br>`test_oma_dm_session_uses_the_password_bootstrapped_by_oma_cp` | — |
| `OMADM-FLOW-CORRELATION` | Match incoming Status and Results to the commands that were sent | mandatory | partial | behaviour-tested | `test_client_reported_failures_are_surfaced` | Non-2xx statuses are detected and logged, but CmdRef and MsgRef are not matched against the commands the server issued, and a phase advances even if the expected Results are absent. |
| `OMADM-ALERT-1226-END` | End the session when the client sends Alert 1226 | mandatory | yes | behaviour-tested | `test_client_ending_the_session_is_acknowledged` | — |
| `OMADM-ALERT-1223-ABORT` | Abort and discard session state when the client sends Alert 1223 | mandatory | yes | behaviour-tested | `test_alert_1223_aborts_the_session` | — |
| `OMADM-CMD-GET` | Issue Get and consume the Results | mandatory | yes | behaviour-tested | `test_device_inventory_is_recorded_from_the_session`<br>`test_get_command_lists_every_uri` | — |
| `OMADM-CMD-REPLACE` | Issue Replace with Format and Type meta for every leaf | mandatory | yes | behaviour-tested | `test_replace_carries_meta_and_data`<br>`test_full_session_pushes_ims_and_volte_configuration` | — |
| `OMADM-CMD-ADD-INTERIOR` | Add the interior nodes a leaf needs before replacing that leaf | mandatory | yes | behaviour-tested | `test_interior_nodes_are_added_before_leaves`<br>`test_interior_nodes_are_ordered_parent_first`<br>`test_add_of_an_interior_node_carries_no_data` | — |
| `OMADM-CMD-STATUS` | Return a Status for the header and for every command received | mandatory | yes | behaviour-tested | `test_every_received_command_gets_a_status` | — |
| `OMADM-CMD-RESULTS` | Parse Results and record the values reported by the device | mandatory | yes | behaviour-tested | `test_device_inventory_is_recorded_from_the_session` | — |
| `OMADM-CMD-EXEC` | Issue Exec against an executable node | optional | partial | behaviour-tested | `test_exec_and_alert_commands_are_emitted` | The command can be generated, but no management object declares an executable node and the session never issues one. |
| `OMADM-CMD-DELETE` | Issue Delete, or refuse it honestly when received | optional | no | behaviour-tested | `test_unsupported_commands_are_refused_not_acknowledged` | The server never issues Delete, and a received Delete is answered 406 rather than executed. |
| `OMADM-CMD-COPY` | Issue Copy, or refuse it honestly when received | optional | no | behaviour-tested | `test_unsupported_commands_are_refused_not_acknowledged` | Not implemented; a received Copy is answered 406. |
| `OMADM-CMD-SEQUENCE` | Execute a Sequence block in order | optional | no | behaviour-tested | `test_unsupported_commands_are_refused_not_acknowledged` | Commands nested inside Sequence are not parsed, and a received Sequence is answered 406 rather than executed. |
| `OMADM-CMD-ATOMIC` | Execute an Atomic block, rolling back on failure | optional | no | behaviour-tested | `test_unsupported_commands_are_refused_not_acknowledged` | No transaction support, and therefore no 507 or 516. A received Atomic is answered 406. |
| `OMADM-STATUS-200` | Emit 200 for a command that was performed | mandatory | yes | behaviour-tested | `test_every_received_command_gets_a_status` | — |
| `OMADM-STATUS-212` | Emit 212 when client authentication has been accepted | mandatory | yes | behaviour-tested | `test_valid_basic_credentials_are_accepted` | — |
| `OMADM-STATUS-401` | Emit 401 with a Chal when the supplied credentials are wrong | mandatory | yes | behaviour-tested | `test_wrong_credentials_produce_401_not_407` | — |
| `OMADM-STATUS-407` | Emit 407 with a Chal when no credentials were supplied | mandatory | yes | behaviour-tested | `test_missing_credentials_produce_a_syncml_challenge` | Distinguishing 407 from 401 matters: 401 tells a client its credentials are bad when in fact it had not been challenged yet. |
| `OMADM-STATUS-404` | Emit 404 when a command targets a node the server does not know | mandatory | yes | behaviour-tested | `test_unknown_node_in_a_command_is_reported_not_found` | — |
| `OMADM-STATUS-406` | Emit 406 for a command the server does not implement | mandatory | yes | behaviour-tested | `test_unsupported_commands_are_refused_not_acknowledged`<br>`test_unknown_command_name_is_refused` | These commands were previously answered 200, i.e. the server claimed to have performed work it had not done. |
| `OMADM-STATUS-418` | Treat 418 already-exists as success for an interior node Add | mandatory | yes | behaviour-tested | `test_already_exists_is_not_treated_as_a_failure` | — |
| `OMADM-STATUS-425` | Emit 425 when an ACL forbids the operation | conditional | no | code-review-only | — | Conditional on OMADM-TREE-ACL; no ACL is evaluated, so 425 never applies. |
| `OMADM-STATUS-5XX` | Interpret a client 5xx status for a command the server issued | mandatory | partial | behaviour-tested | `test_client_reported_failures_are_surfaced` | A failure is counted, logged and reflected in the metric, but there is no retry, repair or alarm path. |
| `OMADM-AUTH-BASIC` | Verify syncml:auth-basic credentials | conditional | yes | behaviour-tested | `test_valid_basic_credentials_are_accepted`<br>`test_wrong_password_is_rejected` | — |
| `OMADM-AUTH-MD5` | Verify syncml:auth-md5 credentials against a server-issued nonce | conditional | yes | behaviour-tested | `test_md5_credentials_are_verified_against_the_session_nonce`<br>`test_md5_with_a_wrong_password_is_rejected` | — |
| `OMADM-AUTH-CHAL` | Issue a Chal carrying the next nonce when authentication fails | mandatory | yes | behaviour-tested | `test_authentication_challenge_includes_a_nonce`<br>`test_missing_credentials_produce_a_syncml_challenge` | — |
| `OMADM-AUTH-NONCE-ROTATION` | Send a fresh nonce on each authenticated message | mandatory | partial | behaviour-tested | `test_md5_sessions_carry_a_chal_on_success` | A Chal is now attached to successful authenticated messages, but it repeats the session's current nonce rather than generating a new one per message. |
| `OMADM-AUTH-SERVER-TO-CLIENT` | Authenticate the server to the client when the client challenges it | mandatory | no | code-review-only | — | SyncMlBuilder.build emits no Cred in the server SyncHdr, and a Chal received from the client is ignored. |
| `OMADM-AUTH-NO-ENUMERATION` | Do not reveal whether a DM username exists | optional | yes | behaviour-tested | `test_unknown_user_is_indistinguishable_from_a_wrong_password` | — |
| `OMADM-TREE-ADDRESSING` | Address nodes by URI, distinguishing interior from leaf | mandatory | yes | behaviour-tested | `test_node_lookup_by_uri`<br>`test_children_are_direct_descendants_only` | — |
| `OMADM-TREE-META` | Send Format and Type meta with every value written | mandatory | yes | behaviour-tested | `test_replace_carries_meta_and_data` | — |
| `OMADM-TREE-ACL` | Represent and enforce node access control lists | mandatory | no | code-review-only | — | MoNode.access is declared on every node in every management object and is never read. No ACL is evaluated, emitted or queried. |
| `OMADM-TREE-PROP-LIST` | Support the property and list tree queries | optional | no | code-review-only | — | MoTree.node only normalises a trailing slash; query suffixes are not parsed. |
| `OMADM-TREE-CLIENT-GET` | Answer a Get issued by the client with Results | optional | no | behaviour-tested | `test_client_get_is_acknowledged_but_not_answered` | A client Get receives a Status but never a Results, because the server holds no tree of its own to read. |
| `OMADM-MO-DEVINFO` | Read the DevInfo management object | mandatory | yes | behaviour-tested | `test_real_tree_loads_the_expected_objects`<br>`test_device_inventory_is_recorded_from_the_session` | — |
| `OMADM-MO-DEVDETAIL` | Read the DevDetail management object | mandatory | yes | behaviour-tested | `test_device_query_uris_are_the_device_owned_leaves` | — |
| `OMADM-MO-DEVDETAIL-URI-LIMITS` | Honour the URI depth and length limits the device reports | mandatory | no | code-review-only | — | MaxDepth, MaxTotLen and MaxSegLen are collected into the device inventory and never consulted when building command URIs. |
| `OMADM-MO-DMACC` | Expose the DM account as a manageable object | optional | no | code-review-only | — | The DM account is bootstrapped one way, through the OMA-CP w7 characteristic. There is no DMAcc management object, so the account cannot be inspected or rotated over DM. |
| `OMADM-MO-EXTENSIBLE` | Add a management object without changing server code | optional | yes | behaviour-tested | `test_a_new_management_object_needs_no_code` | This is the extensibility requirement the project was asked for: a new managed service is a YAML file. |
| `OMADM-ENC-XML` | Accept the XML SyncML encoding | mandatory | yes | behaviour-tested | `test_dm_endpoint_answers_over_http` | — |
| `OMADM-ENC-WBXML` | Accept the WBXML SyncML encoding | mandatory | no | behaviour-tested | `test_wbxml_is_refused_explicitly` | Only the XML encoding is implemented. A WBXML request is refused with HTTP 415 rather than answered with XML the client cannot decode. |
| `OMADM-SEC-XXE` | Parse device-supplied SyncML without resolving external entities | mandatory | yes | behaviour-tested | `test_external_entities_are_not_expanded` | — |
| `OMADM-SEC-BODY-LIMIT` | Bound the size of an accepted DM payload | mandatory | yes | behaviour-tested | `test_oversized_payload_is_refused`<br>`test_dm_endpoint_refuses_a_huge_body` | — |
| `OMADM-SEC-SESSION-STORE` | Keep session state where every server task can see it | mandatory | yes | behaviour-tested | `test_dm_session_round_trip`<br>`test_expired_dm_session_is_treated_as_absent` | A DM session spans several HTTP requests, so in-process state would break the moment the service runs more than one task. |

## GSMA RCC.14 Service Provider Device Configuration (ACS role)

* Specification: GSMA RCC.14 and RCC.07, with OMA Provisioning Content 1.1 as the document format
* Role audited: server
* Edition pinned: **no**

| Id | Requirement | Level | Status | Evidence | Tests | Gap or note |
| --- | --- | --- | --- | --- | --- | --- |
| `RCC14-REQ-ENDPOINT` | Serve the configuration request on the paths deployed clients use | mandatory | yes | behaviour-tested | `test_every_configured_path_serves_the_flow` | Deployed clients disagree on the path, so /, /config and /rcs/config are all registered and the list is configurable. |
| `RCC14-REQ-PARAMETERS` | Parse the documented query parameter set with types and length limits | mandatory | partial | behaviour-tested | `test_parses_the_full_documented_parameter_set`<br>`test_oversized_terminal_model_is_rejected` | The parameter set is taken from public descriptions, not from a pinned RCC.14 edition, so completeness cannot be asserted. Several parsed fields (IMEISV, SMS_format, rcs_state, provisioning_version, device_type, friendly_device_name, alias) are validated and recorded but do not yet influence the document that is built. |
| `RCC14-REQ-IMSI-STRING` | Treat the IMSI as a string, preserving leading zeros | mandatory | yes | behaviour-tested | `test_imsi_leading_zeros_are_preserved` | Parsing an IMSI as an integer silently changes the MCC. |
| `RCC14-REQ-IMEI-TOLERANT` | Accept IMEI and IMEISV shapes including non-Luhn test values | mandatory | yes | behaviour-tested | `test_imei_and_imeisv_lengths_are_accepted`<br>`test_non_luhn_test_imei_is_accepted` | Rejecting a non-Luhn IMEI locks field-test handsets out of provisioning. |
| `RCC14-REQ-NEGATIVE-VERS` | Accept a negative vers in a request | mandatory | yes | behaviour-tested | `test_negative_vers_is_accepted` | Clients echo back the disable value they were previously given. |
| `RCC14-REQ-REPEATED-APP` | Accept a repeated app parameter and filter the document accordingly | optional | yes | behaviour-tested | `test_repeated_app_parameter_is_collected`<br>`test_repeated_app_parameter_filters_the_document` | — |
| `RCC14-REQ-NO-PARAM-SMUGGLING` | Reject a repeated identity or OTP parameter | mandatory | yes | behaviour-tested | `test_repeated_identity_parameter_is_rejected`<br>`test_repeated_otp_is_rejected` | A duplicate is how a second identity is smuggled past a proxy that only inspects the first occurrence. |
| `RCC14-REQ-POST-BODY` | Accept the OTP step as a POST body rather than a query string | optional | yes | behaviour-tested | `test_otp_can_be_supplied_in_a_post_body`<br>`test_post_is_accepted_for_the_otp_step` | The point of offering POST is that the OTP need not appear in a query string, where it lands in every proxy access log on the way. |
| `RCC14-REQ-MALFORMED` | Answer a structurally invalid request without disabling the client | mandatory | yes | behaviour-tested | `test_malformed_parameters_are_rejected_with_400`<br>`test_malformed_status_is_configurable` | 400 by default, configurable to 403. The specification does not pin this down; 400 is chosen because 403 makes a client mark itself as barred. |
| `RCC14-RESP-CONFIG` | Return 200 with a wap-provisioningdoc to an authenticated client | mandatory | yes | behaviour-tested | `test_full_otp_flow_returns_a_valid_document`<br>`test_correct_otp_returns_the_configuration` | — |
| `RCC14-RESP-OTP-PENDING` | Signal a pending OTP with 200 and an empty body | mandatory | yes | behaviour-tested | `test_first_request_sends_an_otp_and_answers_200_empty` | — |
| `RCC14-RESP-VERS-ONLY` | Return a VERS-only document when the client already holds the revision | optional | yes | behaviour-tested | `test_client_holding_the_current_version_gets_a_vers_only_document` | Omitting this optimisation means re-sending about 130 parameters to the whole fleet on every validity refresh. |
| `RCC14-RESP-403` | Return 403 for a known but unentitled subscriber | mandatory | yes | behaviour-tested | `test_non_entitled_subscriber_returns_403`<br>`test_non_entitled_subscriber_receives_403` | — |
| `RCC14-RESP-511` | Return 511 when the subscriber cannot be identified | mandatory | yes | behaviour-tested | `test_unknown_subscriber_returns_511`<br>`test_unknown_subscriber_receives_511` | — |
| `RCC14-RESP-429` | Return 429 with Retry-After when an OTP rate limit is reached | optional | yes | behaviour-tested | `test_daily_quota_exhaustion_returns_429` | — |
| `RCC14-RESP-503` | Return 503 with Retry-After when the request cannot be served now | mandatory | partial | behaviour-tested | `test_port_addressed_otp_is_refused_rather_than_downgraded` | 503 is produced only when the requested SMS delivery mode is unavailable. Store or dependency failures surface as 500 from the generic handler rather than as a retryable 503. |
| `RCC14-RESP-NO-STORE` | Forbid caching of every configuration response | mandatory | yes | behaviour-tested | `test_every_response_forbids_caching`<br>`test_configuration_response_declares_xml_and_forbids_caching` | The body carries IMS credentials and a bearer token. |
| `RCC14-RESP-MSG` | Return a MSG characteristic to show text to the user | optional | no | code-review-only | — | builder.msg_characteristic and builder.build_message_document exist and are unit-tested, but no decision path in ProvisioningService selects them, so a MSG document is never returned. |
| `RCC14-RESP-NO-STACKTRACE` | Never leak internal detail to a device on an unhandled error | mandatory | yes | behaviour-tested | `test_unhandled_error_does_not_leak_internals` | — |
| `RCC14-VERS-POSITIVE` | Treat a positive version as a configuration revision to apply | mandatory | yes | behaviour-tested | `test_every_documented_version_maps_to_its_action` | — |
| `RCC14-VERS-ZERO` | Treat version 0 as configuration invalid with RCS disabled | mandatory | yes | behaviour-tested | `test_every_documented_version_maps_to_its_action`<br>`test_forced_disable_values_are_served_without_configuration` | — |
| `RCC14-VERS-NEGATIVE` | Distinguish the disable, dormant and blocked meanings of -1 to -4 | mandatory | partial | behaviour-tested | `test_deleting_versions_are_the_ones_that_wipe_configuration`<br>`test_no_requery_values_are_minus_two_and_minus_four`<br>`test_forced_disable_values_are_served_without_configuration` | The mapping is implemented, centralised in one reviewable table and tested, but only the version 0 row has been cross-checked. The meanings of -1 to -4 differ between RCC.14 releases and between vendors. |
| `RCC14-VERS-MONOTONIC` | Never decrease a configuration revision | mandatory | yes | behaviour-tested | `test_version_bump_is_monotonic_and_recovers_from_disable_values`<br>`test_zero_stored_version_is_repaired_to_one` | A decreasing version can wedge a client. |
| `RCC14-VERS-VALIDITY` | Emit a validity lifetime alongside the version | mandatory | yes | behaviour-tested | `test_document_starts_with_vers` | — |
| `RCC14-AUTH-OTP` | Authenticate a subscriber with a single-use SMS OTP | mandatory | yes | behaviour-tested | `test_valid_otp_verifies_once`<br>`test_otp_verification_consumes_the_challenge` | — |
| `RCC14-AUTH-OTP-ABUSE` | Bound OTP sends and verification attempts | mandatory | yes | behaviour-tested | `test_daily_quota_stops_sms_pumping`<br>`test_attempts_are_bounded`<br>`test_resend_within_the_cooldown_is_blocked` | An unbounded OTP endpoint is a direct financial attack on the operator. |
| `RCC14-AUTH-OTP-PORT` | Deliver the OTP as a port-addressed binary SMS when SMS_port is given | conditional | no | behaviour-tested | `test_smpp_sender_is_explicitly_unimplemented`<br>`test_port_addressing_udh_is_built_correctly`<br>`test_port_addressed_otp_is_refused_rather_than_downgraded` | No AWS SMS service can set a User Data Header. SMS_port is carried end to end, the UDH builder is implemented and tested, and the AWS providers raise rather than send a text message the client will never read. |
| `RCC14-AUTH-TOKEN` | Let a token replace the OTP challenge on later requests | mandatory | yes | behaviour-tested | `test_token_skips_the_otp_challenge`<br>`test_valid_token_resolves` | — |
| `RCC14-AUTH-TOKEN-BINDING` | Bind a token to the subscriber and the handset, and allow revocation | mandatory | yes | behaviour-tested | `test_token_bound_to_another_imei_is_rejected`<br>`test_only_the_digest_is_persisted`<br>`test_revoked_token_forces_rebootstrap` | — |
| `RCC14-AUTH-TOKEN-INVALID-REBOOTSTRAP` | Send a client back to bootstrapping when its token is invalid | mandatory | yes | behaviour-tested | `test_invalid_token_sends_the_client_back_to_bootstrapping` | — |
| `RCC14-AUTH-CLAIM-NOT-CREDENTIAL` | Never authenticate on a claimed identity alone | mandatory | yes | behaviour-tested | `test_bare_msisdn_parameter_is_only_a_claim` | A bare msisdn or IMSI query parameter is a claim. Treating it as proof would hand one subscriber's IMS credentials to anyone who asked. |
| `RCC14-AUTH-ENRICHMENT` | Accept an operator-asserted identity header only from a trusted peer | conditional | partial | behaviour-tested | `test_trusted_peer_identity_is_accepted`<br>`test_untrusted_peer_identity_is_ignored`<br>`test_forged_leading_forwarded_for_entry_does_not_grant_trust` | Real enrichment requires an operator packet gateway. What exists is a header honoured only when the peer address falls inside an explicitly configured trusted CIDR, and the mechanism is disabled by default. |
| `RCC14-AUTH-GBA-CHALLENGE` | Issue a GBA bootstrap challenge with AKAv1-MD5 | conditional | yes | behaviour-tested | `test_challenge_header_declares_akav1_md5`<br>`test_gba_challenge_replaces_511_when_enabled` | — |
| `RCC14-AUTH-GBA-VERIFY` | Verify the GBA Digest response rather than trusting the B-TID | mandatory | yes | behaviour-tested | `test_gba_btid_alone_does_not_authenticate`<br>`test_gba_forged_digest_response_is_rejected`<br>`test_gba_nonce_the_server_never_issued_is_rejected`<br>`test_gba_response_bound_to_the_http_method` | A B-TID travels in the clear in the username directive. An earlier revision of this server authenticated on the B-TID alone, which was an authentication bypass; the digest response and the nonce origin are now both verified. |
| `RCC14-AUTH-GBA-REAL` | Bootstrap keys from a real BSF over Zn | conditional | no | behaviour-tested | `test_unconfigured_bsf_fails_closed`<br>`test_factory_returns_mock_in_dev_and_failclosed_in_prod` | Real GBA needs a USIM performing AKA, a BSF reachable over Ub and an HSS. A BsfClient port and a deterministic mock exist; in staging or production the unconfigured client raises rather than fake a successful bootstrap. |
| `RCC14-AUTH-MSISDN-FLOW` | Recover from a 511 through the MSISDN entry web flow | mandatory | partial | behaviour-tested | `test_msisdn_flow_sends_an_otp_and_verifies_it`<br>`test_msisdn_flow_does_not_reveal_whether_a_number_exists`<br>`test_msisdn_web_verification_does_not_yet_complete_provisioning` | The page verifies the OTP and consumes it, but mints no token and records no verified state that the configuration flow can observe. The client's next request therefore finds no pending challenge and is challenged again. |
| `OMACP-DOC-ROOT` | Emit a wap-provisioningdoc version 1.1 root | mandatory | yes | behaviour-tested | `test_serialised_document_is_valid_and_declares_utf8`<br>`test_structural_validation_rejects_a_wrong_root` | — |
| `OMACP-DOC-VERS-FIRST` | Always include the VERS characteristic | mandatory | yes | behaviour-tested | `test_structural_validation_catches_a_missing_vers` | — |
| `OMACP-DOC-APPLICATIONS` | Emit the IMS and RCS applications with the RCS to IMS reference | mandatory | yes | behaviour-tested | `test_both_applications_are_emitted`<br>`test_rcs_application_references_the_ims_application` | — |
| `OMACP-DOC-IDENTITIES` | Derive IMPI, IMPU and home domain from the IMSI | mandatory | yes | behaviour-tested | `test_derived_identity_matches_the_3gpp_naming_convention`<br>`test_placeholders_are_resolved_from_the_subscriber` | — |
| `OMACP-DOC-MNC-LENGTH` | Use the correct MNC length for the subscriber's country | mandatory | partial | behaviour-tested | `test_north_american_mccs_use_three_digit_mncs`<br>`test_mnc_length_can_be_overridden` | A country table selects 2 or 3 digits and the length can be overridden, but no Subscriber field carries an override, so the table is always used. |
| `OMACP-DOC-DETERMINISTIC` | Emit parameters in a deterministic order | mandatory | yes | behaviour-tested | `test_serialisation_is_deterministic` | Some clients are sensitive to element order. |
| `OMACP-DOC-ESCAPING` | Escape parameter values correctly | mandatory | yes | behaviour-tested | `test_special_characters_in_values_are_escaped` | Documents are built with lxml, never string templates: a friendly device name may legitimately contain an ampersand or an angle bracket. |
| `OMACP-DOC-OMIT-EMPTY` | Omit an optional parameter with no value rather than emit an empty one | optional | yes | behaviour-tested | `test_empty_optional_values_are_omitted_not_emitted_blank` | Some clients treat an empty string as a real setting. |
| `OMACP-DOC-SEMANTIC-VALIDATION` | Validate the document against the specification's own rules | mandatory | partial | behaviour-tested | `test_serialised_document_is_valid_and_declares_utf8` | validate_structure checks generic shape, the root version and the presence of VERS. It does not validate against the OMA-CP DTD, nor check mandatory parameters per application, enum membership or integer ranges. Subscriber overrides bypass type coercion beyond the boolean case. |
| `OMACP-DOC-DEFAULT-SMS-APP` | Suppress messaging authorisations when the client is not the default SMS app | mandatory | yes | behaviour-tested | `test_default_sms_app_zero_disables_messaging_authorisations` | Offering standalone messaging otherwise produces duplicate delivery. |
| `OMACP-DOC-PROFILES` | Vary the document by the client's declared RCS profile | optional | yes | behaviour-tested | `test_profile_selection_changes_the_document`<br>`test_up_1_0_removes_chatbot_parameters` | — |
| `OMACP-DOC-DM-BOOTSTRAP` | Bootstrap the OMA-DM account from the configuration document | optional | yes | behaviour-tested | `test_dm_account_is_bootstrapped_when_a_password_exists`<br>`test_oma_dm_session_uses_the_password_bootstrapped_by_oma_cp` | This is the bridge between the two planes, and the chain is verified end to end: provision over RCC.14, read AAUTHSECRET, authenticate a DM session. |
| `OMACP-DOC-DOCTYPE` | Emit the OMA-CP DOCTYPE declaration | conditional | partial | behaviour-tested | `test_doctype_can_be_emitted` | The DOCTYPE can be emitted but the served document omits it, and whether RCC.14 requires it could not be determined without the licensed text. |
| `OMACP-SEC-XXE` | Parse OMA-CP documents without resolving external entities | mandatory | yes | behaviour-tested | `test_parser_does_not_resolve_external_entities` | — |
| `RCC14-PRIV-NO-PII-LOGS` | Never write a subscriber identifier to a log in the clear | mandatory | yes | behaviour-tested | `test_no_raw_identifier_appears_in_request_logs`<br>`test_no_raw_identifier_appears_in_oma_dm_logs` | The identifiers arrive in the query string, so every default access log format records them. uvicorn's access log is disabled for this reason. |
| `RCC14-PRIV-NO-PII-METRICS` | Keep subscriber identifiers out of metric dimensions | mandatory | yes | behaviour-tested | `test_metric_dimensions_are_low_cardinality` | — |
| `RCC14-PRIV-NO-ENUMERATION` | Do not reveal whether a subscriber is known | mandatory | yes | behaviour-tested | `test_msisdn_flow_does_not_reveal_whether_a_number_exists`<br>`test_unknown_subscriber_returns_511` | — |
| `RCC14-PRIV-TLS` | Serve the configuration endpoint over TLS | mandatory | partial | code-review-only | `test_responses_request_transport_security` | TLS is terminated at the load balancer and only when a certificate is supplied. The CloudFormation stack permits an HTTP-only deployment, and scripts/deploy.sh warns rather than refuses. |
| `RCC14-OPS-FAIL-CLOSED` | Refuse to start on a configuration unsafe for the declared environment | mandatory | yes | behaviour-tested | `test_invalid_production_configuration_refuses_to_start`<br>`test_gba_cannot_be_enabled_without_a_nonce_secret` | A misconfigured ACS that starts anyway can switch RCS off on every handset that talks to it. |

## Extending this registry

Add a row to `src/acs/catalog/conformance/omadm.yaml` or `rcc14.yaml`, name the test that proves it, and run `make conformance-doc`. The loader refuses a row that claims a status without evidence, and refuses compliance wording outright.
