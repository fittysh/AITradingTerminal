#!/usr/bin/env bash
# Install or update the AI Trading Terminal on a Linux server (Ubuntu / Debian, e.g. a DigitalOcean droplet).
#
# Run it from inside the project folder you uploaded:
#     sudo bash deploy/install.sh
#
# It does, in order:
#   1. checks Python (3.9+) and updates the code with `git pull` when the folder is a git checkout;
#   2. creates .env from .env.example on the first run (then stops so you can fill in Telegram + Gmail);
#   3. runs the offline test suite;
#   4. runs one signal scan now, so the result reaches Telegram + Gmail and you can check it;
#   5. installs two systemd services that start on boot and restart on failure:
#        aitt            the 24/7 bot: scan + signals every 4 h, entries every 30 min, management every
#                        5 min, reports at 07:00 and 19:00 MYT (paper trading unless .env says otherwise)
#        aitt-dashboard  the read-only dashboard on 127.0.0.1:8787 (open it through an SSH tunnel)
#
# Options: --no-tests  --no-first-scan  --dry-run (print the service files, install nothing)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_TESTS=1 FIRST_SCAN=1 DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    --no-first-scan) FIRST_SCAN=0 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
RUN_USER="$(stat -c %U "$APP_DIR")"                     # the bot runs as the owner of the project folder
as_user() { if [ "$(id -un)" = "$RUN_USER" ]; then "$@"; else sudo -u "$RUN_USER" -H "$@"; fi; }

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
  echo "Run with sudo (it installs systemd services): sudo bash deploy/install.sh" >&2
  exit 1
fi

say "1/5 Python and code in $APP_DIR (runs as $RUN_USER)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  if [ "$DRY_RUN" = 1 ]; then echo "python3 not found" >&2; exit 1; fi
  apt-get update -qq && apt-get install -y -qq python3
  PY="$(command -v python3)"
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9 or newer is needed (found $("$PY" -V))" >&2; exit 1; }
echo "$("$PY" -V) at $PY"
if [ -d "$APP_DIR/.git" ]; then
  as_user git -C "$APP_DIR" pull --ff-only \
    || echo "note: git pull failed (no network access to the repo or local changes) — using the code as it is"
fi

say "2/5 Settings (.env)"
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  as_user cp "$APP_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  cat <<EOF
Created $ENV_FILE from .env.example. Fill in these lines, then run this script again:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID       (bot from @BotFather; chat id from getUpdates)
  GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_TO   (an App Password, not your Google login password)
Keep TRADING_MODE=paper: v5 has not passed the proof rule on real data yet.
Edit with:  nano $ENV_FILE
EOF
  exit 1
fi
chmod 600 "$ENV_FILE"
val() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r'; }
MODE="$(val TRADING_MODE)"; MODE="${MODE:-paper}"
echo "trading mode: $MODE"
[ -n "$(val TELEGRAM_BOT_TOKEN)" ] && [ -n "$(val TELEGRAM_CHAT_ID)" ] && echo "Telegram: set" \
  || echo "Telegram: NOT set — messages only go to state/outbox/"
[ -n "$(val GMAIL_USER)" ] && [ -n "$(val GMAIL_APP_PASSWORD)" ] && [ -n "$(val GMAIL_TO)" ] && echo "Gmail: set" \
  || echo "Gmail: NOT set — messages only go to state/outbox/"
if [ "$MODE" = live ]; then
  echo "TRADING_MODE=live: the bot still refuses real orders unless LIVE_CONFIRM is set AND the edge gate passes."
fi

if [ "$RUN_TESTS" = 1 ]; then
  say "3/5 Offline tests (about a minute)"
  LOG="$(mktemp)"
  if (cd "$APP_DIR" && as_user "$PY" -m unittest discover -s tests -t . >"$LOG" 2>&1); then
    grep -E "^Ran [0-9]+ tests" "$LOG" || true
    echo "tests passed"
  else
    tail -25 "$LOG"
    echo "tests FAILED — not installing (full log: $LOG)" >&2
    exit 1
  fi
  rm -f "$LOG"
else
  say "3/5 Offline tests skipped (--no-tests)"
fi

if [ "$FIRST_SCAN" = 1 ]; then
  say "4/5 First signal scan → Telegram + Gmail (scans every coin; can take a few minutes)"
  if (cd "$APP_DIR" && as_user "$PY" run_bot.py signals); then
    echo "sent — check Telegram and Gmail. A copy of every message is in $APP_DIR/state/outbox/"
  else
    echo "the scan failed — see $APP_DIR/state/bot.log (is api.bitget.com reachable from this server?)" >&2
    exit 1
  fi
else
  say "4/5 First signal scan skipped (--no-first-scan)"
fi

say "5/5 Services"
unit() {   # name, description, command
  cat <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/run_bot.py $3
Restart=always
RestartSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
}
BOT_UNIT="$(unit aitt "AI Trading Terminal — 24/7 bot" daemon)"
DASH_UNIT="$(unit aitt-dashboard "AI Trading Terminal — read-only dashboard" serve)"
if [ "$DRY_RUN" = 1 ]; then
  printf -- '--- /etc/systemd/system/aitt.service\n%s\n--- /etc/systemd/system/aitt-dashboard.service\n%s\n' \
    "$BOT_UNIT" "$DASH_UNIT"
  echo "(dry run: nothing installed)"
  exit 0
fi
printf '%s\n' "$BOT_UNIT" >/etc/systemd/system/aitt.service
printf '%s\n' "$DASH_UNIT" >/etc/systemd/system/aitt-dashboard.service
systemctl daemon-reload
systemctl enable --now aitt aitt-dashboard >/dev/null
systemctl restart aitt aitt-dashboard
sleep 3
systemctl --no-pager --lines=0 status aitt aitt-dashboard | grep -E "●|Active:" || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Done. The bot now runs 24/7 and restarts after a reboot.
  Status now:     cd $APP_DIR && $PY run_bot.py status
  Live log:       journalctl -u aitt -f        (or: tail -f $APP_DIR/state/bot.log)
  Dashboard:      on your computer run  ssh -L 8787:127.0.0.1:8787 $RUN_USER@${IP:-<server-ip>}
                  then open http://localhost:8787
  Update later:   cd $APP_DIR && sudo bash deploy/install.sh --no-first-scan
  Stop:           sudo systemctl stop aitt aitt-dashboard
EOF
