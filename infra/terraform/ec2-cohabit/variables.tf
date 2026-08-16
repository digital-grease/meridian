variable "region" {
  description = "AWS region. Must match the region of specter's EC2 instance and the meridian S3 archive bucket."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "AWS CLI profile name to authenticate with. Leave null to use the default credential chain. Specter's Terraform pins this to \"tf\"; meridian operators usually want the same."
  type        = string
  default     = null
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------------
# Specter's instance — meridian cohabits but does NOT manage it. The
# instance is created in `../specter/infra/main.tf` and named via the
# `Name = "specter"` tag. Either pass `instance_id` directly (preferred),
# or leave it unset and we'll resolve it via the `Name` tag lookup.
# ------------------------------------------------------------------------

variable "instance_id" {
  description = "Specter's EC2 instance id. Leave empty to resolve via tag lookup on var.instance_name_tag."
  type        = string
  default     = ""
}

variable "instance_name_tag" {
  description = "Name tag used to find specter's instance when var.instance_id is empty."
  type        = string
  default     = "specter"
}

variable "instance_role_name" {
  description = <<-EOT
    Name of specter's IAM role attached via instance profile.
    Meridian-specific policies (S3 + SSM read) are attached to this role
    via aws_iam_role_policy resources — meridian never owns the role.
    Mirrors specter's own pattern in `../specter/infra/main.tf`.
  EOT
  type        = string
  default     = "SpecterInstanceRole"
}

variable "instance_availability_zone" {
  description = "AZ specter's instance lives in. Required to colocate the EBS volume — EBS attaches only within the same AZ."
  type        = string
  default     = "us-east-2a"
}

# ------------------------------------------------------------------------
# Meridian-owned EBS volume
# ------------------------------------------------------------------------

variable "data_volume_size_gb" {
  description = "Size of the meridian-owned EBS data volume (model files + raw samples + run-log). gp3."
  type        = number
  default     = 30
}

variable "data_volume_device_name" {
  description = <<-EOT
    Block device path the volume attaches at. Must NOT collide with
    specter's `/dev/xvdf`. Linux kernel maps /dev/sdg → /dev/xvdg or
    /dev/nvme*n1 depending on instance generation.
  EOT
  type        = string
  default     = "/dev/sdg"
}

# ------------------------------------------------------------------------
# Meridian S3 archive — referenced for IAM. Bucket itself is provisioned
# by `../s3/`. Meridian must already have run that module.
# ------------------------------------------------------------------------

variable "archive_bucket_name" {
  description = "Name of the meridian S3 archive bucket (provisioned by `../s3/`)."
  type        = string
}

variable "archive_bucket_prefix" {
  description = "Key prefix used inside the archive bucket. Matches `storage.s3.prefix` in `meridian/config.yaml`."
  type        = string
  default     = "meridian/"

  validation {
    condition     = can(regex("^([a-zA-Z0-9._/-]+/)?$", var.archive_bucket_prefix))
    error_message = "archive_bucket_prefix must end with '/' or be empty."
  }
}

# ------------------------------------------------------------------------
# Provider API keys → SSM Parameter Store. Values are inserted MANUALLY
# via `aws ssm put-parameter --type SecureString` (Terraform never sees
# plaintext; see README). The resources here just provision the parameter
# shells with placeholder values and the Lambda/instance IAM grants.
# ------------------------------------------------------------------------

variable "anthropic_api_key_param_name" {
  description = "SSM Parameter Store path for ANTHROPIC_API_KEY. Must match the env var the wrapper sets MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH to."
  type        = string
  default     = "/meridian/anthropic-api-key"
}

variable "openai_api_key_param_name" {
  description = "SSM Parameter Store path for OPENAI_API_KEY. Must match the env var the wrapper sets MERIDIAN_SECRETS_SSM_OPENAI_PATH to."
  type        = string
  default     = "/meridian/openai-api-key"
}

# ------------------------------------------------------------------------
# Alerting
# ------------------------------------------------------------------------

variable "alert_email" {
  description = "Email address subscribed to the meridian-pipeline-alerts SNS topic. Receives deferral and failure notifications."
  type        = string
}

# ------------------------------------------------------------------------
# Schedule
# ------------------------------------------------------------------------

variable "schedule_cron" {
  description = "EventBridge Scheduler cron expression in the configured timezone. Default: Mon 04:00."
  type        = string
  default     = "cron(0 4 ? * MON *)"
}

variable "schedule_timezone" {
  description = "IANA timezone for the schedule. America/Chicago handles DST automatically; the user's idle-window guarantee is in local time."
  type        = string
  default     = "America/Chicago"
}

# ------------------------------------------------------------------------
# Lifecycle / orchestration
# ------------------------------------------------------------------------

variable "ssm_command_timeout_seconds" {
  description = <<-EOT
    SSM SendCommand DELIVERY deadline: how long SSM keeps trying to hand
    the command to the instance's agent before marking it
    DeliveryTimedOut. It does NOT bound how long the wrapper may run.
    Dispatch is fire-and-forget (Lambda's 15-minute ceiling cannot sit
    through a 30-90 minute pipeline run), so nothing on this side has a
    run budget to raise; that budget lives in scripts/run-weekly.sh.

    The description used to claim this capped the run, and the Lambda
    read the variable nowhere while hardcoding 600, so an operator
    raising it to buy the pipeline more time got a clean apply and no
    behaviour change whatsoever. The Lambda now reads it. The default
    dropped from 5400 to 600 to match what the code has actually been
    doing since the module was written, so wiring it up is a no-op.
  EOT
  type        = number
  default     = 600 # 10 minutes to reach the agent; the run itself is unbounded here

  validation {
    # SSM accepts 30 s to 30 days. Anything under a minute risks a
    # DeliveryTimedOut on an instance that is still finishing its boot.
    condition     = var.ssm_command_timeout_seconds >= 60 && var.ssm_command_timeout_seconds <= 2592000
    error_message = "ssm_command_timeout_seconds must be between 60 and 2592000 (SSM's own range is 30-2592000)."
  }
}

variable "wrapper_script_path" {
  description = "Absolute path of the wrapper script on the instance. Phase 3 places it here as part of on-instance setup."
  type        = string
  default     = "/data/meridian/repo/scripts/run-weekly.sh"
}
