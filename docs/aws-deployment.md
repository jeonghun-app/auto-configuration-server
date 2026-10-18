# AWS deployment

## What gets created

`scripts/deploy.sh` deploys two CloudFormation stacks.

`rcs-acs-ecr` — the registry, kept separate so tearing the application down never
destroys the images it was running. Immutable tags, scan on push, lifecycle policy.

`rcs-acs-app` — everything else:

| Resource | Notes |
| --- | --- |
| VPC, 4 subnets, internet gateway, route table | `10.20.0.0/16` by default |
| DynamoDB gateway VPC endpoint | Free, and keeps table traffic off the internet |
| DynamoDB table + `gsi1` + TTL on `expires_at` | `PAY_PER_REQUEST`, encrypted, PITR on, `DeletionPolicy: Retain` |
| Secrets Manager: admin token, PII hash key | Generated, never in the template |
| Secrets Manager: SMPP password (only with `SmsProvider=smpp`) | Created with a random placeholder; the operator-issued value is put in after the first deploy |
| NAT gateway + Elastic IP, one or one per AZ (only with `FixedEgress`) | Private route tables for the app subnets and a free S3 gateway endpoint; see [Fixed egress address](#fixed-egress-address) |
| ECS Fargate cluster, task definition, service | Read-only root filesystem, non-root user |
| Application Load Balancer + target group | Health check on `/healthz` |
| HTTPS listener (with a certificate) | `ELBSecurityPolicy-TLS13-1-2-2021-06`; HTTP redirects to HTTPS |
| Application Auto Scaling | Target tracking on `ALBRequestCountPerTarget` |
| CloudWatch log group | Retention configurable, default 30 days |
| 3 CloudWatch alarms | Unhealthy targets, 5xx rate, OTP send volume |

## Deploying

```bash
scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --certificate-arn arn:aws:acm:ap-northeast-2:123456789012:certificate/abc123 \
  --region ap-northeast-2 \
  --environment prod \
  --desired-count 2 \
  --sms-provider eum \
  --sms-origination arn:aws:sms-voice:ap-northeast-2:123456789012:phone-number/...
```

Idempotent. Re-running with the same tag reuses the image in ECR, since tags are
immutable and a re-push would fail.

Every run passes every stack parameter the script knows, so a flag left off a
later run resets that parameter to the script's default. Repeat the full set each
time.

### Port-addressed OTP through an operator SMSC

AWS SMS cannot send the port-addressed binary SMS RCC.14 uses for silent OTP
delivery. An operator SMSC over SMPP 3.4 can:

```bash
scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --certificate-arn arn:aws:acm:ap-northeast-2:123456789012:certificate/abc123 \
  --sms-provider smpp \
  --smpp-host smsc.operator.example \
  --smpp-port 3550 \
  --smpp-system-id acs01 \
  --smpp-cidr 198.51.100.0/28,203.0.113.8/29 \
  --fixed-egress per-az \
  --smpp-tls-ca-secret-arn arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rcs-acs/smsc-ca-AbC12z
```

The template refuses `smpp` without a host, `system_id` and SMSC range. The tasks
gain one egress rule per `--smpp-cidr` range (comma-separated, at most four, so an
SMSC pair fits), on `--smpp-port` only, and lose the AWS SMS permissions. TLS is on
by default because SMPP sends the password in clear; pass `--smpp-tls false` only
when the SMSC is reached over a private link.

The rest of the session is set with `--smpp-system-type`, `--smpp-source-addr`,
`--smpp-source-ton`, `--smpp-source-npi`, `--smpp-dest-ton`, `--smpp-dest-npi` and
`--smpp-timeout`. Their defaults are the application's own (an empty
`system_type`, the alphanumeric sender id `RCS` as TON 5 / NPI 0, an international
E.164 destination as TON 1 / NPI 1, 10 seconds), so a stack deployed without them
behaves as before. Take the values from the operator's SMSC account sheet: an
SMSC refuses `submit_sm`, typically with `ESME_RINVSRCADR`, when the TON or NPI
does not describe the source address.

