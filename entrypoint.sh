#!/bin/sh
set -e

# If EE_KEY_JSON is set (Cloud Run secret), write it to a temp file and set EE_KEY_FILE
if [ -n "$EE_KEY_JSON" ] && [ -z "$EE_KEY_FILE" ]; then
  echo "$EE_KEY_JSON" > /tmp/gee_key.json
  chmod 600 /tmp/gee_key.json
  export EE_KEY_FILE=/tmp/gee_key.json
fi

# Start server
exec python server.py