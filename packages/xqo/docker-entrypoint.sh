#!/bin/sh
# Starts as root, makes the data dir writable for the app user (uid 1000), then runs xqo as that user.
# Bind mounts keep the host's owner: on Linux the folder may belong to another uid, and on Docker Desktop
# (Windows / macOS) it can show up as root-owned and chown may not work. When the app user still cannot write
# there, xqo runs as root inside the container instead of crashing (XQO_RUN_AS=root forces that, app forbids it).
set -e
data="${XQO_DATA:-/data}"
want="${XQO_RUN_AS:-auto}"

if [ "$(id -u)" != "0" ]; then exec "$@"; fi   # started with --user: nothing to fix

mkdir -p "$data" 2>/dev/null || true
can_write() { setpriv --reuid=1000 --regid=1000 --clear-groups sh -c "touch '$1/.xqo-write-test' && rm -f '$1/.xqo-write-test'" 2>/dev/null; }

if [ "$want" != "root" ] && ! can_write "$data"; then
  chown 1000:1000 "$data" 2>/dev/null || true
  can_write "$data" || chmod u+rwx,g+rwx,o+rwx "$data" 2>/dev/null || true
fi

if [ "$want" = "root" ] || { [ "$want" = "auto" ] && ! can_write "$data"; }; then
  [ "$want" = "root" ] || echo "xqo: $data is not writable for the app user (uid 1000) and its owner cannot be changed; running as root inside the container" >&2
  export HOME=/root
  exec "$@"
fi

# keep extra groups Docker gave the container (group_add, e.g. the docker socket's group), drop root's own
groups=$(id -G | tr ' ' '\n' | grep -vx 0 | paste -sd, -)
export HOME=/home/app USER=app
if [ -n "$groups" ]; then
  exec setpriv --reuid=1000 --regid=1000 --groups "1000,$groups" "$@"
else
  exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi
