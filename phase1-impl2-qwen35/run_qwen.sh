#!/bin/bash
# One Assignment 6 ELM trial: Qwen3.5-4B Q4_K_M on llama.cpp (Metal) driven by Pi.
# Watchdog kills the run if the Mac runs out of memory or boot-disk space (swap).
set -u
W=/Volumes/T7-Tools/fusion-qwen-elm
REPO=$W/Fusion-Integrated
SERVER="$HOME/Desktop/17-Placement Cell/Assignment6_Pi_Bench/runtime/llama-b11560/llama-server"
PORT=18081
TIMEOUT=10800                 # 3 h
MEM_FLOOR=4                   # kern.memorystatus_level %
DISK_FLOOR_KB=$((1500*1024))  # 1.5 GB free on / (macOS swaps there)
RD=$W/runs/$(date +%m%d-%H%M%S)_qwen35_elm
mkdir -p "$RD" "$W/pi-agent"
exec > "$RD/run.log" 2>&1

cat > "$W/pi-agent/models.json" <<EOF
{"providers":{"local":{"baseUrl":"http://127.0.0.1:$PORT/v1","api":"openai-completions","apiKey":"local",
 "models":[{"id":"qwen35","name":"Qwen3.5-4B Q4_K_M","reasoning":true,"contextWindow":32768,"maxTokens":8192,
 "input":["text"],"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},
 "compat":{"thinkingFormat":"qwen-chat-template"},
 "samplingParams":{"temperature":0.6,"top_p":0.95,"top_k":20,"min_p":0.0}}]}}}
EOF
echo '{"defaultProvider":"local","defaultModel":"qwen35","quietStartup":true}' > "$W/pi-agent/settings.json"

PROMPT="Inspect the provided Fusion ERP base repository (the current directory) and the supplied Employee Leave Management (ELM) specification artifacts in $W/elm_specs/ (plain-text exports split into small numbered parts: 01 Problem Statement, 02 Functional Requirement Specification, 03 Use Case Specifications, 04 Business Rule Specifications, 06 Workflow Specifications, 06 Traceability Matrix; read the parts you need, one at a time).
Implement the ELM module as an integrated module of the existing Fusion ERP system.
Reuse the authentication/login, role-management mechanism, common UI/layout/navigation, backend structure, database configuration, shared components, and other common functionality already available in the Fusion Base Code.
Do not unnecessarily rebuild or replace common functionality already present in Fusion.
Implement the required ELM frontend and backend functionality, including appropriate data models, services, APIs/routes, UI components/pages, role-dependent behavior, database changes, and integration with the existing Fusion structure.
First understand the existing repository and the supplied specifications, and then make the necessary repository-level changes.
Start with README.md, docs/08-delivery/role-assignment-guide.md, and modules/placement and modules/directory as the reference module shape; the backend venv is .venv and 'make check' runs CI.
Attempt to build/run or otherwise verify the resulting implementation where possible."

echo "base: $(git -C "$REPO" log -1 --format='%h %ci %s')"
echo "start: $(date)"
"$SERVER" -m "$W/models/qwen35-4b-q4km.gguf" -c 32768 -ngl 999 --jinja --metrics --parallel 1 \
  --host 127.0.0.1 --port $PORT -fa on -ctk q8_0 -ctv q8_0 --cache-ram 0 -ctxcp 8 > "$RD/server.log" 2>&1 &
SRV=$!
for i in $(seq 300); do curl -sf http://127.0.0.1:$PORT/health >/dev/null && break; sleep 1; done
echo "server up after ${i}s"

cd "$REPO"
PI_CODING_AGENT_DIR=$W/pi-agent PI_OFFLINE=1 pi -p --mode json --provider local --model qwen35 \
  --thinking medium --session-dir "$RD" --no-context-files --no-extensions --no-skills \
  --no-prompt-templates --offline "$PROMPT" > "$RD/events.jsonl" 2>&1 &
AGENT=$!

T0=$(date +%s); low=0; why=done
while kill -0 $AGENT 2>/dev/null; do
  sleep 15
  mem=$(sysctl -n kern.memorystatus_level)
  disk=$(df -k / | awk 'NR==2{print $4}')
  swap=$(sysctl -n vm.swapusage | awk '{print $6}')
  echo "$(date +%T) mem_free=${mem}% disk_free=$((disk/1024))MB swap_used=$swap load=$(sysctl -n vm.loadavg | awk '{print $2}') server_rss=$(( $(ps -o rss= -p $SRV)/1024 ))MB pi_rss=$(( $(ps -o rss= -p $AGENT)/1024 ))MB" >> "$RD/monitor.log"
  [ "$mem" -lt $MEM_FLOOR ] && low=$((low+1)) || low=0
  if [ $low -ge 4 ]; then why="mem_floor"; break; fi
  if [ "$disk" -lt $DISK_FLOOR_KB ]; then why="disk_floor"; break; fi
  if [ $(( $(date +%s) - T0 )) -gt $TIMEOUT ]; then why="timeout"; break; fi
done
kill -TERM $AGENT 2>/dev/null; sleep 2; kill -KILL $AGENT 2>/dev/null
curl -s http://127.0.0.1:$PORT/metrics > "$RD/metrics.txt"
kill -TERM $SRV; sleep 3; kill -KILL $SRV 2>/dev/null
echo "end: $(date) reason=$why wall=$(( $(date +%s) - T0 ))s"

# Package the generated code; the repo itself is left as Qwen left it.
git add -A -N . && git diff > "$RD/qwen35_elm.diff"
git diff --stat > "$RD/diffstat.txt"
OUT="$HOME/Desktop/17-Placement Cell/ELM_MODEL_Qwen3.5-4B.zip"
rm -f "$OUT"
cd "$W" && zip -qr "$OUT" Fusion-Integrated -x 'Fusion-Integrated/.venv/*' 'Fusion-Integrated/.git/*' \
  '*/node_modules/*' '*/__pycache__/*' '*.pyc'
cd "$RD" && zip -qj "$OUT" qwen35_elm.diff diffstat.txt run.log monitor.log events.jsonl
echo "zip: $OUT"
echo RUN_DONE
