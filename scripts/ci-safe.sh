#!/usr/bin/env bash
set -euo pipefail

echo '=== PYTHON SYNTAX ==='
python3 -m py_compile agent.py

echo
echo '=== HEALTH PROBE ==='
curl -fsS 'http://127.0.0.1:8000/health' >/dev/null

echo
echo '=== WATCHDOG SUMMARY ==='
curl -fsS 'http://127.0.0.1:8000/api/watchdog/summary?shop=efro' >/dev/null

echo
echo 'CI_SAFE_OK=1'
