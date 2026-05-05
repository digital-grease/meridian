# Provider API keys live as SecureString parameters. Terraform creates
# the parameter shells with throwaway placeholders; the real values are
# inserted manually via `aws ssm put-parameter --type SecureString
# --overwrite --name <path> --value <secret>` so plaintext never enters
# Terraform state.
#
# `lifecycle.ignore_changes = [value]` is what enforces this: the
# manual update via aws-cli won't drift the Terraform state.
#
# The parameters use the AWS-managed key (alias/aws/ssm) for encryption.
# Migrating to a CMK is a v2 concern — would require kms:Decrypt grants
# on every consumer (the EC2 instance role + the Lambda role).

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = var.anthropic_api_key_param_name
  description = "Anthropic API key for the meridian pipeline. Update via aws-cli put-parameter; do NOT commit the value."
  type        = "SecureString"
  value       = "REPLACE-VIA-AWS-CLI"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "openai_api_key" {
  name        = var.openai_api_key_param_name
  description = "OpenAI API key for the meridian pipeline. Update via aws-cli put-parameter; do NOT commit the value."
  type        = "SecureString"
  value       = "REPLACE-VIA-AWS-CLI"

  lifecycle {
    ignore_changes = [value]
  }
}
