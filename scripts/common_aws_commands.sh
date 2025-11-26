#!/bin/bash
# ===========================================================
# Auto-create AWS Application Load Balancer (ALB)
# and output Terraform variables for ECS + API Gateway use.
# ===========================================================

REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
ALB_NAME="pdf-analysis-alb"
SG_NAME="pdf-analysis-alb-sg"

echo "🔹 Region: $REGION"
echo "🔹 Account: $ACCOUNT_ID"

# -----------------------------------------------------------
# 1️⃣ Get or create default VPC
echo "👉 Checking for existing default VPC..."
VPC_ID=$(aws ec2 describe-vpcs \
  --region $REGION \
  --query 'Vpcs[?IsDefault==`true`].VpcId' \
  --output text)

if [ -z "$VPC_ID" ]; then
  echo "No default VPC found. Creating one..."
  VPC_ID=$(aws ec2 create-default-vpc --region $REGION \
    --query 'Vpc.VpcId' --output text)
  echo "✅ Created default VPC: $VPC_ID"
else
  echo "✅ Found default VPC: $VPC_ID"
fi

# -----------------------------------------------------------
# 2️⃣ Get two public subnets (MapPublicIpOnLaunch=True)
echo "👉 Finding subnets for VPC $VPC_ID..."
SUBNETS=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID \
  --region $REGION \
  --query 'Subnets[?MapPublicIpOnLaunch==`true`].SubnetId' \
  --output text)

# Pick first two subnets
SUBNET1=$(echo $SUBNETS | awk '{print $1}')
SUBNET2=$(echo $SUBNETS | awk '{print $2}')

if [ -z "$SUBNET1" ] || [ -z "$SUBNET2" ]; then
  echo "⚠️ Could not find two public subnets. Please verify subnets in your VPC."
  exit 1
fi

echo "✅ Using subnets: $SUBNET1, $SUBNET2"

# -----------------------------------------------------------
# 3️⃣ Create or reuse Security Group
echo "👉 Checking for security group: $SG_NAME ..."
SG_ID=$(aws ec2 describe-security-groups \
  --filters Name=group-name,Values=$SG_NAME Name=vpc-id,Values=$VPC_ID \
  --region $REGION \
  --query 'SecurityGroups[0].GroupId' \
  --output text)

if [ "$SG_ID" == "None" ] || [ -z "$SG_ID" ]; then
  echo "🔹 Creating new security group..."
  SG_ID=$(aws ec2 create-security-group \
    --group-name $SG_NAME \
    --description "Security group for ALB" \
    --vpc-id $VPC_ID \
    --region $REGION \
    --query 'GroupId' \
    --output text)
  echo "✅ Created security group: $SG_ID"

  # Add inbound rules for HTTP & HTTPS
  aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 \
    --region $REGION >/dev/null

  aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 \
    --region $REGION >/dev/null

  echo "✅ Added inbound rules for ports 80 and 443"
else
  echo "✅ Reusing existing security group: $SG_ID"
fi

# -----------------------------------------------------------
# 4️⃣ Create the ALB
echo "👉 Creating ALB..."
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name $ALB_NAME \
  --subnets $SUBNET1 $SUBNET2 \
  --security-groups $SG_ID \
  --scheme internet-facing \
  --type application \
  --region $REGION \
  --query 'LoadBalancers[0].LoadBalancerArn' \
  --output text)

echo "✅ ALB created: $ALB_ARN"

# Wait for ALB to become active
echo "⏳ Waiting for ALB to become active..."
aws elbv2 wait load-balancer-available \
  --load-balancer-arns $ALB_ARN \
  --region $REGION
echo "✅ ALB is active!"

# -----------------------------------------------------------
# 5️⃣ Create a listener (HTTP port 80)
echo "👉 Creating listener for port 80..."
LISTENER_ARN=$(aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP \
  --port 80 \
  --default-actions Type=fixed-response,FixedResponseConfig='{StatusCode=200,ContentType=text/plain,MessageBody="OK"}' \
  --region $REGION \
  --query 'Listeners[0].ListenerArn' \
  --output text)

echo "✅ Listener created: $LISTENER_ARN"

# -----------------------------------------------------------
# 6️⃣ Output everything for Terraform
echo ""
echo "==========================================================="
echo "✅ ALB Setup Complete! Use these values in Terraform:"
echo "==========================================================="
echo "-var=\"alb_listener_arn=$LISTENER_ARN\""
echo "-var='vpc_link_subnet_ids=[\"$SUBNET1\",\"$SUBNET2\"]'"
echo "-var='vpc_link_security_group_ids=[\"$SG_ID\"]'"
echo ""
echo "💡 Example Terraform apply:"
echo "terraform apply \\"
echo "  -var=\"aws_region=$REGION\" \\"
echo "  -var=\"api_name=pdf-analysis-api\" \\"
echo "  -var=\"stage_name=prod\" \\"
echo "  -var=\"alb_listener_arn=$LISTENER_ARN\" \\"
echo "  -var='vpc_link_subnet_ids=[\"$SUBNET1\",\"$SUBNET2\"]' \\"
echo "  -var='vpc_link_security_group_ids=[\"$SG_ID\"]'"
echo "==========================================================="

