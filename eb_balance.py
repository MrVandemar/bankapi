#!/usr/bin/env python3
"""
eb_balance.py — fetch & cache your bank balance via Enable Banking (restricted/personal mode).

Subcommands:
  aspsps            list banks for a country (find the exact UniCredit name)
  link              one-time consent flow -> stores session + account UIDs
  fetch             fetch balances for linked accounts -> SQLite (run from cron, <=4x/day)
  latest            print latest cached balances as JSON
  notify            publish a daily allowance to local ntfy, using today's stored
                    balance if there is one, else fetching it (--force always fetches)
  serve             tiny read-only HTTP endpoint (Bearer token) serving cached balances

Config via env vars (or a .env you `source`):
  EB_APP_ID         application id from the Enable Banking Control Panel
  EB_KEY_PATH       path to the app's private key (.pem) downloaded when you created the app
  EB_REDIRECT_URL   redirect URL registered on the app (e.g. https://your-domain/eb-callback)
  EB_DB             SQLite path (default: ./balances.db)
  EB_API_TOKEN      bearer token required by `serve` (generate: python3 -c "import secrets;print(secrets.token_urlsafe(32))")
  EB_ACCOUNT_UID    linked account UID used by `notify`
  NTFY_URL          local ntfy server URL (default: http://127.0.0.1:2586)
  NTFY_TOPIC        private ntfy topic
  NTFY_TOKEN        ntfy token with write access to the topic

Deps: pip install requests "pyjwt[crypto]"
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from hmac import compare_digest
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt
import requests

API = "https://api.enablebanking.com"
DB_PATH = os.environ.get("EB_DB", "balances.db")

# Preferred balance types, best first (Berlin Group / ISO 20022 codes)
BALANCE_PREF = ["CLAV", "ITAV", "XPCD", "ITBD", "CLBD", "OPBD", "PRCD"]


# ---------- helpers ----------
def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing env var {name}")
    return v


def auth_headers():
    app_id = env("EB_APP_ID")
    with open(env("EB_KEY_PATH"), "rb") as f:
        key = f.read()
    now = int(time.time())
    token = jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
        key,
        algorithm="RS256",
        headers={"kid": app_id},
    )
    return {"Authorization": f"Bearer {token}"}


def api(method, path, **kw):
    r = requests.request(method, API + path, headers=auth_headers(), timeout=30, **kw)
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (
            session_id TEXT PRIMARY KEY, aspsp TEXT, valid_until TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS account (
            uid TEXT PRIMARY KEY, session_id TEXT, iban TEXT, name TEXT, currency TEXT);
        CREATE TABLE IF NOT EXISTS balance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT, fetched_at TEXT, balance_type TEXT,
            amount TEXT, currency TEXT, reference_date TEXT);
        CREATE INDEX IF NOT EXISTS idx_bal_uid_time ON balance(uid, fetched_at);
        """
    )
    return conn


# ---------- commands ----------
def cmd_aspsps(args):
    data = api("GET", "/aspsps", params={"country": args.country, "psu_type": "personal"})
    for a in data.get("aspsps", []):
        if not args.filter or args.filter.lower() in a["name"].lower():
            print(f'{a["name"]!r:50} max_consent_days={a.get("maximum_consent_validity", 0) // 86400 if a.get("maximum_consent_validity") else "?"}')


