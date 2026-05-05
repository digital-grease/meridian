# Meridian's data volume — separate from specter's `/data` (which lives on
# `/dev/xvdf`). Mounted on the instance at `/data/meridian` by the
# Phase 3 bootstrap script (`scripts/ec2-bootstrap.sh`).
#
# `prevent_destroy` enforces the project's "raw data is never destroyed"
# rule at the IaC layer. To intentionally retire the volume: detach,
# `terraform state rm`, then delete via the AWS console or CLI.
resource "aws_ebs_volume" "meridian_data" {
  availability_zone = var.instance_availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"

  encrypted = true

  tags = { Name = "meridian-data" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "meridian_data" {
  device_name = var.data_volume_device_name
  volume_id   = aws_ebs_volume.meridian_data.id
  instance_id = local.instance_id

  # Allow Terraform to detach if the instance is being stopped/started
  # by the orchestrator Lambda — otherwise destroy plans block on a
  # detach that can't run until shutdown.
  stop_instance_before_detaching = true
}
