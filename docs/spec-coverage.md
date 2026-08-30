<!-- GENERATED FILE — edit the catalogues, then run scripts/gen_spec_coverage.py -->
# Specification coverage

This document is generated from the declarative catalogues:

* `src/acs/catalog/omacp/` — OMA-CP provisioning parameters (RCC.14 / RCC.07)
* `src/acs/catalog/omadm/` — OMA-DM management objects
* `src/acs/protocol/vers.py` — configuration version semantics

## How to read `verified`

`verified: true` means the entry has been cross-checked against the pinned
specification edition named in `docs/scope.md`. `verified: false` means the entry
is implemented from public descriptions of the specification and from
configurations that are widely deployed in the field: it is structurally correct,
typed and tested, but the repository does **not** claim clause-level
certification for it.

Nothing here is a GSMA certification. See `docs/limitations.md`.

## Summary

| Surface | Entries | Cross-checked |
| --- | --- | --- |
| OMA-CP parameters | 116 | `####................` 25/116 (22%) |
| OMA-DM nodes | 47 | `##########..........` 23/47 (49%) |
| Version semantics rows | 5 | `####................` 1/5 (20%) |

Available RCS profiles: `UP_1.0`, `UP_2.4`, `joyn_blackbird`

## Configuration version semantics

| `VERS/version` | Client action | Deletes config | May re-query | Reference | Cross-checked |
| --- | --- | --- | --- | --- | --- |
| `0` | disable_keep | no | yes | RCC.14 Configuration version values | yes |
| `-1` | disable_delete_retry | yes | yes | RCC.14 Configuration version values | no |
| `-2` | disable_delete_no_retry | yes | no | RCC.14 Configuration version values | no |
| `-3` | dormant | no | yes | RCC.14 Configuration version values | no |
| `-4` | blocked | no | no | RCC.14 Configuration version values | no |

> The interpretation of `-1` to `-4` differs between RCC.14 releases and between vendor implementations. It is encoded as a single reviewable table in `src/acs/protocol/vers.py` for exactly that reason.

## OMA-CP parameters

### `APPLICATION:ap2001`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `Name` | chr | `IMS Settings` | OMA-CP 1.1 / RCC.14 IMS application | yes |
| `Private_User_Identity` | chr | `{impi}` | 3GPP TS 24.167 5.2.2 | yes |
| `Home_network_domain_name` | chr | `{ims_domain}` | 3GPP TS 24.167 5.2.4 | yes |
| `Authentication_Type` | enum | `AKA` | 3GPP TS 24.167 5.2.7 | yes |
| `UserName` | chr | `{impi}` | 3GPP TS 24.167 5.2.7.1 | no |
| `Timer_T1` | int | `2000` ms | 3GPP TS 24.167 5.2.9 | no |
| `Timer_T2` | int | `16000` ms | 3GPP TS 24.167 5.2.10 | no |
| `Timer_T4` | int | `17000` ms | 3GPP TS 24.167 5.2.11 | no |
| `Phone_Context` | chr | `{ims_domain}` | 3GPP TS 24.167 5.2.13 | no |
| `PDP_ContextOperPref` | bool01 | `0` | 3GPP TS 24.167 5.2.8 | no |
| `Resource_Allocation_Mode` | bool01 | `0` | 3GPP TS 24.167 5.2.12 | no |
| `Voice_Domain_Preference_E_UTRAN` | enum | `3` | 3GPP TS 24.167 5.2.16 (VoLTE domain selection) | no |
| `SMS_Over_IP_Networks_Indication` | bool01 | `1` | 3GPP TS 24.167 5.2.17 | no |
| `Keep_Alive_Enabled` | bool01 | `1` | 3GPP TS 24.167 5.2.18 | no |

### `APPLICATION:ap2001/Public_User_Identity_List/Public_User_Identity`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `Public_User_Identity` | chr | `{impu}` | 3GPP TS 24.167 5.2.3 | yes |

### `APPLICATION:ap2001/LBO_P-CSCF_Address`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `Address` | chr | `pcscf.{ims_domain}` | 3GPP TS 24.167 5.2.5 | yes |
| `AddressType` | enum | `FQDN` | 3GPP TS 24.167 5.2.6 | yes |

### `APPLICATION:ap2001/ICSI_List/ICSI`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `ICSI` | chr | `urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel` | 3GPP TS 24.167 5.2.14 (MMTEL / VoLTE communication service identifier) | no |

### `APPLICATION:ap2001/Ext/GSMA`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `AppRef` | chr | `IMS-Settings` | RCC.07 IMS/RCS application linkage | no |
| `rcsVolteSingleRegistration` | bool01 | `1` | RCC.07 Single Registration (RCS over VoLTE IMS registration) | no |

