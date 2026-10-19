#!/usr/bin/env bash
# Create a private CA and a server certificate for the broker (TLS on 8883 and WSS on 9443).
#   ./scripts/setup-tls.sh           create if missing
#   ./scripts/setup-tls.sh --renew   new server certificate (same CA)
#   ./scripts/setup-tls.sh --new-ca  new CA + server certificate (clients must get the new ca.crt)
# certs/ca.crt is what clients use to verify the broker; certs/ca.key never leaves this folder.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs
chmod 700 certs

if [[ "${1:-}" == "--new-ca" ]]; then rm -f certs/ca.key certs/ca.crt certs/server.*; fi
if [[ "${1:-}" == "--renew" ]]; then rm -f certs/server.*; fi

if [[ ! -f certs/ca.key ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -sha256 \
    -keyout certs/ca.key -out certs/ca.crt -subj "/CN=amr-mqtt-monitor local CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  echo "created CA certs/ca.crt"
fi

if [[ ! -f certs/server.crt ]]; then
  openssl req -newkey rsa:2048 -nodes -keyout certs/server.key -out certs/server.csr \
    -subj "/CN=localhost" 2>/dev/null
  openssl x509 -req -in certs/server.csr -CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial \
    -days 825 -sha256 -out certs/server.crt -extfile <(printf '%s\n' \
      "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" \
      "keyUsage=critical,digitalSignature,keyEncipherment" "extendedKeyUsage=serverAuth") 2>/dev/null
  rm -f certs/server.csr
  echo "created server certificate (valid 825 days, localhost / 127.0.0.1)"
fi
chmod 600 certs/*.key

# the broker reads its copies from mosquitto/secrets (owned by its uid, like passwd/acl)
mkdir -p mosquitto/secrets
docker run --rm --user root -v "$PWD/certs:/c:ro" -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c '
  set -e; umask 077
  cp /c/ca.crt /c/server.crt /c/server.key /m/secrets/
  chown 1883:1883 /m/secrets/ca.crt /m/secrets/server.crt /m/secrets/server.key
  chmod 600 /m/secrets/*'
touch mosquitto/.tls-ready

openssl x509 -in certs/server.crt -noout -subject -enddate
if docker compose ps --status running --services 2>/dev/null | grep -qx mosquitto; then
  docker compose restart mosquitto > /dev/null && echo "mosquitto restarted with new certificate"
fi
