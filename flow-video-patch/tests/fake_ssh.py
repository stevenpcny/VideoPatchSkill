#!/usr/bin/env python3
"""Stand-in for `ssh -o BatchMode=yes ALIAS COMMAND` in tests: runs COMMAND in the local shell."""
import subprocess
import sys

args = sys.argv[1:]
assert args[:2] == ['-o', 'BatchMode=yes'] and args[2] == 'worker', args
raise SystemExit(subprocess.run(args[3], shell=True).returncode)