### `APPLICATION:ap2001/Ext/NAT`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `natUrlFmt` | bool01 | `0` | RCC.07 NAT traversal | no |
| `intUrlFmt` | bool01 | `0` | RCC.07 NAT traversal | no |
| `natKeepAliveTimeout` | int | `30` s | RCC.07 NAT keep-alive | no |

### `APPLICATION:ap2002`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `Name` | chr | `RCS settings` | RCC.14 RCS application | yes |
| `AppRef` | chr | `ap2001` | RCC.14 RCS -> IMS application reference | yes |

### `APPLICATION:ap2002/SERVICES`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `presencePrfl` | bool01 | `0` | RCC.07 SERVICES | no |
| `ChatAuth` | bool01 | `1` | RCC.07 SERVICES ChatAuth | yes |
| `GroupChatAuth` | bool01 | `1` | RCC.07 SERVICES GroupChatAuth | yes |
| `ftAuth` | bool01 | `1` | RCC.07 SERVICES ftAuth | yes |
| `standaloneMsgAuth` | bool01 | `1` | RCC.07 SERVICES standaloneMsgAuth | yes |
| `geolocPushAuth` | bool01 | `1` | RCC.07 SERVICES geolocPushAuth | no |
| `geolocPullAuth` | bool01 | `0` | RCC.07 SERVICES geolocPullAuth | no |
| `callComposerAuth` | bool01 | `1` | RCC.07 SERVICES callComposerAuth (Enriched Calling) | no |
| `postCallAuth` | bool01 | `1` | RCC.07 SERVICES postCallAuth | no |
| `sharedMapAuth` | bool01 | `0` | RCC.07 SERVICES sharedMapAuth | no |
| `sharedSketchAuth` | bool01 | `0` | RCC.07 SERVICES sharedSketchAuth | no |
| `chatbotCommunicationAuth` | bool01 | `1` | RCC.07 SERVICES chatbotCommunicationAuth (UP 2.x chatbots) | no |
| `plugInAuth` | bool01 | `0` | RCC.07 SERVICES plugInAuth | no |
| `videoCallAuth` | bool01 | `1` | RCC.07 SERVICES videoCallAuth (VoLTE video / ViLTE) | no |
| `vsAuth` | bool01 | `1` | RCC.07 SERVICES vsAuth (video share) | no |
| `callUnansweredAuth` | bool01 | `0` | RCC.07 SERVICES callUnansweredAuth | no |

### `APPLICATION:ap2002/MESSAGING/CHAT`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `ChatAuth` | bool01 | `1` | RCC.07 CHAT | no |
| `AutAccept` | bool01 | `1` | RCC.07 CHAT AutAccept | yes |
| `AutAcceptGroupChat` | bool01 | `1` | RCC.07 CHAT AutAcceptGroupChat | yes |
| `TimerIdle` | int | `300` s | RCC.07 CHAT TimerIdle | no |
| `MaxSize` | int | `8192` byte | RCC.07 CHAT MaxSize | yes |
| `ConfFctyURI` | chr | `sip:conf-factory@{ims_domain}` | RCC.07 CHAT ConfFctyURI | yes |
| `ExploderURI` | chr | `sip:exploder@{ims_domain}` | RCC.07 CHAT ExploderURI | no |
| `deferredMsgFuncUri` | chr | `sip:deferred@{ims_domain}` | RCC.07 CHAT deferredMsgFuncUri | no |
| `MaxAdhocGroupSize` | int | `100` | RCC.07 CHAT MaxAdhocGroupSize | no |
| `imCapAlwaysON` | bool01 | `1` | RCC.07 CHAT imCapAlwaysON | no |
| `imWarnSF` | bool01 | `0` | RCC.07 CHAT imWarnSF | no |
| `imWarnIW` | bool01 | `0` | RCC.07 CHAT imWarnIW | no |
| `imSessionStart` | enum | `2` | RCC.07 CHAT imSessionStart | no |
| `firstMsgInvite` | bool01 | `1` | RCC.07 CHAT firstMsgInvite | no |
| `reportMsgDisposition` | bool01 | `1` | RCC.07 CHAT reportMsgDisposition | no |
| `maxConcurrentSession` | int | `10` | RCC.07 CHAT maxConcurrentSession | no |
| `GroupChatFullStandFwd` | bool01 | `1` | RCC.07 CHAT GroupChatFullStandFwd | no |
| `ChatRevokeTimer` | int | `60` s | RCC.07 CHAT ChatRevokeTimer | no |

