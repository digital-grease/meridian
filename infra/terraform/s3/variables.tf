variable "bucket_name" {
  description = "Globally-unique S3 bucket name for the raw-sample archive."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must match S3 DNS naming rules (3-63 chars, lowercase, no underscores)."
  }
}

variable "region" {
  description = "AWS region for the bucket. Keep this pinned; cross-region replication is a v2 concern."
  type        = string
  default     = "us-east-1"
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
  description = "owner/repo identifier for the sub-claim on the OIDC trust policy, e.g. \"drift-audit/meridian\"."
  type        = string
  default     = ""
}

variable "transition_to_ia_days" {
  description = "Days after creation before raw/ objects transition to STANDARD_IA."
  type        = number
  default     = 30
}

variable "transition_to_deep_archive_days" {
  description = "Days after creation before raw/ objects transition to DEEP_ARCHIVE. The hard rule is \"raw data is never destroyed\", so this lifecycle only moves classes — it never expires current versions."
  type        = number
  default     = 365
}

variable "expire_noncurrent_versions_days" {
  description = "Days after a version becomes non-current before it is deleted. Guards against cost creep from accidental overwrites; current versions are never affected."
  type        = number
  default     = 90
}
