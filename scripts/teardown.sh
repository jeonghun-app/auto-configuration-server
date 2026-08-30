#!/usr/bin/env bash
#
# Tear down the ACS application stack.
#
# The DynamoDB table, the ECR repository and any ALB access log bucket carry
# DeletionPolicy: Retain, so they survive this on purpose — subscriber data and
# deployed images should not vanish because someone deleted a stack. The script
# prints exactly what is left behind and how to remove it.
#
set -euo pipefail

STACK_PREFIX="rcs-acs"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-2}}"
ASSUME_YES="false"
DELETE_REGISTRY="false"

usage() {
  cat <<'USAGE'
Usage: scripts/teardown.sh [options]

Options:
  --stack-prefix <name>   Stack name prefix (default: rcs-acs)
  --region <region>       AWS region
  --delete-registry       Also delete the ECR stack (images are retained)
  --yes                   Do not prompt for confirmation
  -h, --help              Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-prefix) STACK_PREFIX="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --delete-registry) DELETE_REGISTRY="true"; shift ;;
    --yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

APP_STACK="${STACK_PREFIX}-app"
ECR_STACK="${STACK_PREFIX}-ecr"

echo "About to delete CloudFormation stack: $APP_STACK (region $REGION)"
echo "This removes the load balancer, the ECS service and the VPC."
echo "Retained: the DynamoDB table, Secrets Manager secrets and container images."

if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "Type the stack name to confirm: " confirm
  if [[ "$confirm" != "$APP_STACK" ]]; then
    echo "aborted"
    exit 1
  fi
fi

TABLE_NAME="$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" \
  --output text 2>/dev/null || echo '')"

aws cloudformation delete-stack --region "$REGION" --stack-name "$APP_STACK"
echo "waiting for deletion..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$APP_STACK"
echo "application stack deleted"

if [[ "$DELETE_REGISTRY" == "true" ]]; then
  aws cloudformation delete-stack --region "$REGION" --stack-name "$ECR_STACK"
  aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$ECR_STACK"
  echo "registry stack deleted (repository retained)"
fi

cat <<REMAINING

Retained resources, and how to remove them if you really want to:

  DynamoDB table (subscriber records, tokens, device inventory)
    aws dynamodb delete-table --region ${REGION} --table-name ${TABLE_NAME:-<table>}

  Secrets Manager secrets (admin token, PII hash key)
    aws secretsmanager delete-secret --region ${REGION} \\
        --secret-id ${STACK_PREFIX}-app/admin-token --force-delete-without-recovery

  ECR repository (container images)
    aws ecr delete-repository --region ${REGION} \\
        --repository-name ${STACK_PREFIX} --force

Each of these destroys data irreversibly. Run them deliberately, not as cleanup.
REMAINING
