terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
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
        Component = "raw-sample-archive"
        ManagedBy = "terraform"
      },
      var.tags,
    )
  }
}
