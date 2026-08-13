#!/usr/bin/env bash
# Reproduce the discovery runs OFFLINE using the recorded model decisions.
#
# The genuine discovery was driven by an LLM through the bridge
# provider (see REPORT / README). For deterministic, key-free reproduction we
# replay those exact per-step decisions through the SAME real discovery loop
# (observe -> decide -> act), which re-derives the artifact from a live run.
#
# To run a fresh, truly-live discovery with your own key instead:
#   python -m bankcua.cli discover --task <task> --provider anthropic
set -euo pipefail
cd "$(dirname "$0")/.."
# OUT/EV can be overridden (e.g. by scripts/verify.sh) to avoid clobbering the
# committed live-API-recorded artifacts.
OUT="${OUT:-capabilities}"; EV="${EV:-evidence}"
BL="$EV/bridge_lookup"; BS="$EV/bridge_sub"
mkdir -p "$BL" "$BS" "$OUT"

seed () { printf '%s\n' "$2" > "$1/response-$3.json"; }

# --- member_savings_lookup decisions ---
seed "$BL" '{"action":"fill","intent":"Type the operator user id","ref":0,"value":"{username}"}' 0
seed "$BL" '{"action":"fill","intent":"Type the operator password","ref":1,"value":"{password}"}' 1
seed "$BL" '{"action":"click","intent":"Click Sign On","ref":2}' 2
seed "$BL" '{"action":"fill","intent":"Enter the member id","ref":0,"value":"{member_id}"}' 3
seed "$BL" '{"action":"click","intent":"Click Search","ref":1}' 4
seed "$BL" '{"action":"extract","intent":"Read member name","ref":2,"output_name":"member_name","attribute":"text"}' 5
seed "$BL" '{"action":"extract","intent":"Read savings balance","ref":4,"output_name":"savings_balance","attribute":"text"}' 6
seed "$BL" '{"action":"finish","intent":"Goal met","success":true,"reason":"Read name and balance"}' 7

python -m bankcua.cli discover --task config/tasks/member_savings_lookup.json \
  --provider bridge --bridge-dir "$BL" --out "$OUT" --evidence "$EV" --bridge-timeout 30

# --- open_subaccount decisions (allow-risky so discovery completes the create) ---
seed "$BS" '{"action":"fill","intent":"Type user id","ref":0,"value":"{username}"}' 0
seed "$BS" '{"action":"fill","intent":"Type password","ref":1,"value":"{password}"}' 1
seed "$BS" '{"action":"click","intent":"Sign On","ref":2}' 2
seed "$BS" '{"action":"fill","intent":"Enter member id","ref":0,"value":"{member_id}"}' 3
seed "$BS" '{"action":"click","intent":"Search","ref":1}' 4
seed "$BS" '{"action":"click","intent":"Open new sub-account form","ref":0}' 5
seed "$BS" '{"action":"select","intent":"Choose account type","ref":0,"value":"{acct_type}","select_by":"label"}' 6
seed "$BS" '{"action":"fill","intent":"Enter initial deposit","ref":1,"value":"{deposit}"}' 7
seed "$BS" '{"action":"click","intent":"Review","ref":2}' 8
seed "$BS" '{"action":"click","intent":"Confirm and create (irreversible)","ref":0}' 9
seed "$BS" '{"action":"extract","intent":"Read confirmation number","ref":1,"output_name":"confirmation_number","attribute":"text"}' 10
seed "$BS" '{"action":"finish","intent":"Reached confirmation","success":true,"reason":"Sub-account created"}' 11

python -m bankcua.cli discover --task config/tasks/open_subaccount.json \
  --provider bridge --bridge-dir "$BS" --out "$OUT" --evidence "$EV" --bridge-timeout 30 --allow-risky

echo "discovery complete; artifacts in capabilities/"
