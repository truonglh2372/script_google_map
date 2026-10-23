"""Offline self-test: the crawler never uploads Google Street View images (#681, Google terms).

Run: python tools/test_no_streetview.py
It reads the shipped common/safe_run.py, so the code the installer copies is what is tested.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "common", "safe_run.py"), encoding="utf-8").read()
FAILURES = []


def check(label, ok):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILURES.append(label)


ast.parse(SRC)
check("no Street View thumbnail URL is read", "streetviewpixels" not in SRC)
check("no streetview.jpg is written or uploaded", "streetview.jpg" not in SRC)
check("no Street View URL is kept on the place info", '"sv"]' not in SRC and '"_sv"' not in SRC)
check("no streetview_cover is set", "streetview_cover" not in SRC)
if FAILURES:
    sys.exit(f"{len(FAILURES)} failure(s)")
print("all ok")
