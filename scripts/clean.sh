#!/usr/bin/env bash
# Remove stale Mininet state and old result files.
sudo mn -c
rm -f "$(dirname "$0")"/../results/*.csv "$(dirname "$0")"/../results/*.log
