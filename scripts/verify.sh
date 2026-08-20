#!/usr/bin/env bash
#
# One-command local verification. Run this on a machine WITH internet (your Mac
# Terminal). It sets up an isolated environment, then exercises the whole system
# end to end and prints a clear PASS/FAIL summary.
#
#   cd bank-cua && bash scripts/verify.sh
#
# It does NOT touch your committed (live-API) capability artifacts: the offline
# discovery reproduction runs into a scratch directory.

set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
FAIL=0
step() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }
ok()   { printf "  \033[32mPASS\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$1"; FAIL=1; }

step "1/6  Python environment + dependencies"
python3 -m venv .venv || { bad "venv"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null 2>&1
if pip install -q -r requirements.txt >/dev/null 2>&1; then ok "pip install -r requirements.txt"; else bad "pip install"; fi
if python -m playwright install chromium >/dev/null 2>&1; then ok "playwright chromium"; else bad "playwright install chromium"; fi

step "2/6  Automated test suite (unit + integration)"
if python -m pytest -q; then ok "pytest (all tests)"; else bad "pytest"; fi

step "3/6  Offline discovery -> capability artifact (no API key, scratch dir)"
SCRATCH="$(mktemp -d)"
# start the base tenant for discovery
python mockbank/app.py >/tmp/bankcua_mock.log 2>&1 &
MOCK_PID=$!
sleep 2
if OUT="$SCRATCH/capabilities" EV="$SCRATCH/evidence" bash scripts/run_discovery.sh >/tmp/bankcua_disc.log 2>&1; then
  n=$(ls "$SCRATCH/capabilities"/*.json 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -ge 2 ] && ok "discovery produced $n artifacts" || bad "discovery artifacts ($n)"
else
  bad "run_discovery.sh (see /tmp/bankcua_disc.log)"
fi
kill "$MOCK_PID" 2>/dev/null

step "4/6  Deterministic replay + all runtime scenarios"
# gen_evidence starts its own tenants and runs 9 scenarios
if python scripts/gen_evidence.py > /tmp/bankcua_evidence.log 2>&1; then
  grep -E "^\[" /tmp/bankcua_evidence.log
  passes=$(grep -cE "^\[[0-9]" /tmp/bankcua_evidence.log)
  [ "$passes" -ge 14 ] && ok "scenarios ran ($passes reported)" || bad "scenarios ($passes)"
else
  bad "gen_evidence.py (see /tmp/bankcua_evidence.log)"
fi

step "5/6  Agent API + code generation"
python -m bankcua.cli codegen --artifact capabilities/corebank.member_savings_lookup.json \
  --out "$SCRATCH/gen.py" >/dev/null 2>&1
if python -c "import ast,sys; ast.parse(open('$SCRATCH/gen.py').read())" 2>/dev/null; then
  ok "codegen produced valid Python"; else bad "codegen"; fi
python - <<'PY' 2>/dev/null && ok "capability API (manifest + approval gate)" || bad "capability API"
from bankcua.service import create_app
c = create_app().test_client()
caps = c.get("/capabilities").get_json()
assert any(x["name"]=="corebank.member_savings_lookup" for x in caps)
r = c.post("/invoke/corebank.member_savings_lookup", json={"params":{}})
assert r.status_code in (409, 422, 400)   # unapproved / bad params -> gated, not a crash
PY

step "6/6  Safety sweeps"
if grep -rIlE "sk-ant-api[0-9]{2}-" . --exclude-dir=.git --exclude=verify.sh >/dev/null 2>&1; then bad "API key found in repo!"; else ok "no API keys in repo"; fi
if grep -rq "password123" capabilities/ evidence/ 2>/dev/null; then bad "secret in artifacts/evidence"; else ok "no secrets in artifacts/evidence"; fi

rm -rf "$SCRATCH"
printf "\n"
if [ "$FAIL" -eq 0 ]; then
  printf "\033[1;32m========== VERIFICATION PASSED ==========\033[0m\n"
else
  printf "\033[1;31m========== VERIFICATION FAILED (see logs in /tmp/bankcua_*.log) ==========\033[0m\n"
fi
exit "$FAIL"
