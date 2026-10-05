#!/bin/sh
# Kairo's test suite, from any directory: every test, or the named unittest
# modules, classes or tests (e.g. test_work, test_work.WorkCase.test_x).
#
# It is the one test entry point that unattended development tools are
# allowed to run (see scripts/setup-claude-code.sh): a fixed command instead
# of an interpreter with arbitrary arguments.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
if [ "$#" -eq 0 ]; then
	PYTHONPATH="$root/src" exec python3 -m unittest discover -s tests
fi
PYTHONPATH="$root/src:$root/tests" exec python3 -m unittest "$@"
