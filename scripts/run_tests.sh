#!/usr/bin/env bash
# Run every test that does not need Mininet.
set -e
cd "$(dirname "$0")/.."
python3 tests/test_comparison.py
python3 tests/loopback_test.py
python3 tests/test_controller_logic.py   # skipped automatically if Ryu is missing
