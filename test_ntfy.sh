#!/data/data/com.termux/files/usr/bin/sh
# Tests the local ntfy connection without contacting the bank.
# Sends a harmless test message; checks that anonymous access is refused.
set -eu
cd "$(dirname "$0")"
. ./.env
: "${NTFY_URL:=http://127.0.0.1:2586}"
: "${NTFY_TOPIC:?NTFY_TOPIC not set in .env}"
: "${NTFY_TOKEN:?NTFY_TOKEN not set in .env}"

printf 'health:        '
curl -s -m 5 "$NTFY_URL/v1/health" || { echo "server not reachable at $NTFY_URL"; exit 1; }
echo

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' -d "anonymous test" "$NTFY_URL/$NTFY_TOPIC")
echo "anonymous post: HTTP $code (expect 403)"

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $NTFY_TOKEN" -H "Title: bankapi test" \
    -d "ntfy connection test" "$NTFY_URL/$NTFY_TOPIC")
echo "token post:     HTTP $code (expect 200)"
[ "$code" = 200 ]
