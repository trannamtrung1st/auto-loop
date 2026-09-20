"""Process-tree termination tests."""

import subprocess
import sys
import time
from auto_loop.process import terminate_process_tree


def test_terminate_process_tree_stops_child_sleep():
    script = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
time.sleep(120)
"""
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL)
    time.sleep(0.3)
    terminate_process_tree(proc.pid, graceful_seconds=1.0)
    proc.wait(timeout=5)
    assert proc.returncode is not None