The password is never a flag. The stack creates the `SmppPasswordSecretArn` secret
with a random placeholder, so every OTP answers `503` until the operator-issued
value is put into it and the service is redeployed — see
[runbook.md](runbook.md#setting-or-rotating-the-smpp-password). This path has been
tested only against a fake SMSC; see [limitations.md](limitations.md).

### An SMSC certificate from an operator private CA

The root filesystem is read-only, so the CA bundle is not a file. Put it, as plain
PEM text, into a Secrets Manager secret you create, and pass its complete ARN (the
one `create-secret` prints, with the random six-character suffix) as
`--smpp-tls-ca-secret-arn`:

```bash
aws secretsmanager create-secret --region ap-northeast-2 \
  --name rcs-acs/smsc-ca --description "Operator SMSC CA bundle" \
  --secret-string file://operator-ca.pem
```

ECS injects it as `ACS_SMPP_TLS_CA_PEM`, and it then replaces the public CAs for
the SMSC connection only. The execution role gains `secretsmanager:GetSecretValue`
on that one ARN. A partial ARN or a JSON-key reference is refused by the template,
because it would not match the grant and no task could start. If the secret uses a
customer-managed KMS key, that key's policy must also allow the execution role
`kms:Decrypt`; the default `aws/secretsmanager` key needs nothing.

The tasks refuse to start, in every environment, if the bundle cannot be loaded —
for example a key/value secret made in the console, escaped `\n` newlines, or a
private key pasted in with the certificate. The error names the problem, never
the content; the circuit breaker then keeps the running tasks (on a first deploy,
the stack rolls back). Rotating it:
[runbook.md](runbook.md#setting-or-rotating-the-smsc-ca-bundle).

### Fixed egress address

Operator SMSCs usually allow-list the source address. By default each task
connects from its own public IP, which changes whenever the task is replaced.
`--fixed-egress` moves the tasks behind NAT gateways with Elastic IPs:

| `FixedEgress` | Addresses to allow-list | If one AZ fails | Fixed cost |
| --- | --- | --- | --- |
| `none` (default) | none fixed | the other AZ carries on | nothing extra |
| `single` | one | **all egress stops**: SMS, logs, Secrets Manager and image pulls in both AZs go through the NAT gateway in AZ A | one NAT gateway, one address |
| `per-az` | two | the other AZ carries on | two NAT gateways, two addresses |

Use `per-az` in production: with `single` an outage of one AZ stops OTP delivery
and keeps replacement tasks from starting in either AZ. `single` suits staging or
a cost-bound trial. The addresses are the `EgressIps` stack output, printed at the
end of `deploy.sh`; they carry every outbound connection, not only SMPP.

The app subnets are moved onto private route tables whose default route is the
NAT gateway, and the tasks start without public IPs (`AssignPublicIp: DISABLED`).
DynamoDB still goes through its gateway endpoint, and a free S3 gateway endpoint
is added so image layers are not billed per GB by the NAT gateway.

Turning it on is a rolling deployment: the subnets are switched only once the NAT
route exists, and the service only after that, so running tasks keep their egress
throughout. **Turning it off, or
deleting the stack, releases the Elastic IPs**, and a released address cannot be
had back; agree a new allow-list with the operator first. Turning it off also
leaves the running tasks, which have no public IP, without egress until the
rolling deployment replaces them — a few minutes in which OTP sends fail with
`503`. Do it in a quiet period. Moving between `single` and `per-az` keeps the
AZ A address.

### Why `--allowed-cidr` is mandatory

The script refuses `0.0.0.0/0`. An RCC.14 request carries IMSI, IMEI and MSISDN,
and the OTP endpoint causes SMS the operator pays for. Until you intend handsets
to reach the ACS, keep it closed. When you do open it, put a WAF rate-based rule in
front first.

### Why you want a certificate

Without `--certificate-arn` the ALB serves plain HTTP and the script warns. That is
fine for a demo. It is not fine for handsets: the query string contains subscriber
identifiers, the OTP and the provisioning token, and the response body contains IMS
credentials.

For real devices the certificate must cover
`config.rcs.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org` — the name the client derives
from the SIM. Only the operator can publish in that zone; the deploy output prints
the CNAME to create.

## Verification

The last step of `deploy.sh` runs `scripts/verify_stack.py` against the live load
balancer: it seeds a demo subscriber through the admin API, drives the full RCC.14
flow, and then runs an OMA-DM session using the password harvested from the `w7`
characteristic. 32 checks. Non-zero exit fails the deployment.

Standalone:

```bash
BASE_URL=$(aws cloudformation describe-stacks --stack-name rcs-acs-app \
  --query "Stacks[0].Outputs[?OutputKey=='BaseUrl'].OutputValue" --output text)
SECRET=$(aws cloudformation describe-stacks --stack-name rcs-acs-app \
  --query "Stacks[0].Outputs[?OutputKey=='AdminTokenSecretArn'].OutputValue" --output text)
ADMIN=$(aws secretsmanager get-secret-value --secret-id "$SECRET" \
  --query SecretString --output text)

ACS_BASE_URL="$BASE_URL" ACS_ADMIN_TOKEN="$ADMIN" python scripts/verify_stack.py
```

## Cost

Rough monthly figures for `ap-northeast-2` at low volume; check the AWS pricing
calculator for anything you will be billed for.

| Item | Driver |
| --- | --- |
| ALB | Hourly plus LCU — the largest fixed cost |
| Fargate | 2 tasks × 0.5 vCPU / 1 GB, billed per second |
| DynamoDB | On-demand; a provisioning request is a handful of requests |
| CloudWatch Logs | Ingestion and storage; retention is the lever |
| Secrets Manager | Per secret per month, two secrets (three with SMPP, four with a CA bundle) |
| NAT gateway (only with `FixedEgress`) | Hourly per gateway plus per GB processed; see below |
| Public IPv4 addresses | Hourly per address: one per task by default, the Elastic IPs instead with `FixedEgress` |
| ECR | Storage for retained images |
| SMS | Per message, and this is the one an attacker can drive |

To reduce cost: `--desired-count 1` (accepting that a deployment is then a brief
outage), shorter log retention, and a lower `MaxCount`.

No NAT gateway is created unless `FixedEgress` asks for one. In `ap-northeast-2`
at the time of writing a NAT gateway is about USD 0.059 an hour (about USD 43 a
month) plus USD 0.059 per GB processed, and a public IPv4 address, Elastic or not,
is USD 0.005 an hour (about USD 3.65 a month). So `single` adds about USD 47 a
month and `per-az` about USD 94, before traffic, less USD 3.65 per task whose
public IP goes away. OTP and AWS API traffic is small, and DynamoDB and image
layers bypass the NAT gateway, so the per-GB charge is mostly CloudWatch Logs. With `single`, traffic from AZ B also pays inter-AZ transfer to reach
AZ A. Check the AWS pricing pages before relying on these figures.

## Network hardening variants

**Default: tasks in public subnets with public IPs.** A public IP is required to
pull from ECR without a NAT gateway. The task security group has **no inbound rule
except from the load balancer's security group**, so the tasks are not reachable
from the internet. Egress is restricted to TCP 443, plus the SMPP port to the SMSC
range when `SmsProvider=smpp`.

**Hardened: tasks in private subnets.** Two options, both costing more:

1. *NAT gateway* — built in: `FixedEgress=single` or `per-az`, see
   [Fixed egress address](#fixed-egress-address). It also gives the tasks fixed
   source addresses, which an SMSC allow-list needs.
2. *Interface VPC endpoints* — no NAT, and not in the template. Needs endpoints
   for `ecr.api`, `ecr.dkr`, `logs`, `secretsmanager` and `sms-voice`, plus the S3
   gateway endpoint for ECR layers. The DynamoDB gateway endpoint already exists. Each interface endpoint
   carries an hourly charge per AZ, so with 5 endpoints × 2 AZs this is usually
   more expensive than the NAT unless traffic is high.

Pick option 2 if policy forbids a route to the internet; otherwise option 1.

## ALB access logs are off by default

`EnableAlbAccessLogs=false`. An ALB access log line contains the full request line,
and an RCC.14 request line contains IMSI, IMEI, MSISDN, OTP and token. Enabling it
creates an S3 bucket of subscriber-identifying data.

When it is set to `true` the template creates a bucket that is encrypted, blocks
public access, and expires objects after 14 days. Treat that bucket as regulated
subscriber data.

## Observability

Logs are single-line JSON on stdout, shipped by the `awslogs` driver.
Subscriber identifiers are masked (`ACS_PII_LOG_MODE=mask`) or HMAC-pseudonymised
(`hash`, using the generated Secrets Manager key). uvicorn's access log is
disabled: its log line would contain the whole query string.

Metrics are emitted as CloudWatch Embedded Metric Format documents on stdout, so
CloudWatch Logs extracts them automatically — no `PutMetricData`, no extra IAM
permission, no throttling, no latency on the request path.

Namespace `RcsAcs`, dimensions `Environment` and `Outcome` only. Subscriber
identifiers are never dimensions: that would be both a leak and an unbounded bill.

Metrics emitted: `ConfigServed`, `ConfigUnchanged`, `ConfigDisabled`, `OtpSent`,
`OtpPendingReuse`, `OtpRateLimited`, `OtpDeliveryUnsupported`, `OtpDeliveryFailed`,
`OtpStoreContention`, `Rejected403`, `Challenge511`, `GbaChallenge`,
`MalformedRequest`, `ConfigBytes`, and the `Dm*` family, including
`DmEncodingError`.

The stack's three alarms do not cover `OtpDeliveryFailed`, `OtpStoreContention` or
`DmEncodingError`. Each is answered with a `5xx`, so the 5xx-rate alarm sees a
sustained burst; add a dedicated alarm if one of them should page on its own.
Alarms for them in the stack are tracked in #31.
[runbook.md](runbook.md#otp-delivery-failing) says what each means.

## IAM

The task role is scoped to:

- the one DynamoDB table and its one index — `GetItem`, `PutItem`, `UpdateItem`,
  `DeleteItem`, `Query`, `DescribeTable`;
- `sms-voice:SendTextMessage` and `sns:Publish`. Sending to a phone number has no
  resource ARN to scope to, so the permission is narrowed to those two actions,
  and it is not granted at all with `SmsProvider=smpp`.

OTP issue uses `TransactWriteItems`, which needs no action of its own: it is
authorised by `PutItem` on the table.

The execution role adds only `secretsmanager:GetSecretValue` on the secrets this
stack creates (two, or three with SMPP), and on the one SMSC CA bundle secret named
by `SmppTlsCaSecretArn` if it is set, on top of the managed ECS execution policy.

## Tearing down

```bash
scripts/teardown.sh --stack-prefix rcs-acs --region ap-northeast-2
```

Prompts for the stack name. Deletes the ALB, ECS service and VPC. **Retained on
purpose**: the DynamoDB table (subscriber records, tokens, device inventory), the
Secrets Manager secrets, the ECR repository and any access log bucket. The script
prints the exact commands to remove each, which are irreversible.

## Deployment checklist

- [ ] `make check` passes locally
- [ ] `--allowed-cidr` is your address range, not `0.0.0.0/0`
- [ ] ACM certificate issued and validated
- [ ] SMS origination identity provisioned, and out of the SMS sandbox — or, with
      `smpp`, the operator's SMSC account, address ranges, password, TON/NPI and,
      for a private CA, the CA bundle in hand
- [ ] With an SMSC allow-list, `--fixed-egress` chosen and the `EgressIps`
      output given to the operator
- [ ] SMS spending limit set on the account
- [ ] WAF rate-based rule in front if the service is internet-facing
- [ ] Alarm actions wired to an SNS topic someone reads
- [ ] `ACS_PII_LOG_MODE` decided, and `hash` mode has its secret
- [ ] Log retention agreed with whoever owns data retention policy
- [ ] `scripts/verify_stack.py` green against the deployed URL
