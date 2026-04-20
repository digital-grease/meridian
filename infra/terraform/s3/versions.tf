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
  region = var.region
  default_tags {
    tags = merge(
      {
        Project   = "drift-audit"
        Component = "raw-sample-archive"
        ManagedBy = "terraform"
      },
      var.tags,
    )
  }
}
