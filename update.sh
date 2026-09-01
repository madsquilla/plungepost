#!/bin/bash
# Pull the latest code from GitHub and restart the dashboard container.
# Never touches .env or the live data/ queues (those aren't in the repo).
set -e
cd "$(dirname "$0")"
echo "Downloading latest code..."
curl -sL https://github.com/madsquilla/plungepost/archive/refs/heads/master.tar.gz \
  | tar xz --strip-components=1
# The phone app (/app) is served from web/, which older containers do not
# mount -- they would serve a stale copy, or none at all. Catch that here
# rather than letting it show up as a broken page on your phone.
if ! docker inspect plungepost-dashboard \
     --format '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null \
     | grep -q '/app/web'; then
  echo
  echo "NOTE: this container has no /app/web mount, so the phone app will not"
  echo "      pick up updates. Run this once to add it:"
  echo
  echo "          bash recreate-dashboard.sh"
  echo
fi

echo "Restarting dashboard..."
docker restart plungepost-dashboard
echo "Done. Updated to latest and restarted."
echo "Phone app: http://<unraid-ip>:8095/app"
