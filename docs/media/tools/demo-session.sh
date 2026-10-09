#!/usr/bin/env bash
# The scripted terminal session that record-demo.sh captures with asciinema.
#
# Every command shown on screen is really executed against a real `make run` server using
# SimBroker; only the *typing* is simulated (so the recording is readable). Needs: a project
# checkout with `make install` and `make init` done, plus `jq` and GNU `sleep`.
#
# Run it directly (`bash docs/media/tools/demo-session.sh`) to preview without recording.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
export PATH="$ROOT/.venv/bin:$PATH" TERM="${TERM:-xterm-256color}"
set -a
. ./.env # random secrets written by `make init`; never printed
set +a
# Heartbeat default is 5 s; 1 s makes SimBroker's cancel-after-kill visible sooner on screen.
export HEARTBEAT_INTERVAL_S=1
rm -rf data server.log # start from an empty database so the alert_ids are new

URL=http://127.0.0.1:8000
CHAR_DELAY=0.012 # seconds per simulated keystroke

# --- presentation helpers --------------------------------------------------------------------
step() { printf '\e[1;33m%s\e[0m\n' "$1"; }         # "# 1. ..." heading
note() { printf '\e[38;5;245m%s\e[0m\n' "$1"; }     # grey aside
good() { printf '\e[1;32m%s\e[0m\n' "$1"; }         # green takeaway
pause() { sleep "$1"; }

run() { # show a prompt, "type" the command, then really run it
	local cmd="$1" i
	printf '\e[1;32m$\e[0m '
	for ((i = 0; i < ${#cmd}; i++)); do
		if [[ "${cmd:i:1}" == $'\n' ]]; then
			sleep 0.15
			printf '\n\e[38;5;245m>\e[0m ' # the continuation prompt bash shows (PS2)
		else
			printf '%s' "${cmd:i:1}"
			sleep "$CHAR_DELAY"
		fi
	done
	sleep 0.3
	printf '\n'
	eval "$cmd"
}

# `send [--secret S] '<json>'`: POST an alert to the bridge, adding the secret from .env (or S,
# used for the wrong-secret step). Prints the HTTP status, then the reply, pretty-printed.
send() {
	local out code body color secret="$WEBHOOK_SECRET"
	if [[ "${1:-}" == --secret ]]; then
		secret="$2"
		shift 2
	fi
	out=$(printf '%s' "$1" | jq -c --arg s "$secret" '{secret: $s} + .' |
		curl -s -w '\n%{http_code}' "$URL/webhook/tradingview" \
			-H 'Content-Type: application/json' -d @-)
	code=${out##*$'\n'}
	body=${out%$'\n'*}
	case "$code" in
	2*) color='1;32' ;;
	4*) color='1;33' ;;
	*) color='1;31' ;;
	esac
	printf '\e[%sm<- HTTP %s\e[0m\n' "$color" "$code"
	printf '%s' "$body" | jq .
}

# --- fake demo alerts ------------------------------------------------------------------------
VALID='{"alert_id":"hello-1","symbol":"AAPL","side":"buy","qty":10,"order_type":"market"}'
BIG='{"alert_id":"hello-big","symbol":"AAPL","side":"buy","qty":5000,"order_type":"market"}'
BAD='{"alert_id":"hello-bad","symbol":"AAPL","side":"buy","qty":"10","order_type":"market"}'
ALT='{"alert_id":"hello-x","symbol":"AAPL","side":"buy","qty":10,"order_type":"market"}'
LIMIT=$'{"alert_id":"limit-1","symbol":"AAPL","side":"buy","qty":5,\n      "order_type":"limit","limit_price":90}'
BRIEF="tv-bridge status | jq -c '{halted: .kill_switch.engaged, working: (.working_orders|length)}'"
LATER='{"alert_id":"hello-2","symbol":"AAPL","side":"buy","qty":10,"order_type":"market"}'

# --- the show --------------------------------------------------------------------------------
clear
step "# tv-broker-bridge: TradingView alerts in, safe paper-broker orders out"
note "# Engineering demo, not investment advice. SimBroker only, fake values, random secrets."
echo
step "# Start the server"
run "make run > server.log 2>&1 &"
SERVER_PID=$!
until curl -sf "$URL/health" > /dev/null; do sleep 0.2; done
run "curl -s localhost:8000/health | jq -c '{status, broker}'"
note "# 'send' below = curl POST to /webhook/tradingview, secret from .env added"
pause 2.2

clear
step "# 1. A valid market order"
run "send '$VALID'"
pause 0.8
good "# ✓ accepted and filled: 10 AAPL on the paper broker"
pause 2.0

clear
step "# 2. The same alert_id sent again (a TradingView retry)"
run "send '$VALID'"
pause 0.8
good "# ✓ blocked as a duplicate: no second order is placed"
pause 2.0

clear
step "# 3. An order that breaks the risk limits (max 100 shares per order)"
run "send '$BIG'"
pause 0.8
good "# ✓ stopped by the risk gate: it never reaches the broker"
pause 2.2

clear
step "# 4. A malformed alert (quantity sent as text)"
run "send '$BAD'"
pause 0.8
good "# ✓ refused, with the reason: qty must be a whole number"
pause 2.0

clear
step "# 5. A wrong secret"
run "send --secret wrong-secret \\
'$ALT'"
pause 0.8
good "# ✓ unauthorized: nothing runs without the secret"
pause 2.0

clear
step "# 6. What is the bridge holding right now?"
run "tv-bridge status | jq '{halted: .kill_switch.engaged, positions, working_orders}'"
pause 0.8
good "# ✓ long 10 AAPL, nothing waiting, kill switch off"
pause 2.0

clear
step "# 7. A limit order priced far below the market: it rests, unfilled"
run "send '$LIMIT'"
pause 0.8
good "# ✓ accepted, but filled_qty is 0: it is waiting at the broker"
pause 2.0

clear
step "# 8. Emergency stop: the kill switch"
run "$BRIEF"
pause 0.6
run "tv-bridge kill | jq -r '.event // .status'"
note "# SimBroker lives in the server, which cancels working orders on its next heartbeat"
pause 1.0
run "$BRIEF"
pause 0.8
good "# ✓ halted, and the resting limit order was canceled (1 working order -> 0)"
pause 2.4

clear
step "# 9. While halted, a new alert is refused"
run "send '$LATER'"
pause 0.8
good "# ✓ refused with 503: nothing is sent to the broker"
pause 2.0

clear
step "# 10. Resume trading (deliberately a command-line action)"
run "tv-bridge resume | jq -r '.event // .status'"
pause 0.8
good "# ✓ trading can start again"
pause 2.2

kill "$SERVER_PID" 2> /dev/null
wait "$SERVER_PID" 2> /dev/null
pgrep -f "[t]v-bridge serve" | xargs -r kill 2> /dev/null
rm -rf data server.log
exit 0
