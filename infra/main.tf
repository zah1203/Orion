terraform {
  required_version = ">= 1.10.5, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    key          = "orion/infra/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
provider "aws" {
  region = "ap-south-1"
  default_tags {
    tags = { Project = "orion-india", ManagedBy = "Terraform" }
  }
}
variable "instance_profile_name" { type = string }
variable "secret_arn" { type = string }
variable "artifact_bucket" { type = string }
variable "instance_type" {
  type    = string
  default = "t3.small"
}
variable "ami_id" {
  type        = string
  description = "Pin an official Canonical Ubuntu 24.04 x86_64 AMI in ap-south-1; do not auto-refresh on each apply."
  validation {
    condition     = can(regex("^ami-[0-9a-f]+$", var.ami_id))
    error_message = "Supply a pinned AMI ID."
  }
}
resource "aws_vpc" "orion" {
  cidr_block           = "10.76.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
}
resource "aws_subnet" "orion" {
  vpc_id                  = aws_vpc.orion.id
  cidr_block              = "10.76.1.0/24"
  availability_zone       = "ap-south-1a"
  map_public_ip_on_launch = true
}
resource "aws_internet_gateway" "orion" { vpc_id = aws_vpc.orion.id }
resource "aws_route_table" "orion" {
  vpc_id = aws_vpc.orion.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.orion.id
  }
}
resource "aws_route_table_association" "orion" {
  subnet_id      = aws_subnet.orion.id
  route_table_id = aws_route_table.orion.id
}
resource "aws_security_group" "orion" {
  name_prefix = "orion-"
  description = "No inbound ports; administration through SSM"
  vpc_id      = aws_vpc.orion.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_instance" "orion" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.orion.id
  vpc_security_group_ids      = [aws_security_group.orion.id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = true
  disable_api_termination     = true
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    secret_arn = var.secret_arn
  })
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
  }
  lifecycle { prevent_destroy = true }
  depends_on = [aws_route_table_association.orion]
  tags       = { Name = "orion-india-paper" }
}
resource "aws_eip" "orion" {
  domain   = "vpc"
  instance = aws_instance.orion.id
  lifecycle { prevent_destroy = true }
}
output "kotak_whitelist_ip" { value = aws_eip.orion.public_ip }
output "instance_id" { value = aws_instance.orion.id }
output "artifact_bucket" { value = var.artifact_bucket }
