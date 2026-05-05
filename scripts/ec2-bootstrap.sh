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
MERIDIAN_FS_LABEL="meridian-data"
# Set MERIDIAN_VOLUME_ID env var (e.g. "vol-08d8823e42d26e038") to match
# by exact EBS volume id; otherwise we fall back to "first unformatted
# block device" which is safe because specter's volume is already
# formatted (LVM2_member or ext4) and gets skipped automatically.
MERIDIAN_VOLUME_ID="${MERIDIAN_VOLUME_ID:-}"

# 1. Find the meridian EBS volume. Three-pass strategy, in order of
# specificity. Each pass looks at the live state; we sleep between
# attempts because the volume may not be visible immediately after
# attach.
#
# Pass 1: an already-formatted ext4 volume with our label exists. This
#   is the steady-state on every run after the first.
# Pass 2: caller specified MERIDIAN_VOLUME_ID. Match by NVMe serial.
# Pass 3: pick the first /dev/nvme*n1 that has no filesystem at all.
#   Safe because specter's volume always has either LVM2 or ext4 on it
#   and the root volume has its own fs; our brand-new EBS volume is the
#   only unformatted candidate on first boot.
find_meridian_device() {
  local serial="" cand byid label_dev
  if [ -n "$MERIDIAN_VOLUME_ID" ]; then
    serial="$(echo "$MERIDIAN_VOLUME_ID" | tr -d -)"
  fi

  for _ in $(seq 1 30); do
    # Pass 1: by FS label.
    label_dev="$(blkid -L "$MERIDIAN_FS_LABEL" 2>/dev/null || true)"
    if [ -n "$label_dev" ] && [ -b "$label_dev" ]; then
      echo "$label_dev"; return 0
    fi

    # Pass 2: by NVMe serial (matches EBS volume id with dashes stripped).
    if [ -n "$serial" ]; then
      byid="$(lsblk -dno NAME,SERIAL 2>/dev/null \
        | awk -v s="$serial" '$2 == s { print "/dev/" $1; exit }')"
      if [ -n "$byid" ] && [ -b "$byid" ]; then
        echo "$byid"; return 0
      fi
    fi

    # Pass 3: first unformatted block device (excluding partitions).
    for cand in /dev/nvme0n1 /dev/nvme1n1 /dev/nvme2n1 /dev/nvme3n1 \
                /dev/nvme4n1 /dev/nvme5n1 /dev/nvme6n1 /dev/nvme7n1; do
      [ -b "$cand" ] || continue
      # Skip mounted devices and devices that already have any fs/PV.
      if findmnt -nrS "$cand" >/dev/null 2>&1; then continue; fi
      if blkid "$cand" >/dev/null 2>&1; then continue; fi
      echo "$cand"; return 0
    done

    sleep 2
  done
  return 1
}

DEVICE="$(find_meridian_device || true)"
if [ -z "$DEVICE" ]; then
  echo "ERROR: meridian data volume not found."
  echo "Diagnose with: lsblk -dno NAME,SERIAL,SIZE,TYPE"
  echo "If the volume is attached but already formatted with a different label,"
  echo "set MERIDIAN_VOLUME_ID=vol-... in the environment and re-run."
  exit 1
fi
echo "found meridian data volume at $DEVICE"

# Refuse to claim a device that has someone else's filesystem on it
# (e.g. specter's LVM2 PV) — only ext4 with our label, or a blank
# device, is acceptable. mkfs.ext4 below uses -L to stamp our label.
existing_fs="$(blkid -s TYPE -o value "$DEVICE" 2>/dev/null || true)"
existing_label="$(blkid -s LABEL -o value "$DEVICE" 2>/dev/null || true)"
if [ -n "$existing_fs" ] && [ "$existing_fs" != "ext4" ]; then
  echo "ERROR: $DEVICE has filesystem type '$existing_fs' (label '$existing_label')."
  echo "Refusing to mount — looks like another project's volume."
  exit 1
fi
if [ -n "$existing_label" ] && [ "$existing_label" != "$MERIDIAN_FS_LABEL" ]; then
  echo "ERROR: $DEVICE has filesystem label '$existing_label' (expected '$MERIDIAN_FS_LABEL' or blank)."
  echo "Refusing to mount — looks like another project's volume."
  exit 1
fi

# 2. Format if blank, mount, persist via fstab.
if [ -z "$existing_fs" ]; then
  echo "formatting $DEVICE as ext4 with label $MERIDIAN_FS_LABEL"
  mkfs.ext4 -L "$MERIDIAN_FS_LABEL" "$DEVICE"
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
