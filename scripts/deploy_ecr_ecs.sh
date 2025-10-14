#!/usr/bin/env bash
set -euo pipefail

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required but not installed" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not installed" >&2
  exit 1
fi

: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID to your AWS account ID}"
: "${AWS_REGION:?Set AWS_REGION to your AWS region (e.g. us-east-1)}"
: "${ECS_CLUSTER:?Set ECS_CLUSTER to the ECS cluster name}"
: "${ECS_SERVICE:?Set ECS_SERVICE to the ECS service name}"
ECR_REPOSITORY=${ECR_REPOSITORY:-pdf-analysis}
IMAGE_TAG=${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}
DOCKER_PLATFORM=${DOCKER_PLATFORM:-linux/amd64}

REPO_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"

if ! aws ecr describe-repositories --repository-names "${ECR_REPOSITORY}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "Creating ECR repository ${ECR_REPOSITORY} in ${AWS_REGION}" >&2
  aws ecr create-repository --repository-name "${ECR_REPOSITORY}" --region "${AWS_REGION}" >/dev/null
fi

echo "Authenticating Docker to ECR" >&2
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "Building image ${REPO_URI}:${IMAGE_TAG}" >&2
docker build --platform "${DOCKER_PLATFORM}" -t "${REPO_URI}:${IMAGE_TAG}" .

echo "Pushing image ${REPO_URI}:${IMAGE_TAG}" >&2
docker push "${REPO_URI}:${IMAGE_TAG}"

echo "Updating ECS service ${ECS_SERVICE} in cluster ${ECS_CLUSTER}" >&2
aws ecs update-service \
  --cluster "${ECS_CLUSTER}" \
  --service "${ECS_SERVICE}" \
  --force-new-deployment \
  --region "${AWS_REGION}" >/dev/null

echo
printf 'Deployment complete. Image: %s\n' "${REPO_URI}:${IMAGE_TAG}"
