# Provider API keys live as SecureString parameters. Terraform creates
# the parameter shells with throwaway placeholders; the real values are
# inserted manually via `aws ssm put-parameter --type SecureString
# --overwrite --name <path> --value <secret>`.
#
# !! terraform.tfstate CONTAINS THESE KEYS IN CLEARTEXT. Treat the state
# file as secret material: never attach it to an issue, sync it to
# unencrypted storage, or relax the *.tfstate rule in .gitignore.
#
# This corrects an earlier comment here which claimed "plaintext never
# enters Terraform state". It does. `lifecycle.ignore_changes = [value]`
# suppresses the DIFF, so Terraform won't clobber the manually-set key —
# but the AWS provider still READS the SecureString back (decrypted) on
# every refresh and writes it into state. Verified 2026-08-05: state
# holds 108- and 167-character values, not the 19-character placeholder.
#
# The durable fix is to stop managing these resources in Terraform at
# all (create the parameter shells out-of-band and derive the ARNs from
# the name variables), since ignore_changes already means Terraform
# contributes nothing but the shell. Tracked as a follow-up; until then
# the state file is sensitive and is mode 0600 on the operator's box.
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
