# Separate, opt-in recovery stack. No EC2, service, portal or worker changes.
terraform {
  required_version = ">= 1.10.5, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    key          = "orion/recovery/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
provider "aws" {
  region = "ap-south-1"
  default_tags {
    tags = { Project = "orion-india", Purpose = "paper-recovery", ManagedBy = "Terraform" }
  }
}
variable "bucket_name" {
  type        = string
  description = "New dedicated private backup bucket; never the state or release bucket."
}
variable "runtime_role_name" {
  type    = string
  default = "orion-india-ec2"
}
variable "retention_days" {
  type    = number
  default = 35
  validation {
    condition     = var.retention_days >= 35 && floor(var.retention_days) == var.retention_days
    error_message = "Retain at least 35 days of daily backups."
  }
}
resource "aws_s3_bucket" "backup" {
  bucket        = var.bucket_name
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "backup" {
  bucket = aws_s3_bucket.backup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.backup.arn, "${aws_s3_bucket.backup.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket     = aws_s3_bucket.backup.id
  depends_on = [aws_s3_bucket_versioning.backup]
  rule {
    id     = "daily-retention"
    status = "Enabled"
    filter { prefix = "daily/" }
    expiration { days = var.retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  rule {
    id     = "expired-markers"
    status = "Enabled"
    filter { prefix = "daily/" }
    expiration { expired_object_delete_marker = true }
  }
}
resource "aws_iam_role_policy" "backup_writer" {
  name = "orion-private-backup"
  role = var.runtime_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.backup.arn}/daily/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketVersioning", "s3:GetBucketPolicyStatus"]
        Resource = aws_s3_bucket.backup.arn
      }
    ]
  })
}
output "backup_bucket" { value = aws_s3_bucket.backup.id }
