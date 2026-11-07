#!/usr/bin/env bash
# Install mls-grab + its web front end on a Debian/Ubuntu server running nginx and PHP-FPM.
#
#   sudo ./deploy/install.sh [server_name] [web_user]
#
#   server_name  hostname for the nginx site (default: _ = any)
#   web_user     login for the password prompt (default: admin)
#
# Re-running is safe: it updates the code and keeps photos, password and config.
set -euo pipefail

SERVER_NAME="${1:-_}"
WEB_USER="${2:-admin}"
APP_DIR=/opt/mls-grab
WEB_ROOT=/var/www/mls-grab
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "==> packages"
command -v python3 >/dev/null || apt-get install -y python3
command -v nginx   >/dev/null || apt-get install -y nginx
PHP_SOCK="$(ls /run/php/php*-fpm.sock 2>/dev/null | sort -V | tail -1 || true)"
if [ -z "$PHP_SOCK" ]; then
  apt-get install -y php-fpm
  PHP_SOCK="$(ls /run/php/php*-fpm.sock | sort -V | tail -1)"
fi
WWW_USER="$(ps -o user= -C php-fpm8.3,php-fpm8.2,php-fpm8.1,php-fpm8.0,php-fpm7.4 2>/dev/null | grep -v root | head -1 || true)"
WWW_USER="${WWW_USER:-www-data}"
echo "    python: $(python3 --version)  php-fpm socket: $PHP_SOCK  runs as: $WWW_USER"

echo "==> code -> $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/mls_grab"
cp -r "$SRC/mls_grab" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$APP_DIR/bin"
cat > "$APP_DIR/bin/mls-grab" <<SH
#!/bin/sh
PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m mls_grab "\$@"
SH
chmod +x "$APP_DIR/bin/mls-grab"
ln -sf "$APP_DIR/bin/mls-grab" /usr/local/bin/mls-grab

echo "==> web root -> $WEB_ROOT"
mkdir -p "$WEB_ROOT/jobs"
cp "$SRC/deploy/web/index.php" "$WEB_ROOT/index.php"
if [ ! -f "$WEB_ROOT/config.php" ]; then
  cat > "$WEB_ROOT/config.php" <<PHPCFG
<?php
return [
    'python'   => '$(command -v python3)',
    'app_dir'  => '$APP_DIR',
    'jobs_dir' => '$WEB_ROOT/jobs',
    'jobs_url' => 'jobs',
    'timeout'  => 240,
];
PHPCFG
fi
chown -R root:root "$WEB_ROOT"
chown -R "$WWW_USER":"$WWW_USER" "$WEB_ROOT/jobs"
chmod 755 "$WEB_ROOT" && chmod 644 "$WEB_ROOT"/*.php

echo "==> password"
if [ ! -f /etc/nginx/mls-grab.htpasswd ]; then
  PASS="$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  printf '%s:%s\n' "$WEB_USER" "$(openssl passwd -apr1 "$PASS")" > /etc/nginx/mls-grab.htpasswd
  chown root:www-data /etc/nginx/mls-grab.htpasswd   # nginx workers run as www-data on Debian/Ubuntu
  chmod 640 /etc/nginx/mls-grab.htpasswd
  echo "    login: $WEB_USER   password: $PASS   (save this; change with: htpasswd /etc/nginx/mls-grab.htpasswd $WEB_USER)"
else
  echo "    keeping existing /etc/nginx/mls-grab.htpasswd"
fi

echo "==> nginx site"
sed -e "s#__SERVER_NAME__#$SERVER_NAME#" -e "s#__WEB_ROOT__#$WEB_ROOT#" \
    -e "s#__PHP_FPM__#unix:$PHP_SOCK#g" "$SRC/deploy/nginx-mls-grab.conf" \
    > /etc/nginx/sites-available/mls-grab
ln -sf /etc/nginx/sites-available/mls-grab /etc/nginx/sites-enabled/mls-grab
nginx -t
systemctl reload nginx

echo "==> cleanup job (deletes downloads older than 14 days)"
cat > /etc/cron.daily/mls-grab-cleanup <<CRON
#!/bin/sh
find $WEB_ROOT/jobs -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
CRON
chmod +x /etc/cron.daily/mls-grab-cleanup

echo "==> self-test"
sudo -u "$WWW_USER" env PYTHONPATH="$APP_DIR" python3 -m mls_grab --version

echo
echo "Done. Open http://${SERVER_NAME/_/<this-server>}/  (add HTTPS with: certbot --nginx -d <your-domain>)"
echo "CLI: mls-grab '1 Tribou Drive, Paris, ME 04281'"
