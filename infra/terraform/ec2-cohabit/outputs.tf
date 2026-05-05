output "data_volume_id" {
  description = "Meridian data EBS volume id."
  value       = aws_ebs_volume.meridian_data.id
}

output "data_volume_device_name" {
  description = "Block device path the EBS volume attaches at on the instance."
  value       = var.data_volume_device_name
}

output "instance_id" {
  description = "Resolved EC2 instance id (specter's), echoed for sanity-check."
  value       = local.instance_id
}

output "lambda_function_name" {
  description = "Manual-invoke target: aws lambda invoke --function-name <this>."
  value       = aws_lambda_function.orchestrator.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.orchestrator.arn
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule name."
  value       = aws_scheduler_schedule.weekly.name
}

output "alerts_topic_arn" {
  description = "SNS topic ARN. Confirm the email subscription before the first run."
  value       = aws_sns_topic.alerts.arn
}

output "anthropic_param_name" {
  description = "SSM Parameter Store name for ANTHROPIC_API_KEY. Set the value via aws-cli put-parameter."
  value       = aws_ssm_parameter.anthropic_api_key.name
}

output "openai_param_name" {
  description = "SSM Parameter Store name for OPENAI_API_KEY. Set the value via aws-cli put-parameter."
  value       = aws_ssm_parameter.openai_api_key.name
}

output "manual_invoke_command" {
  description = "Convenience: ad-hoc trigger the orchestrator without waiting for the scheduler."
  value       = "aws lambda invoke --function-name ${aws_lambda_function.orchestrator.function_name} --region ${var.region} /tmp/meridian-orch.json"
}

output "set_anthropic_key_command" {
  description = "Run this (with the real key) to populate the SSM parameter."
  value       = "aws ssm put-parameter --type SecureString --overwrite --name ${aws_ssm_parameter.anthropic_api_key.name} --value <ANTHROPIC_API_KEY> --region ${var.region}"
}

output "set_openai_key_command" {
  description = "Run this (with the real key) to populate the SSM parameter."
  value       = "aws ssm put-parameter --type SecureString --overwrite --name ${aws_ssm_parameter.openai_api_key.name} --value <OPENAI_API_KEY> --region ${var.region}"
}
