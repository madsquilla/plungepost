#!/bin/bash
# One-time (re)create of the dashboard container with every persistent mount.
#
# Run this from the repo folder on Unraid whenever the container's mounts need
# to change (day-to-day updates still use update.sh, which only restarts):
#
#     bash recreate-dashboard.sh
#
# Mounts:
#   src/ assets/ template/  -> live code, so update.sh tarball drops take
#     web/                     effect on restart without rebuilding the image.
#                              web/ holds the phone app (/app); without this
#                              mount, update.sh pulls a new phone app that the
#                              container never serves.
#   data/                   -> legacy queues + the migration source
#   tenants/                -> EVERY account's brand, themes, logo, Facebook
#                              token, queues, and cards. Without this mount,
#                              all accounts are wiped when the container is
#                              recreated.
#   logs/                   -> rotating file logs
set -e
cd "$(dirname "$0")"

echo "Removing old dashboard container (if any)..."
docker rm -f plungepost-dashboard 2>/dev/null || true

echo "Creating dashboard container..."
docker run -d --name plungepost-dashboard --restart unless-stopped \
  --env-file .env \
  -e TZ=America/Chicago \
  -l net.unraid.docker.managed="dockerman" \
  -l net.unraid.docker.icon="https://raw.githubusercontent.com/madsquilla/plungepost/master/assets/icon.png" \
  -l net.unraid.docker.webui="http://${UNRAID_IP:-10.0.0.38}:8095/" \
  -p 8095:8080 \
  -v "$PWD/src":/app/src \
  -v "$PWD/assets":/app/assets \
  -v "$PWD/template":/app/template \
  -v "$PWD/web":/app/web \
  -v "$PWD/data":/app/data \
  -v "$PWD/tenants":/app/tenants \
  -v "$PWD/logs":/app/logs \
  --entrypoint python \
  plungepost:latest src/webapp.py

echo "Done."
echo "  Dashboard: http://<unraid-ip>:8095"
echo "  Phone app: http://<unraid-ip>:8095/app"
