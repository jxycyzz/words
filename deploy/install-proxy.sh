#!/bin/sh
set -eu

mode="${1:-}"
root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
http_target=/opt/runfuda/production/nginx/default.conf
https_target=/opt/runfuda/edge/nginx.conf
stamp=$(date +%Y%m%d-%H%M%S)

install_http() {
  if grep -q '# BEGIN WordLearner HTTP' "$http_target"; then
    return
  fi
  cp "$http_target" "$http_target.words-backup-$stamp"
  {
    printf '\n# BEGIN WordLearner HTTP\n'
    cat "$root_dir/deploy/nginx-http.conf"
    printf '# END WordLearner HTTP\n'
  } >> "$http_target"
  docker exec runfuda-web nginx -t
  docker exec runfuda-web nginx -s reload
}

install_https() {
  if grep -q '# BEGIN WordLearner HTTPS' "$https_target"; then
    return
  fi
  cp "$https_target" "$https_target.words-backup-$stamp"
  python3 - "$https_target" "$root_dir/deploy/nginx-https.conf" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1])
snippet = Path(sys.argv[2]).read_text(encoding='utf-8').rstrip()
text = target.read_text(encoding='utf-8').rstrip()
if not text.endswith('}'):
    raise SystemExit('HTTPS Nginx config has no final http closing brace')
text = text[:-1].rstrip()
target.write_text(
    text + '\n\n    # BEGIN WordLearner HTTPS\n' +
    '\n'.join('    ' + line if line else '' for line in snippet.splitlines()) +
    '\n    # END WordLearner HTTPS\n}\n',
    encoding='utf-8',
)
PY
  docker exec runfuda-edge nginx -t
  docker exec runfuda-edge nginx -s reload
}

case "$mode" in
  http) install_http ;;
  https) install_https ;;
  *) printf 'usage: %s http|https\n' "$0" >&2; exit 2 ;;
esac
