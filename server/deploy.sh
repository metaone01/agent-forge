#!/usr/bin/env bash
# Server-side install for agent.ratapi.com.
#
# Assumes the build artifacts have been copied to the server, e.g.:
#   /tmp/agent-forge-deploy/pages-staging/   (static shell)
#   /tmp/agent-forge-deploy/catalog.sqlite  (query database)
#   /tmp/agent-forge-deploy/api.py          (query API)
#
# Usage (on the server, from the repo checkout or the unpacked deploy dir):
#   bash server/deploy.sh /tmp/agent-forge-deploy
set -euo pipefail

SRC="${1:-/tmp/agent-forge-deploy}"
APP=/opt/agent-forge
WEB=/var/www/agent
PORT=8791

echo "==> installing API artifacts to $APP"
sudo mkdir -p "$APP"
sudo cp "$SRC/api.py" "$APP/api.py"
sudo cp "$SRC/catalog.sqlite" "$APP/catalog.sqlite"
sudo chown -R ubuntu:ubuntu "$APP"

echo "==> publishing static shell to $WEB"
sudo mkdir -p "$WEB"
sudo rsync -a --delete "$SRC/pages-staging/" "$WEB/"

echo "==> installing systemd unit"
sudo cp "$SRC/agent-forge-api.service" /etc/systemd/system/agent-forge-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-forge-api.service
sudo systemctl restart agent-forge-api.service

echo "==> installing nginx site"
sudo cp "$SRC/nginx-agent-forge.conf" /etc/nginx/sites-available/agent
sudo ln -sf /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/agent
sudo nginx -t
sudo systemctl reload nginx

echo "==> health check"
curl -fsS "http://127.0.0.1:${PORT}/api/health" && echo
echo "done: https://agent.ratapi.com/"