def cmd_link(args):
    valid_until = (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat()
    state = str(uuid.uuid4())
    body = {
        "access": {"valid_until": valid_until, "balances": True, "transactions": False},
        "aspsp": {"name": args.aspsp, "country": args.country},
        "state": state,
        "redirect_url": env("EB_REDIRECT_URL"),
        "psu_type": "personal",
    }
    url = api("POST", "/auth", json=body)["url"]
    print("\n1) Open this URL, log in to the bank and confirm (SCA in Smart Banking):\n")
    print(url)
    print("\n2) You'll land on your redirect URL. Paste the FULL URL from the address bar here:")
    redirected = input("> ").strip()
    q = parse_qs(urlparse(redirected).query)
    if q.get("state", [None])[0] != state:
        sys.exit("State mismatch — aborting.")
    if "code" not in q:
        sys.exit(f"No code in URL (error: {q.get('error')})")

    sess = api("POST", "/sessions", json={"code": q["code"][0]})
    conn = db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO session VALUES (?,?,?,?)",
            (sess["session_id"], args.aspsp, sess.get("access", {}).get("valid_until", valid_until),
             datetime.now(timezone.utc).isoformat()),
        )
        for acc in sess.get("accounts", []):
            iban = (acc.get("account_id") or {}).get("iban")
            conn.execute(
                "INSERT OR REPLACE INTO account VALUES (?,?,?,?,?)",
                (acc["uid"], sess["session_id"], iban, acc.get("name"), acc.get("currency")),
            )
    print(f"\nLinked {len(sess.get('accounts', []))} account(s). Consent valid until {valid_until[:10]}.")


def pick_balance(balances):
    by_type = {b.get("balance_type"): b for b in balances}
    for t in BALANCE_PREF:
        if t in by_type:
            return by_type[t]
    return balances[0] if balances else None


class BalanceUnavailable(Exception):
    pass


def fetch_account_balance(uid):
    r = requests.get(f"{API}/accounts/{uid}/balances", headers=auth_headers(), timeout=30)
    if r.status_code in (401, 403, 422):
        raise BalanceUnavailable(f"consent expired/invalid ({r.status_code}) — re-run `link`.")
    r.raise_for_status()
    balance = pick_balance(r.json().get("balances", []))
    if not balance:
        raise BalanceUnavailable("no balances returned")
    return balance


def save_balance(conn, uid, balance, fetched_at):
    amt = balance["balance_amount"]
    with conn:
        conn.execute(
            "INSERT INTO balance (uid, fetched_at, balance_type, amount, currency, reference_date) "
            "VALUES (?,?,?,?,?,?)",
            (uid, fetched_at, balance.get("balance_type"), amt["amount"], amt["currency"],
             balance.get("reference_date")),
        )


def cmd_fetch(args):
    conn = db()
    accounts = conn.execute("SELECT uid, iban FROM account").fetchall()
    if not accounts:
        sys.exit("No linked accounts — run `link` first.")
    now = datetime.now(timezone.utc).isoformat()
    for uid, iban in accounts:
        try:
            b = fetch_account_balance(uid)
        except BalanceUnavailable as exc:
            print(f"[{iban}] {exc}", file=sys.stderr)
            continue
        amt = b["balance_amount"]
        save_balance(conn, uid, b, now)
        print(f"[{iban}] {amt['amount']} {amt['currency']} ({b.get('balance_type')})")

    # warn when consent is about to lapse
    for sid, vu in conn.execute("SELECT session_id, valid_until FROM session"):
        try:
            left = datetime.fromisoformat(vu.replace("Z", "+00:00")) - datetime.now(timezone.utc)
            if left.days < 7:
                print(f"WARNING: consent expires in {left.days} day(s) — re-run `link`.", file=sys.stderr)
        except ValueError:
            pass


def latest():
    conn = db()
    rows = conn.execute(
        """
        SELECT a.iban, a.name, b.amount, b.currency, b.balance_type, b.fetched_at
        FROM account a JOIN balance b ON b.id = (
            SELECT id FROM balance WHERE uid = a.uid ORDER BY fetched_at DESC LIMIT 1)
        """
    ).fetchall()
    return [
        {"iban": r[0], "name": r[1], "amount": r[2], "currency": r[3], "type": r[4], "fetched_at": r[5]}
        for r in rows
    ]


def cmd_latest(args):
    print(json.dumps(latest(), indent=2))


def daily_allowance(amount, today):
    try:
        balance = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid balance amount") from None
    if not balance.is_finite():
        raise ValueError("Invalid balance amount")
    if today.day < 10:
        target = date(today.year, today.month, 10)
    elif today.month == 12:
        target = date(today.year + 1, 1, 10)
    else:
        target = date(today.year, today.month + 1, 10)
    numerator, denominator = balance.as_integer_ratio()
    return numerator // (denominator * (target - today).days * 2)


