"""End-to-end test of graceful shutdown via SIGTERM/SIGINT.

Runs the real server in a subprocess, sends it a termination signal, and
asserts it drains and exits cleanly (rather than being killed outright).
"""

import os
import signal
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PROJECT_ROOT, "server.py")
PY = sys.executable


def _start_server(port, extra=None):
    cmd = [
        PY,
        SERVER,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--shutdown-timeout",
        "3.0",
    ]
    if extra:
        cmd += extra
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=PROJECT_ROOT,
    )
    # Wait until it's accepting requests.
    import urllib.request

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        except Exception:
            time.sleep(0.25)
        else:
            return proc
    proc.kill()
    raise AssertionError("server did not become ready")


def test_sigterm_graceful_shutdown():
    proc = _start_server(8123)
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            out, _err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _err = proc.communicate()
            raise AssertionError("server did not exit after SIGTERM")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, (
        f"expected clean exit (code 0) after SIGTERM, got {proc.returncode} "
        f"(negative means killed by the default SIGTERM action):\n{out}"
    )