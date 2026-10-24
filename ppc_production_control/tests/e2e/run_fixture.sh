#!/usr/bin/env bash
# Runs one function of tests/e2e/fixture.py through bench.
#   usage: run_fixture.sh <function> [base64-encoded JSON kwargs]
# Environment: PPC_E2E_SITE (default dev.local), PPC_E2E_BENCH_DIR (default ~/frappe-bench).
# The kwargs are base64 so no quoting survives (or breaks) between the Node test, wsl.exe and bash.
set -e
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "${PPC_E2E_BENCH_DIR:-$HOME/frappe-bench}"
kwargs="{}"
if [ -n "$2" ]; then kwargs="$(printf %s "$2" | base64 -d)"; fi
exec bench --site "${PPC_E2E_SITE:-dev.local}" execute "ppc_production_control.tests.e2e.fixture.$1" --kwargs "$kwargs"
