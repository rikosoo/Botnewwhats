#!/bin/bash
# Prepara uma instância Ubuntu 24.04 (Lightsail ou EC2) do zero. Com o repositório já clonado em /opt/botnefro:
#   sudo bash /opt/botnefro/deploy/aws/bootstrap.sh
set -euo pipefail

REPO_URL="${1:-}"
APP_DIR=/opt/botnefro

timedatectl set-timezone America/Sao_Paulo

# Swap de 2 GB: o Chatwoot usa bastante memória na compilação/inicialização.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
apt-get update -y
apt-get install -y git unattended-upgrades
usermod -aG docker ubuntu || true

if [ -n "$REPO_URL" ] && [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
  chown -R ubuntu:ubuntu "$APP_DIR"
fi

chmod +x "$APP_DIR"/deploy/aws/*.sh "$APP_DIR"/deploy/aws/postgres-init/*.sh 2>/dev/null || true

cat <<EOF

Pronto. Próximos passos (como usuário ubuntu):
  cd $APP_DIR/deploy/aws
  cp .env.example .env && nano .env
  docker compose run --rm rails bundle exec rails db:chatwoot_prepare
  docker compose up -d --build
EOF
