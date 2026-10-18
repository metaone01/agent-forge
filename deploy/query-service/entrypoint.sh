#!/bin/sh
set -eu
python -m agent_forge_service.preflight --catalog /app --runtime /runtime
exec python -m agent_forge_service.app --catalog /app --runtime /runtime --host 0.0.0.0 --port 8791 "$@"
