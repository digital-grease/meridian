#!/usr/bin/env bash
# Install a second, version-pinned ollama engine next to the system one.
#
# Why two engines: the `digest` pin in meridian/config.yaml fixes a local
# model's weights, not the engine that runs them. Upgrading the system
# ollama (0.23.1 on the cohabit instance) to serve newer models could
# shift llama3.2:3b's outputs through new kernels or a new sampler with
# byte-identical weights, which would put a step in the noise-floor
# series the control group exists to keep flat. So the system ollama on
# :11434 is never upgraded, and models that need a newer engine run on
# this one, on :11435, from its own model store. Runners pick an engine
# with `base_url` in config.yaml.
#
# Bumping VERSION below is an engine change for every model this engine
# serves. Treat it like a re-pinned digest: audit it, and disclose it.
#
# Idempotent. Run as root (ec2-bootstrap.sh calls it; it can also be run
# on its own via SSM without touching the system ollama).

set -euo pipefail

VERSION="v0.40.6"
# From the release's sha256sum.txt, for ollama-linux-amd64.tar.zst.
SHA256="c164ab8b1b28e2a247c29719b8e404c423c5b83ae64c3ab4e59d50c7ff7fc5e0"
PORT="11435"
UNIT="ollama-pinned"

MERIDIAN_HOME="/data/meridian"
PREFIX="$MERIDIAN_HOME/engines/ollama-$VERSION"
MODELS="$MERIDIAN_HOME/ollama-models-pinned"
URL="https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tar.zst"

# The tarball is ~1.4 GB (it carries its own CUDA libraries), so it is
# fetched onto the data volume rather than the shared root volume.
if [ ! -x "$PREFIX/bin/ollama" ]; then
  tmp="$(mktemp -d "$MERIDIAN_HOME/.ollama-dl.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp/ollama.tar.zst" "$URL"
  echo "$SHA256  $tmp/ollama.tar.zst" | sha256sum -c -
  mkdir -p "$tmp/x"
  tar --zstd -xf "$tmp/ollama.tar.zst" -C "$tmp/x"
  mkdir -p "$(dirname "$PREFIX")"
  mv "$tmp/x" "$PREFIX"
fi

mkdir -p "$MODELS"
chown -R ollama:ollama "$MODELS"

# The CLI panics without $HOME (SSM's shell has none), and with the
# default OLLAMA_HOST it would report the system engine's version.
if ! env HOME="${HOME:-/root}" OLLAMA_MODELS="$MODELS" OLLAMA_HOST="127.0.0.1:$PORT" \
     "$PREFIX/bin/ollama" --version 2>&1 | grep -qF "${VERSION#v}"; then
  echo "ERROR: $PREFIX/bin/ollama does not report version ${VERSION#v}"
  exit 1
fi

cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Ollama $VERSION (pinned engine for meridian, port $PORT)
After=network-online.target

[Service]
ExecStart=$PREFIX/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment=OLLAMA_HOST=127.0.0.1:$PORT
Environment=OLLAMA_MODELS=$MODELS

[Install]
WantedBy=default.target
EOF

systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; then
    echo "$UNIT up on :$PORT: $(curl -fsS "http://127.0.0.1:$PORT/api/version")"
    exit 0
  fi
  sleep 1
done
echo "ERROR: $UNIT did not answer on :$PORT"
exit 1
