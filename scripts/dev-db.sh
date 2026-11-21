#!/bin/bash
# Creates and starts a private PostgreSQL instance for development inside this project
# (./.pgdata, port 5433, local connections only). Needs PostgreSQL installed (brew install postgresql).
set -euo pipefail
# macOS: PostgreSQL refuses to start without a valid locale.
export LC_ALL="${LC_ALL:-en_US.UTF-8}" LANG="${LANG:-en_US.UTF-8}"
cd "$(dirname "$0")/.."
DATA="$PWD/.pgdata"
PORT="${PGPORT:-5433}"
DB="${POSTGRES_DB:-proglint_pulse}"
USER_NAME="${POSTGRES_USER:-proglint}"

if [ ! -f "$DATA/PG_VERSION" ]; then
  echo "Initializing PostgreSQL cluster in $DATA"
  initdb -D "$DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
  echo "listen_addresses = '127.0.0.1'" >> "$DATA/postgresql.conf"
  echo "port = $PORT" >> "$DATA/postgresql.conf"
  echo "unix_socket_directories = '$DATA'" >> "$DATA/postgresql.conf"
fi

if ! pg_ctl -D "$DATA" status >/dev/null 2>&1; then
  pg_ctl -D "$DATA" -l "$DATA/server.log" -w start >/dev/null
  echo "PostgreSQL started on 127.0.0.1:$PORT"
else
  echo "PostgreSQL already running on 127.0.0.1:$PORT"
fi

psql -h 127.0.0.1 -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$USER_NAME'" | grep -q 1 \
  || psql -h 127.0.0.1 -p "$PORT" -U postgres -qc "CREATE ROLE $USER_NAME LOGIN CREATEDB PASSWORD 'proglint'"
psql -h 127.0.0.1 -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 \
  || psql -h 127.0.0.1 -p "$PORT" -U postgres -qc "CREATE DATABASE $DB OWNER $USER_NAME"
echo "Database '$DB' ready (user '$USER_NAME'). Stop with: pg_ctl -D .pgdata stop"
