# Daily ntfy notification

`notify` fetches the configured account's balance and publishes
`You have {amount} for today` to a private ntfy topic. The amount is
`floor(balance / days until the next 10th / 2)`. Days are calendar days in
Europe/Bratislava; on the 10th, the target is the 10th of the following month.
Negative amounts are rounded down too. Both iPhones can subscribe to the same
topic. This is a push notification, not SMS.

## Private ntfy setup

Run an authenticated ntfy server on the same device as the script, with a
private topic. Set ntfy's default access to deny and grant the publisher write
access and each subscriber read access. Protect the server and its tokens;
ntfy's default anonymous access is not suitable for financial information.
The script only publishes to `127.0.0.1`, not to a public ntfy server.

`NTFY_BASE_URL=https://ntfy.<domain> sh setup_ntfy.sh` (run once, in Termux)
builds ntfy natively for Android, writes `~/.config/ntfy/server.yml` unless it
already exists, creates a write-only `publisher` user and token,
writes `NTFY_*` to `.env` and adds a termux-services entry (`sv-enable ntfy`).
`test_ntfy.sh` checks the local server without contacting the bank.

Remote iPhone access uses the existing Cloudflare Tunnel: a public hostname
`ntfy.<domain>` routes to `http://127.0.0.1:2586`, with `base-url` and
`behind-proxy: true` set in `server.yml`. This is a deliberate trade-off:
Cloudflare terminates TLS, so it can read the notification text and the phones'
ntfy credentials, and the server is reachable from the internet, protected only
by ntfy authentication (deny-all default, no signup, long random passwords).
Cloudflare Access cannot be used because the ntfy iOS app cannot log in to it.
Android background restrictions can stop Termux services. Install the ntfy iOS
app on both iPhones, configure the server and a read-only user, subscribe to
the topic, and enable notifications.

For timely iOS notifications, ntfy's self-hosted server normally uses an
[upstream wake-up relay](https://docs.ntfy.sh/config/#ios-instant-notifications).
The documented relay request contains a message ID and hashed topic URL, not
the notification body; each iPhone fetches the message from the private server.
Without this relay, delivery can be delayed by hours. The private server needs
to cache the message for the phone to retrieve it. Test the complete route with
a non-sensitive message before enabling daily notifications.

## Configuration

After linking the account, set the following in the existing private `.env`
file (see `.env.example`):

- `EB_ACCOUNT_UID`: UID of the linked account. `latest` lists balances but not
  UIDs; find the UID locally in the `account` table, without sharing it.
- `NTFY_URL`: local server URL, default `http://127.0.0.1:2586`. Only a
  loopback address is accepted.
- `NTFY_TOPIC`: the private topic name.
- `NTFY_TOKEN`: an ntfy access token with write permission for that topic.

Keep `.env` readable only by the owner. Never commit tokens, topic names or
account identifiers. The existing Enable Banking credentials and a valid
consent are also required. If the selected account's latest stored balance was
fetched today (Europe/Bratislava date, including by `fetch`), `notify` uses it
without calling the bank; otherwise it fetches the account and stores the new
balance in SQLite. `notify --force` always fetches. If fetching fails or the
balance is invalid, it does not fall back to an older stored value. If publishing fails, it reports an error
without retrying; server acceptance does not confirm delivery to either phone.

Run manually after sourcing `.env`:

```sh
./venv/bin/python eb_balance.py notify
```

This sends a real notification and fetches from the bank unless the balance
was already fetched today. Stay within Enable Banking's fetch limits, including
any other scheduled fetches.

## Scheduling on Termux

Use a Termux-compatible scheduler such as cron, configured in
`Europe/Bratislava`, to run the command at 09:00 every day. For example, once
the cron daemon is installed and confirmed to use that time zone, a crontab
entry for a checkout at `$HOME/bankapi` is:

```cron
0 9 * * * cd "$HOME/bankapi" && . ./.env && ./venv/bin/python eb_balance.py notify >> "$HOME/bankapi-notify.log" 2>&1
```

Adjust the path and Python interpreter for the Termux installation. Setting
`TZ` only inside a cron job does not necessarily change when cron runs it:
verify the daemon's time zone and daylight-saving behavior on the device.
Termux's Python has no IANA time-zone data; install it into the venv with
`./venv/bin/pip install tzdata`, or `notify` fails with `ZoneInfoNotFoundError`.
Configure the ntfy server and scheduler to restart after reboot, for example
using [Termux:Boot](https://github.com/termux/termux-boot), and review Android
battery restrictions and Termux process limits. Cron skips a run if the device
or daemon is stopped; it does not catch up later. Check the log for failures
and protect it from other users. A running device and network connectivity are
required, and push delivery time is not guaranteed.

## Tests

With the project's Python dependencies installed, run:

```sh
python -m unittest -v test_eb_balance
```
