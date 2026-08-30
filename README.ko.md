# GSMA RCS 자동 설정 서버 (ACS)

[![CI](https://github.com/jeonghun-app/auto-configuration-server/actions/workflows/ci.yml/badge.svg)](https://github.com/jeonghun-app/auto-configuration-server/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

GSMA RCS용 컨테이너 기반 자동 설정 서버(ACS)이며, AWS에 명령 한 번으로 배포됩니다.

영문 문서가 기준입니다: [README.md](README.md). 이 문서는 한국어 요약입니다.

하나의 서비스에 두 개의 평면(plane)이 있습니다.

| 평면 | 프로토콜 | 역할 |
| --- | --- | --- |
| **RCS 설정** | GSMA RCC.14 HTTP 플로우, OMA Client Provisioning (`wap-provisioningdoc` 1.1) | RCS 클라이언트 프로비저닝: IMS 식별자, 메시징, 파일 전송, 능력 발견, 챗봇 |
| **단말 관리** | OMA-DM, SyncML DM 1.2 | 부트스트랩 이후 VoLTE 설정과 단말 인벤토리 관리. YAML 관리 객체(MO)만 추가하면 확장됨 |

두 평면은 연결되어 있습니다. OMA-CP 문서에 OMA-DM 계정을 부트스트랩하는 `w7`
characteristic이 포함되므로, RCS 프로비저닝을 받은 단말은 곧바로 DM 세션으로
관리할 수 있습니다. 이 연결은 `scripts/verify_stack.py`가 종단간으로 검증합니다.

---

## 빠른 시작

```bash
make install                 # .venv 생성 및 의존성 설치
make test                    # 테스트 337개
make docker-run              # 컨테이너 빌드 후 :8080 에서 실행
make verify                  # 두 평면의 종단간 검증
```

정상 결과:

```
18 checks passed, 0 failed     # RCS 자동 설정 (RCC.14 / OMA-CP)
14 checks passed, 0 failed     # OMA-DM 단말 관리 (SyncML DM 1.2)
RESULT: PASS — both planes behave as specified
```

운영과 동일한 DynamoDB 코드 경로를 쓰는 로컬 전체 스택:

```bash
make up                      # ACS + amazon/dynamodb-local
make verify
make down
```

## AWS 배포

모든 백엔드 구성요소가 AWS 관리형 서비스입니다.

| 항목 | 서비스 |
| --- | --- |
| 컴퓨팅 | ECS Fargate |
| 인그레스 / TLS | Application Load Balancer + ACM |
| 상태 저장 | DynamoDB (단일 테이블, OTP·DM 세션은 TTL로 자동 만료) |
| 비밀 | Secrets Manager (관리 토큰, PII 해시 키) |
| 로그 | CloudWatch Logs (구조화 JSON) |
| 지표 | CloudWatch, stdout의 EMF 형식 — `PutMetricData` 호출 없음 |
| SMS | AWS End User Messaging SMS 또는 Amazon SNS |
| 레지스트리 | ECR (태그 불변, 푸시 시 스캔) |
| 인프라 | CloudFormation |

```bash
scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --certificate-arn arn:aws:acm:ap-northeast-2:123456789012:certificate/abc123
```

이미지 빌드 → ECR 푸시 → 두 스택 배포 → 헬스 대기 → 실제 로드밸런서 대상
종단간 검증까지 수행합니다. `scripts/teardown.sh`로 제거하며, DynamoDB 테이블과
비밀, 이미지는 **의도적으로 보존**합니다.

`--allowed-cidr`는 필수이고 `0.0.0.0/0`은 거부합니다. ACS는 쿼리 문자열로 IMSI,
IMEI, MSISDN을 받고, 악용 시 실제 비용이 발생하는 OTP 엔드포인트를 노출하므로
단말이 접근해야 할 시점까지는 전체 인터넷에 열지 않습니다. 자세한 내용과
프라이빗 서브넷 강화 방식은 [docs/aws-deployment.md](docs/aws-deployment.md)를
참고하십시오.

## RCC.14 플로우

```
        클라이언트                                ACS
          │  GET /config?vers=0&IMSI=…&IMEI=…      │
          ├───────────────────────────────────────►│  신원 미확인
          │                                        │  → OTP 생성, SMS 발송
          │◄───────────────────────────────────────┤  200 OK, Content-Length: 0
          │                                        │
          │  GET /config?vers=0&IMSI=…&OTP=142760  │
          ├───────────────────────────────────────►│  OTP 검증 후 소진
          │◄───────────────────────────────────────┤  200 OK + wap-provisioningdoc
          │                                        │     VERS/version = 1
          │  (validity 만료)                        │     TOKEN, ap2001, ap2002, w7
          │  GET /config?vers=1&token=…            │
          ├───────────────────────────────────────►│  이미 최신 버전 보유
          │◄───────────────────────────────────────┤  200 OK + VERS 만 포함
```

상태 코드:

| 코드 | 의미 |
| --- | --- |
| `200` + XML 본문 | 설정 전달 |
| `200` + `Content-Length: 0` | OTP 발송됨. 동일 요청에 `OTP=`만 추가해 재요청 |
| `200` + `VERS`만 | 클라이언트가 이미 최신 리비전 보유 |
| `401` + `WWW-Authenticate: Digest … AKAv1-MD5` | GBA 부트스트랩 챌린지 (기본 비활성) |
| `403` | 가입자는 알지만 RCS 권한 없음 |
| `429` + `Retry-After` | OTP 속도 제한 |
| `503` + `Retry-After` | 일시적으로 제공 불가 |
| `511` | 가입자 식별 불가. 이동망으로 재시도하거나 MSISDN 입력 플로우 사용 |

설정 버전은 단순 리비전이 아니라 **동작 지시**입니다.

| `VERS/version` | 클라이언트 동작 |
| --- | --- |
| `> 0` | 유효 리비전. 저장 후 적용 |
| `0` | 설정 무효, RCS 끔. 트리거 발생 시에만 재요청 |
| `-1` | 비활성화, 설정 삭제, 다음 트리거에 재요청 |
| `-2` | 비활성화, 설정 삭제, 공장 초기화나 SIM 교체까지 재요청 금지 |
| `-3` | 휴면. 설정 유지, `validity` 후 재시도 |
| `-4` | 프로비저닝 영구 차단 |

`-1`~`-4`의 해석은 RCC.14 릴리스와 벤더마다 다르고 ACS에서 가장 흔히 잘못
구현되는 지점이므로, 전체 매핑을
[`src/acs/protocol/vers.py`](src/acs/protocol/vers.py)의 검토 가능한 표 한 곳에
모아 두었습니다.

## 규격 커버리지를 정직하게 표기

프로비저닝 파라미터는 **코드가 아니라 YAML로 선언**합니다.

```yaml
- path: APPLICATION:ap2002/MESSAGING/FT
  parm: MaxSizeFileTr
  type: int
  unit: KB
  default: "10240"
  spec: RCC.07 A.1.4 FT
  verified: false
```

[`docs/spec-coverage.md`](docs/spec-coverage.md)는 이 파일들에서 생성되므로,
증명할 수 없는 준수 주장을 하지 않고 실제 커버리지를 그대로 밝힙니다.

| 대상 | 항목 수 | 고정된 규격판과 교차 확인 |
| --- | --- | --- |
| OMA-CP 파라미터 | 116 | 25 |
| OMA-DM 노드 | 47 | 23 |
| 버전 의미 행 | 5 | 1 |

`verified: false`는 RCC.07/RCC.14의 공개된 설명과 현장에 널리 배포된 설정을
근거로 구현했다는 뜻입니다. 구조적으로 올바르고 타입이 지정되어 있으며 테스트도
되지만, 본 프로젝트는 **GSMA 인증을 주장하지 않습니다**. RCC.07과 RCC.14는
라이선스 문서이므로, 보유한 판본을 [`docs/scope.md`](docs/scope.md)에 명시하고
확인한 항목을 `verified: true`로 바꾸십시오. 수정은 YAML 한 줄이면 됩니다.

파라미터, 사업자 프로파일, 새 관리 서비스 추가는 모두 데이터 변경입니다.
`GET /admin/coverage`가 런타임에 같은 수치를 보고합니다.

## OMA-DM 및 VoLTE 확장

DM 평면은 `src/acs/catalog/omadm/`의 관리 객체 정의로 동작합니다.

| 파일 | URN | 내용 |
| --- | --- | --- |
| `01-devinfo.yaml` | `urn:oma:mo:oma-dm-devinfo:1.0` | 단말 식별 정보 |
| `02-devdetail.yaml` | `urn:oma:mo:oma-dm-devdetail:1.0` | 펌웨어, 소프트웨어, URI 제한 |
| `03-3gpp-ims.yaml` | `urn:oma:mo:ext-3gpp-ims:1.0` | IMS + VoLTE (음성 도메인 우선순위, SMSoIP, ICSI, AMR-WB, ViLTE, 단일 등록) |
| `04-rcs-ext.yaml` | `urn:acs:mo:rcs-ext:1.0` | 전체 재프로비저닝 없이 바꿀 수 있는 RCS 서비스 스위치 |

각 노드는 소유자를 선언합니다. `source: device` 노드는 `Get`으로 읽어 단말
인벤토리를 구성하고, `source: server` 노드는 `Replace`로 내려보냅니다. 펌웨어
업데이트(FUMO), 벤더 MO, VoLTE 파라미터 추가는 YAML 파일 추가만으로 끝나며 서버
코드는 바꾸지 않습니다. `GET /dm/mo`가 적재된 목록을 보여줍니다.

자세한 내용: [docs/oma-dm.md](docs/oma-dm.md).

## 보안 및 개인정보

이 서비스는 IMSI, IMEI, MSISDN을 취급합니다. 그에 따른 설계 결정:

- **로그에 PII 없음.** 식별자는 마스킹 또는 HMAC 가명화하며, uvicorn 액세스
  로그는 비활성화합니다(로그 한 줄에 쿼리 문자열 전체가 담김). 원문 식별자가
  출력에 나타나지 않음을 테스트로 검증합니다.
- **지표 차원에 PII 없음.** 무한 카디널리티는 정보 유출이자 비용 폭증입니다.
- **ALB 액세스 로그 기본 비활성.** RCC.14 요청 라인에는 IMSI, IMEI, MSISDN, OTP,
  토큰이 들어 있어 활성화하면 가입자 데이터 버킷이 만들어집니다.
- **OTP 악용 통제.** MSISDN별 쿨다운, 일일 상한, 검증 시도 제한, 1회용,
  상수 시간 비교, 발송량 CloudWatch 알람.
- **토큰은 해시 저장**, IMSI·IMEI에 바인딩, 개별 폐기 가능.
- **가입자 열거 불가.** 알려진 신원과 미지의 신원이 동일한 응답 형태를 받습니다.
- **XXE 차단.** 두 XML 파서 모두. 문서는 문자열 템플릿이 아니라 `lxml`로 생성.
- **컨테이너 강화.** 비루트 UID 10001, 읽기 전용 루트 파일시스템, 베이스 이미지
  다이제스트 고정, 서비스 사용자에게 셸 없음.

## 이 프로젝트가 할 수 없는 것

| 항목 | 이유 | 대신 제공하는 것 |
| --- | --- | --- |
| 포트 지정(무음) OTP SMS | UDH를 지원하는 사업자 SMSC(SMPP)가 필요. AWS SMS 서비스로는 불가 | 인터페이스가 `SMS_port`를 끝까지 전달하고, AWS 프로바이더는 강등 대신 거부하며, `SmppSmsSender.build_udh()`가 헤더를 구현 |
| 실제 GBA / AKA | USIM, Ub·Zn 상의 BSF, HSS 필요 | HTTP 챌린지/응답 형태, `BsfClient` 포트, 결정적 모의 구현 |
| 실제 사업자 헤더 강화 | 사업자 패킷 게이트웨이 필요 | 신뢰 프록시로 제한된 헤더, 기본 비활성 |
| `config.rcs.mncXXX.mccYYY.pub.3gppnetwork.org` 로 접근 가능 | 해당 DNS 존은 사업자와 GSMA가 관리 | 배포 출력이 생성해야 할 CNAME을 안내 |
| 실제 단말 | CI에서 사용 불가 | 규격 위반 시 빌드를 실패시키는 프로토콜 정확 시뮬레이터 2종 |

전체 목록: [docs/limitations.md](docs/limitations.md).

## 검증

```bash
make check      # lint + mypy strict + 커버리지 게이트 테스트 + cfn-lint
                # + shellcheck + 규격 커버리지 최신성
```

이 저장소에서 실제 측정한 결과:

| 항목 | 결과 |
| --- | --- |
| `pytest` | 337 통과 |
| 커버리지 | 93% |
| `mypy --strict` | 소스 46개 파일 이상 없음 |
| `ruff` (lint + format) | 이상 없음 |
| `cfn-lint` | 이상 없음 |
| 컨테이너 | 빌드 성공, UID 10001로 실행, `/healthz` 200 |
| 종단간 (인메모리 백엔드) | 32개 검사 통과 |
| 종단간 (DynamoDB 백엔드, 컨테이너) | 32개 검사 통과 |

## 라이선스

Apache-2.0. [LICENSE](LICENSE) 참고.

RCC.07, RCC.14 및 OMA 규격은 GSMA와 Open Mobile Alliance의 자산입니다. 이
저장소에는 규격 본문이 포함되어 있지 않습니다.
