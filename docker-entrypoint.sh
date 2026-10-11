#!/bin/sh
set -e

flask db upgrade

# Creates the admin on first boot, re-syncs its password afterwards.
if [ -n "$ADMIN_EMAIL" ] && [ -n "$ADMIN_PASSWORD" ]; then
  flask seed-admin
fi

exec "$@"