### `APPLICATION:ap2002/MESSAGING/FT`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `ftWarnSize` | int | `1024` KB | RCC.07 FT ftWarnSize | no |
| `MaxSizeFileTr` | int | `10240` KB | RCC.07 FT MaxSizeFileTr | yes |
| `ftHTTPCSURI` | chr | `https://ft.{ims_domain}/ft` | RCC.07 FT ftHTTPCSURI | yes |
| `ftHTTPDLURI` | chr | `https://ft.{ims_domain}/dl` | RCC.07 FT ftHTTPDLURI | no |
| `ftHTTPFallback` | bool01 | `1` | RCC.07 FT ftHTTPFallback | no |
| `ftStAndFwEnabled` | bool01 | `1` | RCC.07 FT ftStAndFwEnabled | no |
| `ftThumb` | bool01 | `1` | RCC.07 FT ftThumb | no |
| `ftAutAccept` | bool01 | `1` | RCC.07 FT ftAutAccept | no |
| `ftMax1ToManyRecipients` | int | `10` | RCC.07 FT ftMax1ToManyRecipients | no |
| `ftDefaultMech` | enum | `HTTP` | RCC.07 FT ftDefaultMech | yes |

### `APPLICATION:ap2002/MESSAGING/StandaloneMsg`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `SwitchOverSize` | int | `1300` byte | RCC.07 StandaloneMsg SwitchOverSize | no |
| `MaxSize1to1` | int | `8192` byte | RCC.07 StandaloneMsg MaxSize1to1 | no |
| `MaxSize1toMany` | int | `8192` byte | RCC.07 StandaloneMsg MaxSize1toMany | no |
| `ConfFctyURI` | chr | `sip:standalone-factory@{ims_domain}` | RCC.07 StandaloneMsg ConfFctyURI | no |

### `APPLICATION:ap2002/MESSAGING/MessageStore`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `MsgStoreUrl` | chr | `https://msgstore.{ims_domain}` | RCC.07 Message Store | no |
| `MsgStoreAuth` | enum | `1` | RCC.07 Message Store authentication | no |
| `MsgStoreNotifUrl` | chr | `https://msgstore.{ims_domain}/notify` | RCC.07 Message Store notification | no |

### `APPLICATION:ap2002/MESSAGING/Chatbot`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `ChatbotDirectory` | chr | `https://botdir.{ims_domain}` | RCC.07 Chatbot directory | no |
| `BotinfoFQDNRoot` | chr | `botplatform.{ims_domain}` | RCC.07 Chatbot botinfo | no |
| `ChatbotMsgTech` | enum | `2` | RCC.07 Chatbot messaging technology | no |
| `PrivacyDisable` | bool01 | `0` | RCC.07 Chatbot privacy | no |
| `SpamReportBotId` | chr | `sip:spamreport@{ims_domain}` | RCC.07 Chatbot spam reporting | no |

### `APPLICATION:ap2002/IM`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `imMsgTech` | enum | `1` | RCC.07 IM messaging technology | no |
| `imServiceAuth` | bool01 | `1` | RCC.07 IM service authorisation | no |

### `APPLICATION:ap2002/CAPDISCOVERY`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `capInfoExpiry` | int | `86400` s | RCC.07 CAPDISCOVERY capInfoExpiry | yes |
| `presenceDisc` | bool01 | `0` | RCC.07 CAPDISCOVERY presenceDisc | no |
| `defaultDisc` | bool01 | `1` | RCC.07 CAPDISCOVERY defaultDisc | no |
| `pollingPeriod` | int | `3600` s | RCC.07 CAPDISCOVERY pollingPeriod | no |
| `capDiscCommonStack` | bool01 | `1` | RCC.07 CAPDISCOVERY capDiscCommonStack | no |
| `msgCapValidity` | int | `604800` s | RCC.07 CAPDISCOVERY msgCapValidity | no |
| `nonRCSCapInfoExpiry` | int | `86400` s | RCC.07 CAPDISCOVERY nonRCSCapInfoExpiry | no |
| `disableInitialAddressBookScan` | bool01 | `0` | RCC.07 CAPDISCOVERY initial address book scan | no |

