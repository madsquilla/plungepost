#!/bin/bash
# Pull the latest code from GitHub and restart the dashboard container.
# Never touches .env or the live data/ queues (those aren't in the repo).
set -e
cd "$(dirname "$0")"
echo "Downloading latest code..."
curl -sL https://github.com/madsquilla/skysystems-fb-poster/archive/refs/heads/master.tar.gz \
  | tar xz --strip-components=1
echo "Restarting dashboard..."
docker restart skysystems-dashboard
echo "Done. Updated to latest and restarted."
