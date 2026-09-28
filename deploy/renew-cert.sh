#!/bin/sh
set -eu

docker run --rm \
  -v /opt/runfuda/acme-webroot:/var/www/certbot \
  -v /opt/runfuda/production/certs/siss:/etc/letsencrypt \
  certbot/certbot:latest renew --webroot -w /var/www/certbot --quiet

docker exec runfuda-edge nginx -t
docker exec runfuda-edge nginx -s reload
