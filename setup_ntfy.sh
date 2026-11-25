#!/data/data/com.termux/files/usr/bin/sh
# One-time setup of a private ntfy server in Termux. It listens on loopback
# only; the iPhones reach it through a Cloudflare Tunnel public hostname that
# routes to http://127.0.0.1:2586 (see README). Run inside Termux (not
# proot-distro) from the project directory, with that public URL:
#   NTFY_BASE_URL=https://ntfy.example.com sh setup_ntfy.sh
# Writes NTFY_URL, NTFY_TOPIC and NTFY_TOKEN to ./.env without printing them.
# An existing ~/.config/ntfy/server.yml is never overwritten.
set -eu

cd "$(dirname "$0")"
[ -f .env ] || { echo ".env not found in $(pwd)"; exit 1; }
case "${NTFY_BASE_URL:-}" in
    https://*/*) echo "NTFY_BASE_URL must have no path or trailing slash."; exit 1 ;;
    https://?*) ;;
    *) echo "Set NTFY_BASE_URL to the public https:// URL (no trailing slash)."; exit 1 ;;
esac
if grep -q '^export NTFY_TOPIC=.' .env; then
    echo "NTFY_TOPIC is already set in .env; refusing to run again."
    exit 1
fi

pkg install -y curl termux-services

# ntfy is not packaged for Termux, and the official linux/arm64 release is
# killed by Android's seccomp filter (SIGSYS). Build it natively instead.
# The web UI is left out (placeholder files); the API and phone apps work.
NTFY_VER=2.38.2
if ! ntfy --help >/dev/null 2>&1; then
    rm -f "$PREFIX/bin/ntfy"
    pkg install -y golang clang git
    SRC="$(mktemp -d)"
    git clone --depth 1 --branch "v$NTFY_VER" \
        https://github.com/binwiederhier/ntfy.git "$SRC/ntfy"
    (
        cd "$SRC/ntfy"
        mkdir -p server/docs server/site
        touch server/docs/index.html server/site/app.html
        echo "Building ntfy (this can take several minutes)..."
        CGO_ENABLED=1 go build \
            -tags sqlite_omit_load_extension,osusergo,netgo \
            -ldflags "-s -w -X main.version=$NTFY_VER" -o ntfy .
    )
    install -m 755 "$SRC/ntfy/ntfy" "$PREFIX/bin/ntfy"
    rm -rf "$SRC"
fi
ntfy --help >/dev/null

CONF="$HOME/.config/ntfy"
DATA="$HOME/.local/share/ntfy"
umask 077
mkdir -p "$CONF" "$DATA"

if [ -e "$CONF/server.yml" ]; then
    echo "Keeping existing $CONF/server.yml (not overwritten)."
else
    cat > "$CONF/server.yml" <<EOF
# Public URL the iPhones use (Cloudflare Tunnel -> 127.0.0.1:2586). Must match
# the iOS app's Default Server exactly.
base-url: "$NTFY_BASE_URL"
listen-http: "127.0.0.1:2586"
behind-proxy: true
auth-file: "$DATA/user.db"
auth-default-access: "deny-all"
enable-signup: false
cache-file: "$DATA/cache.db"
cache-duration: "24h"
# Instant iOS delivery: sends only message ID + hashed topic URL to ntfy.sh.
upstream-base-url: "https://ntfy.sh"
EOF
fi

export NTFY_AUTH_FILE="$DATA/user.db"
export NTFY_AUTH_DEFAULT_ACCESS="deny-all"

# ntfy creates the auth database on first start; start it briefly.
if [ ! -f "$DATA/user.db" ]; then
    ntfy serve --config "$CONF/server.yml" >/dev/null 2>&1 &
    NTFY_PID=$!
    i=0
    until [ -f "$DATA/user.db" ] && curl -s -m 2 -o /dev/null http://127.0.0.1:2586/v1/health; do
        i=$((i + 1))
        [ "$i" -le 20 ] || { kill "$NTFY_PID"; echo "ntfy did not start."; exit 1; }
        sleep 1
    done
    kill "$NTFY_PID"
    wait "$NTFY_PID" 2>/dev/null || true
fi

TOPIC="daily-$(od -An -tx1 -N12 /dev/urandom | tr -d ' \n')"
NTFY_PASSWORD="$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')" ntfy user add publisher
ntfy access publisher "$TOPIC" write-only >/dev/null  # output contains the topic
TOKEN="$(ntfy token add publisher 2>&1 | grep -o 'tk_[A-Za-z0-9]*' | head -n1)"
[ -n "$TOKEN" ] || { echo "Could not create ntfy token."; exit 1; }

# Drop any empty placeholders, then append the real values.
sed -i '/^export NTFY_\(URL\|TOPIC\|TOKEN\)=/d' .env
{
    echo "export NTFY_URL=http://127.0.0.1:2586"
    echo "export NTFY_TOPIC=$TOPIC"
    echo "export NTFY_TOKEN=$TOKEN"
} >> .env
chmod 600 .env

# termux-services entry so ntfy runs in the background and restarts.
SVC="$PREFIX/var/service/ntfy"
mkdir -p "$SVC/log"
cat > "$SVC/run" <<EOF
#!$PREFIX/bin/sh
exec ntfy serve --config "$CONF/server.yml" 2>&1
EOF
chmod 700 "$SVC/run"
ln -sf "$PREFIX/share/termux-services/svlogger" "$SVC/log/run"

echo
echo "Done. Topic and token were written to .env (not shown)."
echo "Start the server with:  sv-enable ntfy   (or: sv up ntfy)"
echo "If sv complains, restart Termux once so termux-services starts."
echo "Then test with:          sh test_ntfy.sh"
