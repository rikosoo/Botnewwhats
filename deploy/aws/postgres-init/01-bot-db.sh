#!/bin/bash
# Roda só na primeira inicialização do volume do Postgres: cria o banco e o usuário do bot.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
	CREATE USER bot WITH PASSWORD '${BOT_DB_PASSWORD}';
	CREATE DATABASE bot OWNER bot;
SQL
