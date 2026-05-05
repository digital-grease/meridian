data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

# Resolve specter's instance: prefer var.instance_id when set, otherwise
# look up by Name tag. Both branches end up at local.instance_id.
data "aws_instances" "by_tag" {
  count = var.instance_id == "" ? 1 : 0

  filter {
    name   = "tag:Name"
    values = [var.instance_name_tag]
  }

  filter {
    name   = "instance-state-name"
    values = ["pending", "running", "stopped", "stopping", "shutting-down"]
  }
}

locals {
  instance_id = (
    var.instance_id != ""
    ? var.instance_id
    : try(data.aws_instances.by_tag[0].ids[0], "")
  )

  bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.archive_bucket_name}"

  ssm_param_arns = [
    "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.anthropic_api_key_param_name}",
    "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.openai_api_key_param_name}",
  ]
}

# Fail fast on apply if neither route resolves an instance — better here
# than during the EBS attachment, which produces a confusing AWS-side error.
resource "terraform_data" "validate_instance" {
  lifecycle {
    precondition {
      condition     = local.instance_id != ""
      error_message = "Could not resolve specter's instance id. Pass var.instance_id explicitly or ensure an instance with tag Name=${var.instance_name_tag} exists in ${var.region}."
    }
  }
}
