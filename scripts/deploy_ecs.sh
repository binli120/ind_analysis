#!/bin/bash
set -euo pipefail

# =========================================================
# AWS ECR + ECS End-to-End Deployment Script (Fargate)
# =========================================================
# Region / account / resource names
REGION="us-east-1"
ACCOUNT_ID="977684304019"
ECR_REPO="pdf-analysis-service"
CLUSTER_NAME="pdf-analysis-cluster"
SERVICE_NAME="pdf-analysis-service"
TASK_FAMILY="pdf-analysis-task"
CONTAINER_NAME="pdf-analysis-service"
APP_PORT=8080

# =========================================================
# Step 1: Create ECR repo, login, build linux/amd64 image, push
# =========================================================

aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" >/dev/null 2>&1 || aws ecr create-repository --repository-name "$ECR_REPO" --image-scanning-configuration scanOnPush=true --region "$REGION"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:latest"
docker buildx build --platform linux/amd64 -t "$IMAGE_URI" .
docker push "$IMAGE_URI"

# =========================================================
# Step 2: Create ECS execution role if missing
# =========================================================

ROLE_NAME="ecsTaskExecutionRole"
aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 || {
  aws iam create-role --role-name "$ROLE_NAME"     --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
  aws iam attach-role-policy --role-name "$ROLE_NAME"     --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
}

# =========================================================
# Step 3: Networking - get VPC, subnets, and security group
# =========================================================

VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
readarray -t SUBNET_IDS < <(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC_ID --query 'Subnets[].SubnetId' --output text)

SG_ID=$(aws ec2 create-security-group   --group-name ${SERVICE_NAME}-sg   --description "ECS service SG"   --vpc-id "$VPC_ID"   --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress --group-id "$SG_ID"   --ip-permissions "IpProtocol=tcp,FromPort=$APP_PORT,ToPort=$APP_PORT,IpRanges=[{CidrIp=0.0.0.0/0,Description='app'}]" >/dev/null 2>&1 || true

# =========================================================
# Step 4: Create ECS cluster
# =========================================================

aws ecs create-cluster --cluster-name "$CLUSTER_NAME" >/dev/null 2>&1 || true

# =========================================================
# Step 5: Task definition
# =========================================================

cat > taskdef.json << JSON
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}",
  "containerDefinitions": [
    {
      "name": "${CONTAINER_NAME}",
      "image": "${IMAGE_URI}",
      "essential": true,
      "portMappings": [
        { "containerPort": ${APP_PORT}, "hostPort": ${APP_PORT}, "protocol": "tcp" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/${SERVICE_NAME}",
          "awslogs-region": "${REGION}",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
JSON

aws logs create-log-group --log-group-name /ecs/${SERVICE_NAME} >/dev/null 2>&1 || true
aws ecs register-task-definition --cli-input-json file://taskdef.json

# =========================================================
# Step 6: Create ECS service
# =========================================================

aws ecs create-service   --cluster "$CLUSTER_NAME"   --service-name "$SERVICE_NAME"   --task-definition "$TASK_FAMILY"   --desired-count 1   --launch-type FARGATE   --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_IDS[0]},${SUBNET_IDS[1]}],securityGroups=[$SG_ID],assignPublicIp=ENABLED}"

aws ecs wait services-stable --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME"

# =========================================================
# Step 7: Print public IP
# =========================================================

TASK_ARN=$(aws ecs list-tasks --cluster "$CLUSTER_NAME" --service-name "$SERVICE_NAME" --query 'taskArns[0]' --output text)
ENI_ID=$(aws ecs describe-tasks --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text)
PUBLIC_IP=$(aws ec2 describe-network-interfaces --network-interface-ids "$ENI_ID" --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
echo "========================================================="
echo "Service deployed successfully!"
echo "Public IP: http://$PUBLIC_IP:${APP_PORT}"
echo "========================================================="