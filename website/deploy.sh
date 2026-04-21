#!/bin/bash
# Deploy static website to S3
# Usage: ./deploy.sh
#
# Prerequisites:
#   1. AWS CLI configured (aws configure)
#   2. S3 bucket created: secondsignalapps.com
#   3. Bucket configured for static website hosting
#
# One-time setup steps:
#
# 1. Create the S3 bucket:
#    aws s3 mb s3://secondsignalapps.com --region us-east-1
#
# 2. Enable static website hosting:
#    aws s3 website s3://secondsignalapps.com \
#      --index-document index.html \
#      --error-document index.html
#
# 3. Set bucket policy for public read:
#    aws s3api put-bucket-policy --bucket secondsignalapps.com --policy '{
#      "Version": "2012-10-17",
#      "Statement": [{
#        "Sid": "PublicReadGetObject",
#        "Effect": "Allow",
#        "Principal": "*",
#        "Action": "s3:GetObject",
#        "Resource": "arn:aws:s3:::secondsignalapps.com/*"
#      }]
#    }'
#
# 4. Disable "Block Public Access" on the bucket:
#    aws s3api put-public-access-block --bucket secondsignalapps.com \
#      --public-access-block-configuration \
#      'BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false'
#
# 5. In Route 53, create an A record (Alias) for secondsignalapps.com
#    pointing to the S3 website endpoint:
#      Alias target: s3-website-us-east-1.amazonaws.com
#      (Select the S3 bucket from the dropdown)
#
# 6. For HTTPS, create a CloudFront distribution:
#    - Origin: secondsignalapps.com.s3-website-us-east-1.amazonaws.com (HTTP only origin)
#    - Alternate domain: secondsignalapps.com
#    - SSL certificate: Request one in ACM (us-east-1) for secondsignalapps.com
#    - Then update the Route 53 A record to point to CloudFront instead of S3

set -e

BUCKET="secondsignalapps.com"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying to s3://$BUCKET..."

aws s3 sync "$DIR" "s3://$BUCKET" \
  --exclude "deploy.sh" \
  --exclude ".DS_Store" \
  --exclude "*.sh" \
  --content-type "text/html" \
  --cache-control "max-age=300"

echo "Done! Site is live at http://$BUCKET.s3-website-us-east-1.amazonaws.com"
echo "  (or https://secondsignalapps.com if CloudFront is configured)"
