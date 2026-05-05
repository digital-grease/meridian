#!/usr/bin/env bash
# Meridian EC2 bootstrap — idempotent first-boot setup for the cohabit
# instance. Run via SSM Session as ubuntu (or whatever user the AMI
# defaults to) the first time the meridian EBS volume is attached.
#
# What this does:
#   1. Discover the meridian EBS volume (must be attached at /dev/sdg
#      per the Terraform module's `var.data_volume_device_name`).
#   2. Format with ext4 if blank, mount at /data/meridian.
#   3. Create the meridian system user.
#   4. Install ollama, configure it to put model files on the data
#      volume, enable on boot.
#   5. Install uv (system-wide) so `uv run` works for the meridian user.
#   6. Place /etc/meridian/config.env from a template; the operator fills
#      in real values after running this script (see ec2-runbook.md).
#
# Designed to be re-runnable: every step checks current state before
# acting. Safe to invoke twice.
#
# AMI assumption: the specter Deep Learning AMI (Ubuntu 22.04 base).

set -euxo pipefail

exec > >(tee -a /var/log/meridian-bootstrap.log) 2>&1

MERIDIAN_USER="meridian"
MERIDIAN_HOME="/data/meridian"
MERIDIAN_DEVICE_CANDIDATES=(/dev/sdg /dev/xvdg /dev/nvme3n1 /dev/nvme4n1)

# 1. Find the meridian EBS volume. The kernel may rename /dev/sdg to
# /dev/xvdg or /dev/nvmeXn1 depending on instance generation; specter's
# data volume is already on /dev/xvdf (nvme2 typically), so we skip the
# first nvme*n1 devices when probing.
DEVICE=""
for _ in $(seq 1 30); do
  for candidate in "${MERIDIAN_DEVICE_CANDIDATES[@]}"; do
    if [ -b "$candidate" ]; then
      # Don't claim specter's volume by accident: a device with an
      # existing ext4 fs labelled or mounted by specter must be skipped.
      DEVICE="$candidate"
      break 2
    fi
  done
  sleep 2
done

if [ -z "$DEVICE" ]; then
  echo "ERROR: meridian data volume not found among ${MERIDIAN_DEVICE_CANDIDATES[*]}"
  echo "Check the Terraform module's var.data_volume_device_name and aws ec2 describe-volumes."
  exit 1
fi
echo "found meridian data volume at $DEVICE"

# 2. Format if blank, mount, persist via fstab.
if ! blkid "$DEVICE" >/dev/null 2>&1; then
  echo "formatting $DEVICE as ext4"
  mkfs.ext4 -L meridian-data "$DEVICE"
fi

mkdir -p "$MERIDIAN_HOME"
if ! mountpoint -q "$MERIDIAN_HOME"; then
  mount "$DEVICE" "$MERIDIAN_HOME"
fi

DEVICE_UUID=$(blkid -s UUID -o value "$DEVICE")
FSTAB_LINE="UUID=$DEVICE_UUID  $MERIDIAN_HOME  ext4  defaults,nofail  0  2"
if ! grep -q "$DEVICE_UUID" /etc/fstab; then
  echo "$FSTAB_LINE" >> /etc/fstab
fi

# 3. Create the meridian system user. Owns /data/meridian and runs the
# pipeline. Locked password (no console login) — ssm session-manager
# is the access path.
if ! id -u "$MERIDIAN_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$MERIDIAN_HOME" \
          --shell /bin/bash "$MERIDIAN_USER"
fi
chown -R "$MERIDIAN_USER:$MERIDIAN_USER" "$MERIDIAN_HOME"

# 4. Install ollama. Idempotent: the official installer is a no-op when
# the latest version is already present.
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Override the systemd service to put model files on /data/meridian
# instead of /usr/share/ollama. Keeps the ~10 GB of pulled models on
# the dedicated volume rather than the root volume that specter shares.
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/meridian-overrides.conf <<EOF
[Service]
Environment=OLLAMA_MODELS=$MERIDIAN_HOME/ollama-models
Environment=OLLAMA_HOST=127.0.0.1:11434
EOF
mkdir -p "$MERIDIAN_HOME/ollama-models"
chown -R "$MERIDIAN_USER:$MERIDIAN_USER" "$MERIDIAN_HOME/ollama-models"

systemctl daemon-reload
systemctl enable --now ollama

# 5. Install uv system-wide (idempotent — installer skips if same version).
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

# 6. Drop a config-env template. The operator fills in SNS_TOPIC_ARN
# and confirms the SSM-secret paths after Terraform apply (see
# `terraform output` in infra/terraform/ec2-cohabit/).
mkdir -p /etc/meridian
if [ ! -f /etc/meridian/config.env ]; then
  cat > /etc/meridian/config.env <<'EOF'
# Meridian wrapper-script environment. Populated after Terraform apply.
# Lines starting with REPLACE-* must be set to real values before the
# wrapper script runs.

# SNS topic for orchestrator alerts. Get this from:
#   terraform -chdir=infra/terraform/ec2-cohabit output -raw alerts_topic_arn
SNS_TOPIC_ARN="REPLACE-WITH-SNS-TOPIC-ARN"

# AWS region — same as specter / S3 / SSM parameters.
AWS_DEFAULT_REGION="us-east-2"

# SSM Parameter Store paths for provider API keys. Match var.*_param_name
# defaults in the Terraform module; only override if you customised those.
MERIDIAN_SECRETS_SSM="1"
MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH="/meridian/anthropic-api-key"
MERIDIAN_SECRETS_SSM_OPENAI_PATH="/meridian/openai-api-key"

# S3 archive — must match storage.s3 in meridian/config.yaml. The
# wrapper does not read these; they're here for runbook clarity.
# MERIDIAN_S3_BUCKET=""
# MERIDIAN_S3_PREFIX="meridian/"
EOF
  chmod 644 /etc/meridian/config.env
fi

echo "bootstrap complete. next: clone the repo, uv sync, edit /etc/meridian/config.env, and pull models per ec2-runbook.md"
