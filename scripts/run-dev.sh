#!/bin/bash
# Local development: start the private PostgreSQL, apply migrations, run Django.
#   ./scripts/run-dev.sh            -> http://127.0.0.1:8000 (this computer only)
#   ./scripts/run-dev.sh 0.0.0.0    -> also reachable from other computers on the network
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/dev-db.sh
.venv/bin/python manage.py migrate --noinput
exec .venv/bin/python manage.py runserver "${1:-127.0.0.1}:8000"
