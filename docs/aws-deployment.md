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
| Secrets Manager | Per secret per month, two secrets |
| ECR | Storage for retained images |
| SMS | Per message, and this is the one an attacker can drive |

To reduce cost: `--desired-count 1` (accepting that a deployment is then a brief
outage), shorter log retention, and a lower `MaxCount`.

No NAT gateway is created, which is a deliberate saving — see below.

## Network hardening variants

**Default: tasks in public subnets with public IPs.** A public IP is required to
pull from ECR without a NAT gateway. The task security group has **no inbound rule
except from the load balancer's security group**, so the tasks are not reachable
from the internet. Egress is restricted to TCP 443.

**Hardened: tasks in private subnets.** Two options, both costing more:

1. *NAT gateway* — simplest. Add a NAT gateway in a public subnet, a private route
   table, and set `AssignPublicIp: DISABLED`. Roughly one hourly charge plus data
   processing.
2. *Interface VPC endpoints* — no NAT. Needs endpoints for `ecr.api`, `ecr.dkr`,
   `logs`, `secretsmanager` and `sms-voice`, plus the S3 gateway endpoint for ECR
   layers. The DynamoDB gateway endpoint already exists. Each interface endpoint
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
`OtpPendingReuse`, `OtpRateLimited`, `OtpDeliveryUnsupported`, `Rejected403`,
`Challenge511`, `GbaChallenge`, `MalformedRequest`, `ConfigBytes`, and the `Dm*`
family.

## IAM

The task role is scoped to:

- the one DynamoDB table and its one index — `GetItem`, `PutItem`, `UpdateItem`,
  `DeleteItem`, `Query`, `DescribeTable`;
- `sms-voice:SendTextMessage` and `sns:Publish`. Sending to a phone number has no
  resource ARN to scope to, so the permission is narrowed to those two actions.

The execution role adds only `secretsmanager:GetSecretValue` on the two secrets
this stack creates, on top of the managed ECS execution policy.

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
- [ ] SMS origination identity provisioned, and out of the SMS sandbox
- [ ] SMS spending limit set on the account
- [ ] WAF rate-based rule in front if the service is internet-facing
- [ ] Alarm actions wired to an SNS topic someone reads
- [ ] `ACS_PII_LOG_MODE` decided, and `hash` mode has its secret
- [ ] Log retention agreed with whoever owns data retention policy
- [ ] `scripts/verify_stack.py` green against the deployed URL
