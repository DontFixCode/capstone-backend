#!/bin/sh
set -eu
umask 077

exec 9>/var/lib/bus-auth/renew.lock
flock -n 9 || exit 0

compose=/home/gmlrud/bus-transfer-supabase/deployment/public-auth/compose.yaml
certificate=/var/lib/bus-auth/certificates/live/bus-public-ip/fullchain.pem
loaded=/var/lib/bus-auth/loaded-certificate.sha256
renew_status=0

docker run --rm --name bus-auth-certbot-renew \
  --mount type=bind,src=/var/lib/bus-auth/certificates,dst=/etc/letsencrypt \
  --mount type=bind,src=/var/lib/bus-auth/work,dst=/var/lib/letsencrypt \
  --mount type=bind,src=/var/lib/bus-auth/logs,dst=/var/log/letsencrypt \
  --mount type=bind,src=/var/lib/bus-auth/acme,dst=/var/www/acme \
  certbot/certbot:v5.4.0@sha256:c23159d30afdd9c97960578aa4654f5901de6cae394958f894074dedd55e599d \
  renew --cert-name bus-public-ip --non-interactive --quiet --no-random-sleep-on-renew || renew_status=$?

current=$(sha256sum "$certificate" | cut -d ' ' -f 1)
previous=$(cat "$loaded" 2>/dev/null || true)
if [ "$current" != "$previous" ]; then
  reload_status=0
  for service in proxy studio-proxy; do
    if docker compose -f "$compose" exec -T "$service" nginx -t &&
       docker compose -f "$compose" exec -T "$service" nginx -s reload; then
      :
    else
      reload_status=1
    fi
  done
  [ "$reload_status" -eq 0 ] || exit "$reload_status"
  printf '%s\n' "$current" > "$loaded.tmp"
  mv -f "$loaded.tmp" "$loaded"
fi
exit "$renew_status"
