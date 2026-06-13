#!/bin/bash
# PreToolUse hook for Bash. Classifies command via classifier.py (ADR-003/ADR-004).
# Verdicts: allow(0)→pass; deny(2)→block; ask(10)→send Telegram inline buttons, poll DECISION.
# Fail-safe: no Telegram config or timeout → exit 2 (block).

IPC=/tmp/agent
PENDING="$IPC/approval.pending"
DECISION="$IPC/approval.decision"
mkdir -p "$IPC" 2>/dev/null

# Read stdin JSON from Claude Code PreToolUse hook
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("tool_input") or {}).get("command", ""))
except Exception:
    print("")
' 2>/dev/null)"

[ -z "$cmd" ] && exit 0

# Locate classifier.py relative to this script (same directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFIER="$SCRIPT_DIR/classifier.py"

if [ ! -f "$CLASSIFIER" ]; then
    echo "classifier.py not found at $CLASSIFIER — blocking for safety." >&2
    exit 2
fi

# Run classifier: read command via stdin, get JSON + exit code
classifier_out="$(printf '%s' "$cmd" | python3 "$CLASSIFIER" 2>/dev/null)"
classifier_rc=$?

# Exit codes: 0=allow, 10=ask, 2=deny
if [ "$classifier_rc" -eq 0 ]; then
    # allow — pass immediately, do NOT contact Telegram
    exit 0
fi

if [ "$classifier_rc" -eq 2 ]; then
    # deny — block immediately, do NOT contact Telegram
    category="$(printf '%s' "$classifier_out" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(d.get("category","опасная операция"))' 2>/dev/null)"
    reason="$(printf '%s' "$classifier_out" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(d.get("reason","запрещено политикой"))' 2>/dev/null)"
    echo "Команда заблокирована (${category:-опасная операция}): ${reason:-запрещено политикой}" >&2
    exit 2
fi

# verdict=ask (exit code 10) — need Telegram approval
if [ -z "$TELEGRAM_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "Нет Telegram-конфига для approval; опасная команда заблокирована." >&2
    exit 2
fi

# Extract category and reason from classifier JSON
category="$(printf '%s' "$classifier_out" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(d.get("category","неизвестная категория"))' 2>/dev/null)"
reason="$(printf '%s' "$classifier_out" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(d.get("reason","требует подтверждения"))' 2>/dev/null)"

# Generate unique request id: timestamp + PID
req_id="$(date +%Y%m%d%H%M%S)_$$"

# Write PENDING JSON
rm -f "$DECISION"
python3 -c "
import json, sys
data = {
    'id':       sys.argv[1],
    'cmd':      sys.argv[2],
    'category': sys.argv[3],
    'reason':   sys.argv[4],
    'ts':       sys.argv[5],
}
print(json.dumps(data, ensure_ascii=False))
" "$req_id" "$cmd" "${category}" "${reason}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PENDING"

# Build message text (category + reason, no raw secrets)
msg_text="$(printf '⚠️ Подтверждение: %s\nКатегория: %s\nЧем опасно: %s' \
    "$cmd" "${category}" "${reason}")"

# Build inline keyboard JSON with approve:<action>:<id>
keyboard_json="$(python3 -c "
import json, sys
kb = {'inline_keyboard': [[
    {'text': '✅ Разрешить', 'callback_data': 'approve:allow:' + sys.argv[1]},
    {'text': '⛔ Запретить', 'callback_data': 'approve:deny:'  + sys.argv[1]},
]]}
print(json.dumps(kb))
" "$req_id")"

# Send message with inline buttons via Telegram
curl -s --max-time 15 -X POST \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, sys
payload = {
    'chat_id':      sys.argv[1],
    'text':         sys.argv[2],
    'reply_markup': json.loads(sys.argv[3]),
}
print(json.dumps(payload))
" "$TELEGRAM_CHAT_ID" "$msg_text" "$keyboard_json")" >/dev/null 2>&1

# Poll for DECISION (max 300 seconds = 5 minutes)
for i in $(seq 1 300); do
    if [ -f "$DECISION" ]; then
        d="$(cat "$DECISION" 2>/dev/null)"
        rm -f "$DECISION" "$PENDING"
        if [ "$d" = "allow" ]; then
            exit 0
        fi
        echo "Пользователь ЗАПРЕТИЛ выполнение этой команды." >&2
        exit 2
    fi
    sleep 1
done

rm -f "$PENDING"
echo "Таймаут подтверждения: нет ответа за 5 минут, команда заблокирована." >&2
exit 2
