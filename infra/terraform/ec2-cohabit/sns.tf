# Alerting topic. Lambda publishes here on:
#   - Deferral (instance was already running when we tried to start it)
#   - Pre-flight contention (in-instance check tripped — GPU busy or
#     specter process detected)
#   - Wrapper script non-zero exit
#   - SSM command timeout
#
# Subscription confirmation: AWS sends an "Confirm subscription" email
# to var.alert_email after first apply. The user has to click the link;
# Terraform can't auto-confirm.

resource "aws_sns_topic" "alerts" {
  name = "meridian-pipeline-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
