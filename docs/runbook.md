# Runbook

## Reading the environment

```bash
STACK=rcs-acs-app
REGION=ap-northeast-2

BASE_URL=$(aws cloudformation describe-stacks --region $REGION --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='BaseUrl'].OutputValue" --output text)
SECRET=$(aws cloudformation describe-stacks --region $REGION --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='AdminTokenSecretArn'].OutputValue" --output text)
ADMIN=$(aws secretsmanager get-secret-value --region $REGION --secret-id "$SECRET" \
  --query SecretString --output text)
```

## Health

```bash
curl -s "$BASE_URL/healthz"    # liveness, makes no AWS calls
curl -s "$BASE_URL/readyz"     # store reachability + catalogue integrity
```

`/readyz` returning `503` while `/healthz` is `200` means the process is alive but
a dependency is not. Check the `checks.store` field first.

## A subscriber says RCS will not activate

1. **Does the subscriber exist and is it entitled?**

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN" \
     "$BASE_URL/admin/subscribers/001010000000001" | jq
   ```

   `entitled: false` produces `403`. `forced_vers` set to a negative value means an
   operator disabled them deliberately.

2. **Reproduce the client's request.**

   ```bash
   python tools/rcs_client_sim.py --base-url "$BASE_URL" \
     --imsi 001010000000001 --imei 356938035643809 --scenario full
   ```

   The simulator names the failing check.

3. **Read the logs for that request.** Identifiers are masked, so search on the
   masked tail:

   ```bash
   aws logs filter-log-events --region $REGION \
     --log-group-name /aws/ecs/rcs-acs-app \
     --filter-pattern '{ $.imsi = "*0001" }' --max-items 20
   ```

Common outcomes and their meaning:

| `outcome` in the log | Meaning | Action |
| --- | --- | --- |
| `Challenge511` | Identity unresolved | Check that the IMSI is registered; check `detail` |
| `Rejected403` | Not entitled, or IMEI not allowlisted | Check `entitled` and `imei_allowlist` |
| `OtpSent` | Challenge issued | The client should repeat the request with `OTP=` |
| `OtpRateLimited` | Cooldown or daily cap | Wait, or raise the cap |
| `OtpDeliveryUnsupported` | Client asked for port-addressed SMS and the provider is `eum` or `sns` | Expected on AWS; needs `smpp`, see limitations |
| `OtpDeliveryFailed` | The SMS provider or SMSC refused or failed the send; `503`, `Retry-After: 60`, challenge deleted | See [OTP delivery failing](#otp-delivery-failing) |
| `OtpStoreContention` | OTP issue lost five conditional-write rounds; `503`, `Retry-After: 5` | Occasional: concurrent requests for one MSISDN. Sustained: see [OTP delivery failing](#otp-delivery-failing) |
| `ConfigUnchanged` | Client already current | Not a fault |
| `ConfigDisabled` | A forced negative `VERS` was served | Deliberate; check `forced_vers` |

## Forcing a re-provision

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" \
  "$BASE_URL/admin/subscribers/001010000000001/invalidate"
```

Bumps the configuration version, so the next request gets the full document
instead of a `VERS`-only answer. Clients pick it up when their `validity` expires,
so a fleet-wide change propagates over `ACS_PROVISIONING_VALIDITY_SECONDS`
(default 24 h), not instantly.

## Disabling a subscriber

```bash
# Disable, wipe the client's configuration, do not re-query.
curl -s -X POST -H "Authorization: Bearer $ADMIN" \
  "$BASE_URL/admin/subscribers/001010000000001/disable?vers=-2"
```

Valid values: `0`, `-1`, `-2`, `-3`, `-4` — see [protocol.md](protocol.md). This
also revokes the subscriber's tokens.

`-2` and `-4` mean the client will not ask again until a factory reset or SIM swap.
**Do not use them for a temporary problem.** For "come back later" use `-3`
(dormant) or `0`.

Re-enable:

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" \
  "$BASE_URL/admin/subscribers/001010000000001/enable"
```

## Suspected token compromise

```bash
# One subscriber
curl -s -X POST -H "Authorization: Bearer $ADMIN" \
  "$BASE_URL/admin/subscribers/001010000000001/revoke-tokens"
