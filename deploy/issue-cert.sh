#!/bin/sh
set -eu

docker run --rm \
  -v /opt/runfuda/acme-webroot:/var/www/certbot \
  -v /opt/runfuda/production/certs/siss:/etc/letsencrypt \
  certbot/certbot:latest certonly \
  --webroot -w /var/www/certbot \
  --cert-name words.rfdsx.online \
  -d words.rfdsx.online \
  --non-interactive --agree-tos --keep-until-expiring
