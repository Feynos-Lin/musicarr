#!/bin/sh
# Run as PUID/PGID (like linuxserver images) so files land with the right owner.
set -e
if [ "$(id -u)" = "0" ]; then
  groupadd -o -g "$PGID" musicarr 2>/dev/null || true
  useradd -o -u "$PUID" -g "$PGID" -M -s /bin/sh musicarr 2>/dev/null || true
  mkdir -p /config
  chown -R "$PUID:$PGID" /config
  exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