```

Every holder then gets `511` and must re-authenticate by OTP. There is no
bulk revoke-all endpoint on purpose: revoking the whole fleet would trigger an OTP
storm and a large SMS bill. If it is genuinely necessary, do it in batches and
watch the `OtpSent` metric.

## OTP volume alarm fired

1. Look at the rate: metric `OtpSent`, namespace `RcsAcs`.
2. Check the source distribution in the ALB metrics or access logs (if enabled).
3. Contain: narrow `AllowedCidr` on the stack, or attach a WAF rate-based rule.
4. Reduce the limits without a deployment by updating the task definition
   environment: `ACS_OTP_MAX_SENDS_PER_MSISDN_PER_DAY`,
   `ACS_OTP_RESEND_COOLDOWN_SECONDS`.
5. Confirm the account SMS spending limit is set.

## OTP delivery failing

`OtpDeliveryFailed` rising means OTPs are not leaving. Clients are told to retry
after 60 seconds, and each failure deletes its challenge, so nothing is left
waiting for a code that was never sent.

```bash
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/ecs/rcs-acs-app \
  --filter-pattern '"otp delivery failed"' --max-items 20
```

The `error` field names the cause without the password, the message or the
number. With `smpp`, in order:

1. **Every send fails right after a deploy** — the password secret still holds the
   stack's random placeholder. Set it as under
   [Setting or rotating the SMPP password](#setting-or-rotating-the-smpp-password).
2. **Connection refused or timeout** — the SMSC address is outside `SmppSmscCidr`,
   the port is wrong, or the operator has not allowed the task's public address.
   The tasks may open `SmppPort` to that range and nothing else. Task public IPs
   change with every replacement, and the stack has no fixed-egress option yet
   (#31), so an SMSC that allow-lists source addresses cannot be satisfied
   reliably.
3. **TLS failure** — the SMSC does not offer TLS on that port, or its certificate
   is from an operator private CA. `ACS_SMPP_TLS_CA_FILE` names a CA bundle, but
   it is not a stack parameter and the root filesystem is read-only, so the
   bundle has to be built into the image until #31 delivers it. Turn TLS off
   (`--smpp-tls false`) only on a private link, since SMPP sends the password in
   clear.
4. **A `command_status` name such as `ESME_RINVPASWD` or `ESME_RBINDFAIL`** — the
   SMSC answered; take the name to the operator. A code outside the SMPP 3.4 table
   is reported only as vendor-specific or unlisted, never by its number, because
   the SMSC chooses the number freely; read it on the SMSC side.

With `eum` or `sns`, check the origination identity, the sandbox and the account
spending limit.

`OtpStoreContention` is a store problem, not an SMS one: concurrent issue requests
for one MSISDN kept invalidating each other's conditional writes. A burst for one
number is expected and self-limiting. Sustained across many numbers, check
DynamoDB throttling and the `5xx` alarm; throttling and capacity errors are raised
as faults, not counted as contention.

## Setting or rotating the SMPP password

The operator issues the SMSC password, so the stack cannot generate it. With
`SmsProvider=smpp` it creates the secret with a random 8-character placeholder,
and every OTP answers `503` until the real value is in place:

```bash
SMPP_SECRET=$(aws cloudformation describe-stacks --region $REGION --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='SmppPasswordSecretArn'].OutputValue" --output text)
# In a subshell, so the umask and the trap end with it. The password is not
# echoed and not in shell history; printf writes no trailing newline; the file
# is readable only by you and removed on every exit path, failures included.
(
  umask 077
  f=$(mktemp)
  trap 'rm -f "$f"' EXIT
  read -rs SMPP_PASSWORD
  printf '%s' "$SMPP_PASSWORD" > "$f"
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SMPP_SECRET" \
    --secret-string "file://$f"
)
aws ecs update-service --region $REGION --cluster rcs-acs-app-cluster \
  --service rcs-acs-app-service --force-new-deployment
```

The password must be at most 8 printable ASCII characters (SMPP 3.4). Anything
else stops the new tasks at startup with a configuration error that does not
quote the value; the circuit breaker then rolls back to the running tasks, which
still hold the previous value.

Secrets are read at task start, so tasks started before the update keep the old
value until the deployment replaces them. Agree the switch-over with the operator:
if the SMSC accepts only the new password, OTPs fail on old tasks until the
rollout completes.

`scripts/deploy.sh` passes every `Smpp*` parameter on every run, so repeat the
`--smpp-*` flags on every later deploy, or they reset to their defaults.

## DM response encoding failed

`DmEncodingError` is the server failing to encode its own WBXML response, answered
with `500`. Session and device state are saved only after encoding succeeds, so
nothing was stored and the client can retry. It is a server fault, not a device
one: read the `dm response encoding failure` log entry and open an issue.
`DmProtocolError` is the device-side counterpart — a request that could not be
parsed or decoded, answered with `400`.

## 5xx alarm fired

```bash
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/ecs/rcs-acs-app \
  --filter-pattern '{ $.level = "ERROR" }' --max-items 50
