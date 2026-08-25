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
    through a 30-90 minute pipeline run). To raise the run budget, use
    ssm_execution_timeout_seconds below, not this: this one stops
    mattering the moment the agent picks the command up.

    The description used to claim this capped the run, and the Lambda
    read the variable nowhere while hardcoding 600, so an operator
    raising it to buy the pipeline more time got a clean apply and no
    behaviour change whatsoever. The Lambda now reads it. The default
    dropped from 5400 to 600 to match what the code has actually been
    doing since the module was written, so wiring it up is a no-op.
  EOT
  type        = number
  default     = 600 # 10 minutes to reach the agent

  validation {
    # SSM accepts 30 s to 30 days. Anything under a minute risks a
    # DeliveryTimedOut on an instance that is still finishing its boot.
    condition     = var.ssm_command_timeout_seconds >= 60 && var.ssm_command_timeout_seconds <= 2592000
    error_message = "ssm_command_timeout_seconds must be between 60 and 2592000 (SSM's own range is 30-2592000)."
  }
}

variable "ssm_execution_timeout_seconds" {
  description = <<-EOT
    SSM EXECUTION ceiling, passed as the AWS-RunShellScript
    `executionTimeout` document parameter: how long the wrapper may
    actually run before SSM SIGKILLs it and reports ExecutionTimedOut.

    This is the knob that ssm_command_timeout_seconds was mistaken for.
    Leaving it unset does not mean the run is unbounded, which is the
    assumption the module shipped with; it means the document default of
    3600 s applies. 2026-W34 hit that ceiling once opus-5 joined
    opus-4-8 in the roster and pushed the run to about 2h10m. The run
    was killed with no manifest written, and because SIGKILL cannot be
    trapped, scripts/run-weekly.sh never reached its self-stop and the
    g5.2xlarge billed roughly 18 idle hours.

    Sized as a runaway ceiling rather than an expected duration. A
    healthy run stops its own instance on completion and never gets
    near this, so slack here is free; tightness here costs a week of
    data that cannot be backfilled.
  EOT
  type        = number
  default     = 21600 # 6h ceiling over a roughly 2h run

  validation {
    # SSM's own range for executionTimeout is 30 to 172800 (48h). The
    # floor here is deliberately far above 30: the pipeline has never
    # finished in under 20 minutes, so any value that low is a
    # misconfiguration that would guarantee a killed run.
    condition     = var.ssm_execution_timeout_seconds >= 3600 && var.ssm_execution_timeout_seconds <= 172800
    error_message = "ssm_execution_timeout_seconds must be between 3600 and 172800 (SSM's own range is 30-172800)."
  }
}

variable "wrapper_script_path" {
  description = "Absolute path of the wrapper script on the instance. Phase 3 places it here as part of on-instance setup."
  type        = string
  default     = "/data/meridian/repo/scripts/run-weekly.sh"
}

# ------------------------------------------------------------------------
# Instance reaper (reaper.tf) - backstop for the wrapper's self-stop
# ------------------------------------------------------------------------

variable "reaper_min_uptime_seconds" {
  description = <<-EOT
    How long the instance must have been running before the reaper will
    judge it at all.

    This guards the startup race, and it is the value most likely to be
    lowered by somebody impatient about billing. Do not. The
    orchestrator starts the instance and then waits for InstanceStatusOk
    (up to 600 s) before it dispatches, so for that whole window the box
    is running with no meridian SSM invocation recorded against it and
    is indistinguishable from a specter boot. A reaper firing inside
    that window stops the weekly run before it starts.

    The default clears the readiness wait several times over. What it
    costs is at most one extra hour of billing on a failure path that
    used to run for eighteen.
  EOT
  type        = number
  default     = 2700 # 45 minutes

  validation {
    # 900 s is roughly the readiness wait plus a margin. Below that the
    # reaper starts racing the orchestrator it is supposed to protect.
    condition     = var.reaper_min_uptime_seconds >= 900 && var.reaper_min_uptime_seconds <= 86400
    error_message = "reaper_min_uptime_seconds must be between 900 and 86400."
  }
}

variable "gpu_memory_threshold_mb" {
  description = <<-EOT
    GPU memory in use, in MB, above which the shared instance counts as
    busy with somebody else's work.

    Mirrors GPU_MEMORY_THRESHOLD_MB in scripts/run-weekly.sh, whose
    pre-flight uses the identical test to decide whether to defer to
    specter. The two are deliberately the same number: "busy" should
    mean one thing in this system, so that the wrapper declining to
    start and the reaper declining to stop are the same judgement made
    at different moments.

    Non-zero rather than zero because an idle CUDA context and the
    display stack hold a few tens of MB on a g5 without anyone using it.
  EOT
  type        = number
  default     = 500

  validation {
    condition     = var.gpu_memory_threshold_mb >= 0 && var.gpu_memory_threshold_mb <= 24000
    error_message = "gpu_memory_threshold_mb must be between 0 and 24000 (a g5.2xlarge A10G has 24 GB)."
  }
}
