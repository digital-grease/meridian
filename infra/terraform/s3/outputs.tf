output "bucket_name" {
  description = "Name of the archive bucket. Use in meridian/config.yaml:storage.s3.bucket."
  value       = aws_s3_bucket.archive.bucket
}

output "bucket_arn" {
  description = "ARN of the archive bucket."
  value       = aws_s3_bucket.archive.arn
}

output "bucket_region" {
  description = "Region of the archive bucket."
  value       = aws_s3_bucket.archive.region
}

output "writer_policy_arn" {
  description = "ARN of the IAM policy granting write access. Attach to any other principal that should upload."
  value       = aws_iam_policy.writer.arn
}

output "writer_user_name" {
  description = "Name of the IAM user (when create_writer_iam_user = true)."
  value       = try(aws_iam_user.writer[0].name, null)
}

output "writer_access_key_id" {
  description = "Access key ID for the IAM writer. Rotate every 90 days."
  value       = try(aws_iam_access_key.writer[0].id, null)
  sensitive   = true
}

output "writer_secret_access_key" {
  description = "Secret access key for the IAM writer. Store in a secrets manager, not the repo."
  value       = try(aws_iam_access_key.writer[0].secret, null)
  sensitive   = true
}

output "github_writer_role_arn" {
  description = "ARN of the GitHub OIDC role (when enable_github_oidc_role = true). Set this as the AWS_ROLE_TO_ASSUME secret in the weekly-pipeline workflow."
  value       = try(aws_iam_role.github_writer[0].arn, null)
}
