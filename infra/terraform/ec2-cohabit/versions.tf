terraform {
  required_version = ">= 1.10"

  # State lives in S3 so every operator host sees the same serial; a local
  # state file on one machine went stale for weeks while another host
  # applied. The bucket is bootstrapped outside Terraform (see README.md).
  # use_lockfile takes an S3-native lock, so no DynamoDB table is needed.
  # Credentials come from the environment: run with AWS_PROFILE=tf.
  backend "s3" {
    bucket       = "meridian-tfstate-421515025815"
    key          = "meridian/ec2-cohabit.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = merge(
      {
        Project   = "meridian"
        Component = "ec2-cohabit"
        ManagedBy = "terraform"
      },
      var.tags,
    )
  }
}