```

`unhandled error` entries carry a stack trace in the log but never in the response.
If the catalogue failed to load, the container will not have started at all — check
the ECS service events and `stoppedReason`.

## Unhealthy targets

1. ECS service events: `aws ecs describe-services --cluster rcs-acs-app-cluster
   --services rcs-acs-app-service`.
2. If tasks are cycling, the deployment circuit breaker will roll back
   automatically.
3. Cold start plus catalogue validation is covered by a 60 s health check grace
   period. A consistently slow start means the catalogue grew large or the store is
   unreachable.

## Rollback

```bash
scripts/deploy.sh --allowed-cidr <cidr> --certificate-arn <arn> \
  --image-tag <previous-git-sha> --skip-build
```

Repeat every parameter the stack was deployed with, including `--sms-provider`
and the `--smpp-*` flags; anything left out reverts to the script's default (see
[releasing.md](releasing.md#an-aws-deployment)). Tags are immutable, so the
previous image is still in ECR. The service deploys with
`MinimumHealthyPercent: 100` and the circuit breaker enabled, so a bad rollout
rolls itself back.

## Adding a provisioning parameter

1. Add the entry to `src/acs/catalog/omacp/base.yaml` (or a profile overlay).
2. `make coverage-doc` to regenerate `docs/spec-coverage.md`.
3. `make test` — a malformed catalogue fails at load, and a broken document fails
   structural validation.
4. Deploy. Bump affected subscribers with `/invalidate` if they should pick it up
   before their `validity` expires.

## Adding a management object

Drop a YAML file into `src/acs/catalog/omadm/`. See
[oma-dm.md](oma-dm.md#adding-a-management-object). No code change.

## Rotating the admin token

```bash
aws secretsmanager put-secret-value --region $REGION --secret-id "$SECRET" \
  --secret-string "$(openssl rand -hex 24)"
aws ecs update-service --region $REGION --cluster rcs-acs-app-cluster \
  --service rcs-acs-app-service --force-new-deployment
```

Secrets are read at task start, so a new deployment is required.

## Restoring data

The table has point-in-time recovery enabled.

```bash
aws dynamodb restore-table-to-point-in-time --region $REGION \
  --source-table-name rcs-acs-app-acs \
  --target-table-name rcs-acs-app-acs-restored \
  --restore-date-time 2026-01-01T00:00:00Z
```

Restore to a new table, verify, then repoint `ACS_TABLE_NAME`. Do not restore over
a live table.

## A first deployment rolled back and will not retry

If the very first `CREATE` of `rcs-acs-app` fails, CloudFormation rolls back — but
the DynamoDB table carries `DeletionPolicy: Retain`, so it **survives the
rollback**. The retry then fails early validation with
`AWS::EarlyValidation::ResourceExistenceCheck`, because the stack is trying to
create a table that already exists.

That retain policy is correct (a stack deletion must never destroy subscriber
data), so the recovery is manual and deliberate:

```bash
REGION=us-east-1
# 1. Confirm the retained table is genuinely empty before removing it.
aws dynamodb scan --region $REGION --table-name rcs-acs-app-acs --select COUNT

# 2. Delete the failed or REVIEW_IN_PROGRESS stack.
aws cloudformation delete-stack --region $REGION --stack-name rcs-acs-app
aws cloudformation wait stack-delete-complete --region $REGION --stack-name rcs-acs-app

# 3. Only if the scan returned Count: 0.
aws dynamodb delete-table --region $REGION --table-name rcs-acs-app-acs

# 4. Retry; the image is already in ECR.
scripts/deploy.sh --allowed-cidr <cidr> --region $REGION --skip-build
```

If the scan returns anything other than zero, the table holds real subscriber
state: do not delete it. Deploy the stack under a different `--stack-prefix`, or
import the existing table into the new stack.

## ECR is unreachable from the build host

`aws ecr get-login-password` hanging with no output usually means the host's VPC
has interface VPC endpoints for ECR in that region that the host cannot reach —
the endpoint DNS resolves to a private address and TCP 443 is dropped:

```bash
getent hosts api.ecr.$REGION.amazonaws.com          # a 10.x address means an endpoint
timeout 5 bash -c "cat </dev/null >/dev/tcp/api.ecr.$REGION.amazonaws.com/443"
```

Either fix the endpoint's security group, or build and push from a host with a
route to ECR in the target region.
