#!/bin/sh
set -e

# If EE_KEY_JSON is set (Cloud Run/Render secret), write it to a temp file and set EE_KEY_FILE
if [ -n "$EE_KEY_JSON" ] && [ -z "$EE_KEY_FILE" ]; then
  echo "$EE_KEY_JSON" > /tmp/gee_key.json
  chmod 600 /tmp/gee_key.json
  export EE_KEY_FILE=/tmp/gee_key.json
  echo "entrypoint: wrote EE_KEY_JSON to /tmp/gee_key.json"
fi

if [ -z "$EE_SERVICE_ACCOUNT" ]; then
  echo "entrypoint: WARNING - EE_SERVICE_ACCOUNT is not set. /gee/* routes will return 503 until it is."
fi
if [ -z "$EE_KEY_FILE" ]; then
  echo "entrypoint: WARNING - EE_KEY_FILE/EE_KEY_JSON is not set. /gee/* routes will return 503 until it is."
fi

# Start server (server.py itself no longer crashes on missing EE env vars,
# so the container will still start and answer /health)
exec python server.py
