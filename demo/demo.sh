#!/usr/bin/env bash
#
# End-to-end demo of the webhook gateway. Bring the stack up first, ideally with
# a short retry budget so scenario (d) reaches the dead-letter quickly:
#
#   MAX_ATTEMPTS=4 BASE_BACKOFF_SECONDS=1 docker compose up --build
#
# then in another terminal:
#
#   ./demo/demo.sh
#
# It exercises: (a) a valid event, (b) the same event again (dedupe), (c) a
# forged-signature event (401), and (d) an event whose downstream always 500s
# (retry -> dead), then reads the resulting event states back out.
set -euo pipefail

API="${API:-http://localhost:8000}"
ADMIN="${ADMIN_TOKEN:-change-me-admin-token}"
GOOD_SECRET="demo-good-secret"
BAD_SECRET="demo-bad-secret"

hr() { printf '\n\033[1m%s\033[0m\n' "== $* =="; }
sign() { printf '%s' "$2" | openssl dgst -sha256 -hmac "$1" | sed 's/^.*= //'; }
post_event() { # source secret body
  curl -s -w "\n[HTTP %{http_code}]\n" -X POST "$API/v1/webhooks/$1" \
    -H "X-Signature: sha256=$(sign "$2" "$3")" -H "Content-Type: application/json" \
    --data-binary "$3"
}

hr "0. health check (real DB round-trip)"
curl -s -w "\n[HTTP %{http_code}]\n" "$API/healthz"

hr "1. register sources"
# The downstream URLs are resolved by the WORKER inside the compose network:
#   echo/anything -> 200,  echo/status/500 -> 500 (httpbin).
curl -s -o /dev/null -X POST "$API/v1/sources" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"demo\",\"signing_secret\":\"$GOOD_SECRET\",\"downstream_url\":\"http://echo:80/anything\"}" || true
curl -s -o /dev/null -X POST "$API/v1/sources" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"demo-bad\",\"signing_secret\":\"$BAD_SECRET\",\"downstream_url\":\"http://echo:80/status/500\"}" || true
echo "registered sources 'demo' and 'demo-bad'"

VALID='{"event_id":"demo-evt-1","resource_key":"order:1","status_ordinal":1,"data":{"amount":100}}'

hr "(a) valid event -> accepted"
post_event demo "$GOOD_SECRET" "$VALID"

hr "(b) same event again -> duplicate (idempotent, still one row)"
post_event demo "$GOOD_SECRET" "$VALID"

hr "(c) forged signature -> 401"
curl -s -w "\n[HTTP %{http_code}]\n" -X POST "$API/v1/webhooks/demo" \
  -H "X-Signature: sha256=deadbeefdeadbeef" -H "Content-Type: application/json" \
  --data-binary "$VALID"

hr "(d) downstream always 500 -> retried, then dead-lettered"
POISON='{"event_id":"demo-poison-1","resource_key":"order:2","status_ordinal":1}'
post_event demo-bad "$BAD_SECRET" "$POISON"
echo "polling until it lands in the dead-letter queue (Ctrl-C to stop)..."
for _ in $(seq 1 90); do
  sleep 1
  st=$(curl -s "$API/v1/events?source=demo-bad" \
       | python3 -c "import sys,json;e=json.load(sys.stdin)['events'];print(e[0]['status'] if e else '')" 2>/dev/null || echo "")
  printf '  status=%s\n' "$st"
  [ "$st" = "dead" ] && break
done

hr "final: delivered events"
curl -s "$API/v1/events?status=delivered" \
  | python3 -c "import sys,json;[print(' ',e['provider_event_id'],e['status'],'attempts='+str(e['attempts'])) for e in json.load(sys.stdin)['events']]"

hr "final: dead-letter queue (?status=dead)"
curl -s "$API/v1/events?status=dead" \
  | python3 -c "import sys,json;[print(' ',e['provider_event_id'],e['status'],'attempts='+str(e['attempts']),'|',e['last_error']) for e in json.load(sys.stdin)['events']]"

cat <<'EOF'

Tip: replay a dead event once you've "fixed" the downstream:
  curl -X POST http://localhost:8000/v1/events/<EVENT_ID>/replay
EOF
