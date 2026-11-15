"""A capture program that hangs: records its pid and its children's, then sleeps.

    sleeper.py PID_DIR [SECONDS]

Starts one child in its own process group (as a program's helper would) and one in a new
session (as Playwright starts Chromium), so a test can check that both are cleaned up.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

d = Path(sys.argv[1])
secs = sys.argv[2] if len(sys.argv) > 2 else "300"
d.mkdir(parents=True, exist_ok=True)
nap = [sys.executable, "-c", f"import time; time.sleep({secs})"]
same = subprocess.Popen(nap)
own = subprocess.Popen(nap, start_new_session=True)
for name, pid in (("main", None), ("child", same.pid), ("session", own.pid)):
    (d / f"{name}.pid").write_text(str(pid or os.getpid()))
time.sleep(float(secs))
