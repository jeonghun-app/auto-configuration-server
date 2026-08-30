#!/usr/bin/env bash
#
# One-command AWS deployment for the GSMA RCS ACS.
#
# Builds the image, pushes it to ECR, deploys (or updates) the CloudFormation
# stacks, waits for the service to become healthy, and runs the end-to-end
# verification. Idempotent: safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

STACK_PREFIX="rcs-acs"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-2}}"
ALLOWED_CIDR=""
CERT_ARN=""
ENVIRONMENT="prod"
DESIRED_COUNT="2"
SMS_PROVIDER="eum"
SMS_ORIGINATION=""
IMAGE_TAG=""
SKIP_BUILD="false"
SKIP_VERIFY="false"

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh --allowed-cidr <CIDR> [options]

Required:
  --allowed-cidr <CIDR>     Address range permitted to reach the load balancer.
                            There is no default and 0.0.0.0/0 is refused: the ACS
                            handles IMSI, IMEI and MSISDN.

Options:
  --region <region>         AWS region (default: $AWS_REGION or ap-northeast-2)
  --stack-prefix <name>     CloudFormation stack name prefix (default: rcs-acs)
  --certificate-arn <arn>   ACM certificate for HTTPS. Strongly recommended:
                            without it the ACS serves plaintext HTTP.
  --environment <env>       staging | prod (default: prod)
  --desired-count <n>       Number of Fargate tasks (default: 2)
  --sms-provider <p>        eum | sns (default: eum)
  --sms-origination <id>    AWS End User Messaging origination identity
  --image-tag <tag>         Image tag to build and deploy (default: git sha)
  --skip-build              Reuse the existing image for this tag
  --skip-verify             Do not run the end-to-end verification
  -h, --help                Show this help

Example:
  scripts/deploy.sh --allowed-cidr 203.0.113.10/32 \
      --certificate-arn arn:aws:acm:ap-northeast-2:123456789012:certificate/abc
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allowed-cidr) ALLOWED_CIDR="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --stack-prefix) STACK_PREFIX="$2"; shift 2 ;;
    --certificate-arn) CERT_ARN="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --desired-count) DESIRED_COUNT="$2"; shift 2 ;;
    --sms-provider) SMS_PROVIDER="$2"; shift 2 ;;
    --sms-origination) SMS_ORIGINATION="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD="true"; shift ;;
    --skip-verify) SKIP_VERIFY="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$ALLOWED_CIDR" ]]; then
  echo "error: --allowed-cidr is required" >&2
  usage
  exit 2
fi

if [[ "$ALLOWED_CIDR" == "0.0.0.0/0" ]]; then
  cat >&2 <<'REFUSE'
error: refusing --allowed-cidr 0.0.0.0/0

The ACS accepts IMSI, IMEI and MSISDN in query strings and issues IMS
credentials. Opening it to the whole internet before you intend handsets to reach
it exposes an OTP endpoint that costs real money to abuse.

If a globally reachable ACS is genuinely what you want, edit the AllowedCidr
parameter on the stack directly, after putting a WAF rate-based rule in front.
REFUSE
  exit 2
fi

if [[ -z "$CERT_ARN" ]]; then
  echo "WARNING: no --certificate-arn given. The ACS will be served over plain" >&2
  echo "         HTTP. Acceptable for a demo, not for real handsets." >&2
fi

command -v aws >/dev/null || { echo "error: aws CLI not found" >&2; exit 1; }
command -v docker >/dev/null || { echo "error: docker not found" >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ECR_STACK="${STACK_PREFIX}-ecr"
APP_STACK="${STACK_PREFIX}-app"

if [[ -z "$IMAGE_TAG" ]]; then
  if git -C "$PROJECT_DIR" rev-parse --short HEAD >/dev/null 2>&1; then
    IMAGE_TAG="$(git -C "$PROJECT_DIR" rev-parse --short HEAD)"
  else
    IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
  fi
fi

echo "=============================================================="
echo " account     : $ACCOUNT_ID"
echo " region      : $REGION"
echo " environment : $ENVIRONMENT"
echo " image tag   : $IMAGE_TAG"
echo " allowed CIDR: $ALLOWED_CIDR"
echo " TLS         : ${CERT_ARN:-<none, HTTP only>}"
echo "=============================================================="

echo
echo "[1/5] registry stack"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$ECR_STACK" \
  --template-file "$PROJECT_DIR/infra/ecr.yaml" \
  --parameter-overrides "RepositoryName=${STACK_PREFIX}" \
  --no-fail-on-empty-changeset

REPO_URI="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$ECR_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)"
IMAGE_URI="${REPO_URI}:${IMAGE_TAG}"

echo
echo "[2/5] container image"
if [[ "$SKIP_BUILD" == "true" ]]; then
  echo "  skipped (--skip-build)"
else
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"
  docker build -t "$IMAGE_URI" "$PROJECT_DIR"
  # Tags are immutable in this repository, so a re-push of the same tag fails.
  if aws ecr describe-images --region "$REGION" \
       --repository-name "$STACK_PREFIX" --image-ids "imageTag=$IMAGE_TAG" \
       >/dev/null 2>&1; then
    echo "  image $IMAGE_TAG already present in ECR; reusing it"
  else
    docker push "$IMAGE_URI"
  fi
fi

echo
echo "[3/5] application stack"
PARAMS=(
  "ImageUri=${IMAGE_URI}"
  "AllowedCidr=${ALLOWED_CIDR}"
  "Environment=${ENVIRONMENT}"
  "DesiredCount=${DESIRED_COUNT}"
  "SmsProvider=${SMS_PROVIDER}"
  "SmsOriginationIdentity=${SMS_ORIGINATION}"
  "CertificateArn=${CERT_ARN}"
)
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$APP_STACK" \
  --template-file "$PROJECT_DIR/infra/app.yaml" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "${PARAMS[@]}" \
  --no-fail-on-empty-changeset

BASE_URL="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BaseUrl'].OutputValue" --output text)"
SECRET_ARN="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AdminTokenSecretArn'].OutputValue" --output text)"

echo
echo "[4/5] waiting for the service to become healthy"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "${BASE_URL}/healthz" >/dev/null 2>&1; then
    echo "  healthy: ${BASE_URL}/healthz"
    break
  fi
  sleep 5
done

echo
echo "[5/5] end-to-end verification"
if [[ "$SKIP_VERIFY" == "true" ]]; then
  echo "  skipped (--skip-verify)"
else
  ADMIN_TOKEN="$(aws secretsmanager get-secret-value \
    --region "$REGION" --secret-id "$SECRET_ARN" \
    --query SecretString --output text)"
  PYTHON_BIN="${PYTHON_BIN:-python3}"
  ACS_BASE_URL="$BASE_URL" ACS_ADMIN_TOKEN="$ADMIN_TOKEN" \
    "$PYTHON_BIN" "$PROJECT_DIR/scripts/verify_stack.py"
fi

cat <<SUMMARY

==============================================================
Deployment complete.

  base URL            ${BASE_URL}
  configuration       ${BASE_URL}/config
  OMA-DM              ${BASE_URL}/dm
  admin token secret  ${SECRET_ARN}

Read the admin token with:
  aws secretsmanager get-secret-value --region ${REGION} \\
      --secret-id ${SECRET_ARN} --query SecretString --output text

For real handsets, point
  config.rcs.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org
at the load balancer, with a certificate covering that name.

Tear down with: scripts/teardown.sh --stack-prefix ${STACK_PREFIX} --region ${REGION}
==============================================================
SUMMARY
