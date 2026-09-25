#!/usr/bin/env bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
"$DIR/.venv/bin/python" "$DIR/run_all_algorithms.py" "$@"
