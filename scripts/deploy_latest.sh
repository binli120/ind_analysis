#!/bin/bash
# ============================================================
# Deploy new Docker image to ECS Fargate & cleanup old revisions
# ============================================================

# --- CONFIGURATION ---
REGION="us-east-1"
ACCOUNT_ID="977684304019"
REPO_NAME="pdf-analysis-service"
CLUSTER_NAME="pdf-analysis-cluster"
SERVICE_NAME="pdf-analysis-service"
TASK_FAMILY="pdf-analysis-task"
IMAGE_TAG="latest"
# ============================================================

echo "🔹 Step 1. Tag & push Docker image to ECR"
IMAGE_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME:$IMAGE_TAG"

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

docker tag $(docker images -q $REPO_NAME | head -n 1) $IMAGE_URI
docker push $IMAGE_URI

echo "✅ Image pushed: $IMAGE_URI"


# ------------------------------------------------------------
echo "🔹 Step 2. Get existing ECS task definition and update image"
TASKDEF_DIR="tmp/ecs"
TASKDEF_PATH="$TASKDEF_DIR/task-def.json"
mkdir -p "$TASKDEF_DIR"
aws ecs describe-task-definition \
  --task-definition $TASK_FAMILY \
  --region $REGION > "$TASKDEF_PATH"

# Update image in task definition
sed -i '' "s|\"image\": \".*\"|\"image\": \"$IMAGE_URI\"|" "$TASKDEF_PATH"

# Register new revision
aws ecs register-task-definition \
  --cli-input-json file://"$TASKDEF_PATH" \
  --region $REGION

NEW_REVISION=$(aws ecs describe-task-definition \
  --task-definition $TASK_FAMILY \
  --query 'taskDefinition.revision' \
  --output text \
  --region $REGION)

echo "✅ Registered new task revision: $TASK_FAMILY:$NEW_REVISION"


# ------------------------------------------------------------
echo "🔹 Step 3. Update ECS service to use new revision"
aws ecs update-service \
  --cluster $CLUSTER_NAME \
  --service $SERVICE_NAME \
  --task-definition $TASK_FAMILY:$NEW_REVISION \
  --region $REGION

echo "✅ ECS service updated to revision $NEW_REVISION"


# ------------------------------------------------------------
echo "🔹 Step 4. Force new deployment (in case old tasks linger)"
aws ecs update-service \
  --cluster $CLUSTER_NAME \
  --service $SERVICE_NAME \
  --force-new-deployment \
  --region $REGION

echo "✅ Forced new deployment"


# ------------------------------------------------------------
echo "🔹 Step 5. Check for old or pending tasks"
TASKS=$(aws ecs list-tasks \
  --cluster $CLUSTER_NAME \
  --service-name $SERVICE_NAME \
  --region $REGION \
  --query 'taskArns[]' \
  --output text)

if [ -n "$TASKS" ]; then
  echo "Found tasks: $TASKS"
  aws ecs describe-tasks \
    --cluster $CLUSTER_NAME \
    --tasks $TASKS \
    --region $REGION \
    --query 'tasks[*].{id:taskArn,status:lastStatus,def:taskDefinitionArn}'
else
  echo "No tasks currently running or pending."
fi


# ------------------------------------------------------------
echo "🔹 Step 6. Manually stop any pending old revision (:2, etc.)"
PENDING_TASKS=$(aws ecs list-tasks \
  --cluster $CLUSTER_NAME \
  --desired-status PENDING \
  --region $REGION \
  --query 'taskArns[]' \
  --output text)

if [ -n "$PENDING_TASKS" ]; then
  echo "Stopping pending tasks..."
  for task in $PENDING_TASKS; do
    echo "🛑 Stopping $task ..."
    aws ecs stop-task \
      --cluster $CLUSTER_NAME \
      --task $task \
      --reason "Cleanup pending old revision" \
      --region $REGION
  done
else
  echo "✅ No pending tasks found."
fi


# ------------------------------------------------------------
echo "🔹 Step 7. Verify deployment"
aws ecs describe-services \
  --cluster $CLUSTER_NAME \
  --services $SERVICE_NAME \
  --region $REGION \
  --query 'services[0].deployments'

echo "✅ Done. ECS service should now run only the latest revision ($TASK_FAMILY:$NEW_REVISION)."
