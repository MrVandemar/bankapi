import contextlib
import io
import sqlite3
import threading
import unittest
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests

import eb_balance


class DailyAllowanceTests(unittest.TestCase):
    def test_calendar_boundaries_and_flooring(self):
        cases = [
            ("100", date(2026, 11, 9), 50),
            ("100", date(2026, 11, 10), 1),
            ("100", date(2026, 12, 31), 5),
            ("100", date(2024, 2, 29), 5),
            ("-4.60", date(2026, 11, 9), -3),
            ("1.9999999999999999999999999999999", date(2026, 11, 9), 0),
        ]
        for balance, today, expected in cases:
            with self.subTest(balance=balance, today=today):
                self.assertEqual(eb_balance.daily_allowance(balance, today), expected)

    def test_invalid_amount_is_rejected(self):
        for amount in ("NaN", "Infinity", "invalid", None):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                eb_balance.daily_allowance(amount, date(2026, 11, 9))


class FetchTests(unittest.TestCase):
    def test_existing_fetch_skips_unavailable_account_and_saves_other(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE account (uid TEXT, iban TEXT);
            CREATE TABLE balance (id INTEGER PRIMARY KEY, uid TEXT, fetched_at TEXT,
                                  balance_type TEXT, amount TEXT, currency TEXT, reference_date TEXT);
            CREATE TABLE session (session_id TEXT, valid_until TEXT);
            INSERT INTO account VALUES ('missing', 'first'), ('available', 'second');
            """
        )
        balance = {"balance_type": "CLAV", "balance_amount": {"amount": "10", "currency": "EUR"}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(eb_balance, "db", return_value=conn), \
                patch.object(eb_balance, "fetch_account_balance",
                             side_effect=[eb_balance.BalanceUnavailable("no balances returned"),
                                          balance]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            eb_balance.cmd_fetch(None)
        self.assertIn("[first] no balances returned", stderr.getvalue())
        self.assertIn("[second] 10 EUR (CLAV)", stdout.getvalue())
        self.assertEqual(conn.execute("SELECT uid, amount FROM balance").fetchall(),
                         [("available", "10")])


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE account (uid TEXT PRIMARY KEY);
            CREATE TABLE balance (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, fetched_at TEXT,
                balance_type TEXT, amount TEXT, currency TEXT, reference_date TEXT
            );
            INSERT INTO account VALUES ('account-1');
            """
        )
        self.addCleanup(self.conn.close)
        self.env = {
            "EB_ACCOUNT_UID": "account-1",
            "NTFY_TOPIC": "private-topic",
            "NTFY_TOKEN": "secret-token",
            "NTFY_URL": "http://127.0.0.1:2586",
        }
        self.balance = {
            "balance_type": "CLAV",
            "balance_amount": {"amount": "100", "currency": "EUR"},
        }

    def notify(self, fetch_result=None, fetch_error=None, status=200, env=None,
               mock_calculation=True):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.status_code = status
        calculation = (patch.object(eb_balance, "daily_allowance", return_value=5)
                       if mock_calculation else contextlib.nullcontext())
        with patch.dict(eb_balance.os.environ, env or self.env, clear=True), \
                patch.object(eb_balance, "db", return_value=self.conn), \
                patch.object(eb_balance, "fetch_account_balance",
                             return_value=fetch_result, side_effect=fetch_error) as fetch, \
                patch.object(eb_balance.requests, "Session", return_value=session), \
                calculation, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                eb_balance.cmd_notify(None)
                error = None
            except SystemExit as exc:
                error = str(exc)
        return fetch, session, error

    def test_fetches_configured_account_and_publishes_once(self):
        fetch, session, error = self.notify(fetch_result=self.balance)
        self.assertIsNone(error)
        fetch.assert_called_once_with("account-1")
        session.post.assert_called_once_with(
            "http://127.0.0.1:2586/private-topic",
            data=b"You have 5 for today",
            headers={"Authorization": "Bearer secret-token"},
            timeout=30,
            allow_redirects=False,
        )
        self.assertFalse(session.trust_env)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0], 1)

    def test_fetch_failure_never_publishes_or_uses_cache(self):
        for error in (eb_balance.BalanceUnavailable("no balances returned"),
                      requests.ConnectionError("network error")):
            with self.subTest(error=error):
                _, session, message = self.notify(fetch_error=error)
                self.assertIsNotNone(message)
                session.post.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0], 0)

    def test_unknown_account_and_nonlocal_server_are_rejected(self):
        for changes in ({"EB_ACCOUNT_UID": "unknown"}, {"NTFY_URL": "https://ntfy.sh"}):
            with self.subTest(changes=changes):
                fetch, session, error = self.notify(env={**self.env, **changes})
                self.assertIsNotNone(error)
                fetch.assert_not_called()
                session.post.assert_not_called()

    def test_publish_failure_is_reported_without_retry(self):
        _, session, error = self.notify(fetch_result=self.balance, status=503)
        self.assertEqual(error, "ntfy publish failed (HTTP 503).")
        session.post.assert_called_once()

    def test_invalid_fetched_amount_is_not_published_or_cached(self):
        balance = {"balance_amount": {"amount": "NaN", "currency": "EUR"}}
        _, session, error = self.notify(fetch_result=balance, mock_calculation=False)
        self.assertIn("Could not fetch a valid balance", error)
        session.post.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0], 0)

    def test_local_http_publish_uses_bratislava_calendar_date(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append((self.path, self.headers["Authorization"], self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = {**self.env, "NTFY_URL": f"http://127.0.0.1:{server.server_port}"}
        instant = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)
        with patch.dict(eb_balance.os.environ, env, clear=True), \
                patch.object(eb_balance, "db", return_value=self.conn), \
                patch.object(eb_balance, "fetch_account_balance", return_value=self.balance), \
                patch.object(eb_balance, "datetime", wraps=datetime) as clock, \
                contextlib.redirect_stdout(io.StringIO()):
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            eb_balance.cmd_notify(None)
        self.assertEqual(instant.astimezone(ZoneInfo("Europe/Bratislava")).date(),
                         date(2026, 11, 10))
        self.assertEqual(received, [("/private-topic", "Bearer secret-token",
                                     b"You have 1 for today")])


if __name__ == "__main__":
    unittest.main()
