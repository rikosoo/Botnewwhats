#!/bin/bash
# Atualiza o bot para a última versão do repositório (as migrations rodam no start do container).
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build bot
docker compose ps
