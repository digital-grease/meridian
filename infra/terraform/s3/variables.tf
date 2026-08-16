variable "bucket_name" {
  description = "Globally-unique S3 bucket name for the raw-sample archive."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must match S3 DNS naming rules (3-63 chars, lowercase, no underscores)."
  }
}

variable "region" {
  description = "AWS region for the bucket. Pinned to us-east-2 to colocate with specter's EC2 instance (free intra-region S3 transfer; cross-region replication is a v2 concern)."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "AWS CLI profile name to authenticate with. Leave null to use the default credential chain (env vars, instance role, etc.). Specter's Terraform pins this to \"tf\"; meridian operators usually want the same."
  type        = string
  default     = null
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}

variable "create_writer_iam_user" {
  description = <<-EOT
    Whether to create a dedicated IAM user with an access key for the pipeline.
    Set this to false if you will grant write access via GitHub Actions OIDC
    (preferred for CI) or via an instance profile. See README.md.
  EOT
  type        = bool
  default     = true
}

variable "enable_github_oidc_role" {
  description = <<-EOT
    Whether to create an IAM role assumable by the project's GitHub Actions
    workflow via OIDC. Preferred over long-lived access keys.
  EOT
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in the target AWS account. Required when enable_github_oidc_role = true."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "owner/repo identifier for the sub-claim on the OIDC trust policy, e.g. \"digital-grease/meridian\"."
  type        = string
  default     = ""
}

variable "alert_topic_arn" {
  description = <<-EOT
    SNS topic the weekly-pipeline workflow publishes to when a publish
    fails. Set this to the ec2-cohabit module's `alerts_topic_arn` output
    so orchestrator-side and publish-side failures land in one operator
    inbox — a publish failure previously produced only a GitHub issue
    that nothing routed to a human, which is how 2026-W30 and W31 went
    unnoticed for two weeks.

    Leave empty to skip granting sns:Publish; the workflow's alert step
    is a no-op without it and only the GitHub issue is filed.
  EOT
  type        = string
  default     = ""
}

variable "transition_to_ia_days" {
  description = "Days after creation before meridian/raw/ objects transition to STANDARD_IA. S3 will not transition an object under 128 KB and today's raw objects are 9-93 KB, so this is expected to be a no-op until the object profile changes; see the lifecycle comment in main.tf."
  type        = number
  default     = 30
}

variable "transition_to_deep_archive_days" {
  description = "Days after creation before meridian/raw/ objects make their second transition, now to GLACIER_IR rather than the DEEP_ARCHIVE this variable is named after. The name is kept because renaming it forces every operator's tfvars to change. GLACIER_IR because published results must stay reproducible from the raw data without a 12-to-48-hour restore. Same 128 KB floor caveat as transition_to_ia_days."
  type        = number
  default     = 365
}

variable "expire_noncurrent_versions_days" {
  description = "Days after a version becomes non-current before it is deleted. Applies to meridian/manifests/ ONLY, where the churn is the latest.json pointer. It is deliberately not applied to meridian/raw/: a non-current raw object is prior raw data, and the hard rule is that raw data is never destroyed."
  type        = number
  default     = 90
}