### `APPLICATION:ap2002/PRESENCE`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `usePresence` | bool01 | `0` | RCC.07 PRESENCE usePresence | no |
| `presencePrfl` | bool01 | `0` | RCC.07 PRESENCE presencePrfl | no |
| `AvailabilityAuth` | bool01 | `0` | RCC.07 PRESENCE AvailabilityAuth | no |
| `clientObjDataLimit` | int | `16384` byte | RCC.07 PRESENCE clientObjDataLimit | no |
| `contentServerUri` | chr | `https://presence.{ims_domain}/content` | RCC.07 PRESENCE contentServerUri | no |
| `sourceThrottlePublish` | int | `30` s | RCC.07 PRESENCE sourceThrottlePublish | no |
| `maxNumbOfSubscInPresList` | int | `100` | RCC.07 PRESENCE maxNumbOfSubscInPresList | no |
| `serviceAuthPolicy` | enum | `0` | RCC.07 PRESENCE serviceAuthPolicy | no |
| `publishTimer` | int | `3600` s | RCC.07 PRESENCE publishTimer | no |

### `APPLICATION:ap2002/XDMS`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `XCAPRootURI` | chr | `https://xcap.{ims_domain}/xcap-root` | RCC.07 XDMS XCAPRootURI | no |
| `XCAPAuthenticationUserName` | chr | `{impi}` | RCC.07 XDMS XCAPAuthenticationUserName | no |
| `XCAPAuthenticationType` | enum | `Digest` | RCC.07 XDMS XCAPAuthenticationType | no |
| `RevokeTimer` | int | `60` s | RCC.07 XDMS RevokeTimer | no |

### `APPLICATION:ap2002/OTHER`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `endUserConfReqId` | chr | `sip:eucr@{ims_domain}` | RCC.07 OTHER endUserConfReqId | yes |
| `deviceID` | chr | `{device_id}` | RCC.07 OTHER deviceID | no |
| `WarnSizeImageShare` | int | `1024` KB | RCC.07 OTHER WarnSizeImageShare | no |

### `APPLICATION:ap2002/OTHER/TRANSPORTPROTO`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `psSignalling` | enum | `SIPoTLS` | RCC.07 TRANSPORTPROTO psSignalling | yes |
| `psMedia` | enum | `MSRPoTLS` | RCC.07 TRANSPORTPROTO psMedia | yes |
| `psRTMedia` | enum | `SRTP` | RCC.07 TRANSPORTPROTO psRTMedia | yes |
| `psSignallingRoaming` | enum | `SIPoTLS` | RCC.07 TRANSPORTPROTO roaming variant | no |
| `psMediaRoaming` | enum | `MSRPoTLS` | RCC.07 TRANSPORTPROTO roaming variant | no |
| `psRTMediaRoaming` | enum | `SRTP` | RCC.07 TRANSPORTPROTO roaming variant | no |

### `APPLICATION:ap2002/APN`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `rcseOnlyAPN` | chr | _(omitted)_ | RCC.07 APN rcseOnlyAPN | no |
| `enableRcseSwitch` | bool01 | `1` | RCC.07 APN enableRcseSwitch | no |

### `APPLICATION:ap2002/ServiceProviderExt`

| Parameter | Type | Default | Reference | Cross-checked |
| --- | --- | --- | --- | --- |
| `acsHost` | chr | `{acs_host}` | Operator extension (informative) | no |

## OMA-DM management objects

### Device information

* URN: `urn:oma:mo:oma-dm-devinfo:1.0`
* Root: `./DevInfo`
* Reference: OMA-DM Standardized Objects (DevInfo)

| Node | Format | Owner | Default | Feature | Cross-checked |
| --- | --- | --- | --- | --- | --- |
| `./DevInfo` | node | device | _(none)_ | - | yes |
| `./DevInfo/DevId` | chr | device | _(none)_ | - | yes |
| `./DevInfo/Man` | chr | device | _(none)_ | - | yes |
| `./DevInfo/Mod` | chr | device | _(none)_ | - | yes |
| `./DevInfo/DmV` | chr | device | _(none)_ | - | yes |
| `./DevInfo/Lang` | chr | device | _(none)_ | - | yes |

### Device detail

* URN: `urn:oma:mo:oma-dm-devdetail:1.0`
* Root: `./DevDetail`
* Reference: OMA-DM Standardized Objects (DevDetail)

| Node | Format | Owner | Default | Feature | Cross-checked |
| --- | --- | --- | --- | --- | --- |
| `./DevDetail` | node | device | _(none)_ | - | yes |
| `./DevDetail/DevTyp` | chr | device | _(none)_ | - | yes |
| `./DevDetail/OEM` | chr | device | _(none)_ | - | yes |
| `./DevDetail/FwV` | chr | device | _(none)_ | - | yes |
| `./DevDetail/SwV` | chr | device | _(none)_ | - | yes |
| `./DevDetail/HwV` | chr | device | _(none)_ | - | yes |
| `./DevDetail/LrgObj` | bool | device | _(none)_ | - | yes |
| `./DevDetail/URI/MaxDepth` | int | device | _(none)_ | - | yes |
| `./DevDetail/URI/MaxTotLen` | int | device | _(none)_ | - | yes |
| `./DevDetail/URI/MaxSegLen` | int | device | _(none)_ | - | yes |