LOCAL_TZ = "Europe/Bratislava"


def stored_balance_for_day(conn, uid, day):
    """Latest stored amount for uid if it was fetched on `day` (Bratislava date), else None."""
    row = conn.execute(
        "SELECT amount, fetched_at FROM balance WHERE uid = ? ORDER BY fetched_at DESC LIMIT 1",
        (uid,),
    ).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if fetched.tzinfo is None:
        return None
    return row[0] if fetched.astimezone(ZoneInfo(LOCAL_TZ)).date() == day else None


def cmd_notify(args):
    force = getattr(args, "force", False)
    uid = env("EB_ACCOUNT_UID")
    topic = env("NTFY_TOPIC")
    token = env("NTFY_TOKEN")
    server = os.environ.get("NTFY_URL", "http://127.0.0.1:2586")
    parsed = urlparse(server)
    if (parsed.scheme not in ("http", "https") or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        sys.exit("NTFY_URL must be a loopback server URL with no path, query, or credentials.")
    try:
        parsed.port
    except ValueError:
        sys.exit("NTFY_URL has an invalid port.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        sys.exit("NTFY_TOPIC must contain only letters, numbers, underscores, and hyphens.")

    conn = db()
    if not conn.execute("SELECT 1 FROM account WHERE uid = ?", (uid,)).fetchone():
        sys.exit("Configured account is not linked.")
    try:
        today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    except ZoneInfoNotFoundError:
        sys.exit(f"Time-zone data for {LOCAL_TZ} not found; run `pip install tzdata` in the venv.")
    stored = None if force else stored_balance_for_day(conn, uid, today)
    if stored is not None:
        try:
            amount = daily_allowance(stored, today)
        except ValueError:
            sys.exit("Stored balance for today is invalid; re-run with --force.")
        print("Using balance already fetched today (use --force to fetch again).")
    else:
        try:
            balance = fetch_account_balance(uid)
            amount = daily_allowance(balance["balance_amount"]["amount"], today)
            save_balance(conn, uid, balance, datetime.now(timezone.utc).isoformat())
        except BalanceUnavailable as exc:
            sys.exit(str(exc))
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            sys.exit(f"Could not fetch a valid balance ({type(exc).__name__}).")

    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.post(
                f"{server.rstrip('/')}/{topic}",
                data=f"You have {amount} EUR for today".encode("utf-8"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                sys.exit(f"ntfy publish failed (HTTP {response.status_code}).")
        except requests.RequestException:
            sys.exit("ntfy publish failed (connection error).")
    print("Notification accepted by ntfy.")


def cmd_serve(args):
    token = env("EB_API_TOKEN")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            supplied = self.headers.get("Authorization", "")
            if not compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return self._send(401, {"error": "unauthorized"})
            if self.path.rstrip("/") == "/balance":
                return self._send(200, latest())
            self._send(404, {"error": "not found"})

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # don't log requests (they'd contain nothing secret, but keep it quiet)

    print(f"Serving on http://{args.host}:{args.port}/balance (Bearer auth)")
    HTTPServer((args.host, args.port), H).serve_forever()


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    a = sp.add_parser("aspsps"); a.add_argument("--country", default="SK"); a.add_argument("--filter", default="unicredit")
    a.set_defaults(fn=cmd_aspsps)

    l = sp.add_parser("link")
    l.add_argument("--aspsp", required=True, help="exact bank name from `aspsps`")
    l.add_argument("--country", default="SK")
    l.add_argument("--days", type=int, default=90, help="consent validity (bank max applies)")
    l.set_defaults(fn=cmd_link)

    sp.add_parser("fetch").set_defaults(fn=cmd_fetch)
    sp.add_parser("latest").set_defaults(fn=cmd_latest)
    n = sp.add_parser("notify")
    n.add_argument("--force", action="store_true",
                   help="fetch from the bank even if a balance was already fetched today")
    n.set_defaults(fn=cmd_notify)

    s = sp.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1", help="keep on localhost / Tailscale IP; don't bind 0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=cmd_serve)
    return p


def main():
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
