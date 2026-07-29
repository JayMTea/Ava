#!/usr/bin/env python3
"""Deprecated shim -> tools/ondevice_check.py.

This was the Apple-Silicon-only on-device validator. The generic checker does the
same checks for every platform in deploy/platforms.conf and additionally records
the evidence a `verified-on-device` tier has to cite, so there is nothing
Mac-specific left to keep here.

Kept as a shim rather than deleted because the name is referenced from
docs/HWINFO_VALIDATION.md, tools/mac_sim_audit.py and a year of the maintainer's
muscle memory — the same courtesy deploy/omni-serve.sh gets. Every argument is
forwarded unchanged.
"""
import os
import runpy
import sys

_REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ondevice_check.py")

if __name__ == "__main__":
    print("note: mac_ondevice_check.py is now tools/ondevice_check.py "
          "(platform-agnostic, and it can --record evidence)\n", file=sys.stderr)
    sys.argv[0] = _REAL
    runpy.run_path(_REAL, run_name="__main__")