### 3GPP IMS / VoLTE settings

* URN: `urn:oma:mo:ext-3gpp-ims:1.0`
* Root: `./3GPP_IMS`
* Reference: 3GPP TS 24.167 IMS Management Object

| Node | Format | Owner | Default | Feature | Cross-checked |
| --- | --- | --- | --- | --- | --- |
| `./3GPP_IMS` | node | server | _(none)_ | - | yes |
| `./3GPP_IMS/1` | node | server | _(none)_ | - | yes |
| `./3GPP_IMS/1/Private_User_Identity` | chr | server | `{impi}` | - | yes |
| `./3GPP_IMS/1/Public_User_Identity_List/1/Public_User_Identity` | chr | server | `{impu}` | - | yes |
| `./3GPP_IMS/1/Home_network_domain_name` | chr | server | `{ims_domain}` | - | yes |
| `./3GPP_IMS/1/LBO_P-CSCF_Address/1/Address` | chr | server | `pcscf.{ims_domain}` | - | yes |
| `./3GPP_IMS/1/LBO_P-CSCF_Address/1/AddressType` | chr | server | `FQDN` | - | yes |
| `./3GPP_IMS/1/Authentication_Type` | chr | server | `AKA` | - | no |
| `./3GPP_IMS/1/PDP_ContextOperPref` | bool | server | `false` | - | no |
| `./3GPP_IMS/1/Timer_T1` | int | server | `2000` | - | no |
| `./3GPP_IMS/1/Timer_T2` | int | server | `16000` | - | no |
| `./3GPP_IMS/1/Timer_T4` | int | server | `17000` | - | no |
| `./3GPP_IMS/1/Resource_Allocation_Mode` | bool | server | `false` | - | no |
| `./3GPP_IMS/1/Phone_Context` | chr | server | `{ims_domain}` | - | no |
| `./3GPP_IMS/1/ICSI_List/1/ICSI` | chr | server | `urn:urn-7:3gpp-service.ims.icsi.mmtel` | volte | no |
| `./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN` | int | server | `3` | volte | no |
| `./3GPP_IMS/1/SMS_Over_IP_Networks_Indication` | bool | server | `true` | volte | no |
| `./3GPP_IMS/1/Keep_Alive_Enabled` | bool | server | `true` | volte | no |
| `./3GPP_IMS/1/Ext/RCS/rcsVolteSingleRegistration` | bool | server | `true` | volte | no |
| `./3GPP_IMS/1/Ext/VoLTE/AMRWB_Enabled` | bool | server | `true` | volte | no |
| `./3GPP_IMS/1/Ext/VoLTE/VideoCallEnabled` | bool | server | `true` | volte | no |
| `./3GPP_IMS/1/Ext/VoLTE/EmergencyRegistration` | bool | server | `true` | volte | no |

### RCS service switches (extension MO)

* URN: `urn:acs:mo:rcs-ext:1.0`
* Root: `./RCS`
* Reference: Extension MO mirroring GSMA RCC.07 SERVICES

| Node | Format | Owner | Default | Feature | Cross-checked |
| --- | --- | --- | --- | --- | --- |
| `./RCS` | node | server | _(none)_ | - | no |
| `./RCS/Services/ChatAuth` | bool | server | `true` | rcs | no |
| `./RCS/Services/GroupChatAuth` | bool | server | `true` | rcs | no |
| `./RCS/Services/FtAuth` | bool | server | `true` | rcs | no |
| `./RCS/Services/StandaloneMsgAuth` | bool | server | `true` | rcs | no |
| `./RCS/Services/ChatbotCommunicationAuth` | bool | server | `true` | rcs | no |
| `./RCS/Messaging/MaxSizeFileTr` | int | server | `10240` | rcs | no |
| `./RCS/Messaging/ConfFctyURI` | chr | server | `sip:conf-factory@{ims_domain}` | rcs | no |
| `./RCS/Config/ProvisioningVersion` | int | server | `{provisioning_version}` | rcs | no |

## Extending coverage

Adding a parameter is a data change: append an entry to `src/acs/catalog/omacp/base.yaml` (or a profile overlay) and regenerate this document. Adding a whole new managed service — VoLTE extensions, firmware update, a vendor MO — means dropping a YAML file into `src/acs/catalog/omadm/`. Neither requires touching server code.
