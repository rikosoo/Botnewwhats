#!/bin/bash
# Backup diário dos bancos (bot + chatwoot) para o S3.
# Requer: bucket criado e a instância com IAM role que permita s3:PutObject nele.
# Agende com:  crontab -e  ->  30 3 * * * /opt/botnefro/deploy/aws/backup.sh >> /var/log/botnefro-backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")"
BUCKET="${BACKUP_BUCKET:?defina BACKUP_BUCKET, ex.: export BACKUP_BUCKET=meu-bucket-backups}"
STAMP=$(date +%Y-%m-%d_%H%M)
FILE="/tmp/pg_${STAMP}.sql.gz"

docker compose exec -T postgres pg_dumpall -U postgres | gzip > "$FILE"
aws s3 cp "$FILE" "s3://${BUCKET}/postgres/pg_${STAMP}.sql.gz" --storage-class STANDARD_IA
rm -f "$FILE"
echo "$(date) backup ok: pg_${STAMP}.sql.gz"
