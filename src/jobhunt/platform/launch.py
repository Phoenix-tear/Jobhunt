"""Start the local server without hijacking an app that already runs on the default port.

Used by scripts/start.ps1 and scripts/start.sh. The browser is opened only on the port of the
server this launcher itself started, never in CI and never with --no-browser.

    python -m jobhunt.platform.launch [--no-browser] [--port 8000] [<extra serve args>]
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence

DEFAULT_PORT = 8000
LAST_FALLBACK_PORT = 8010
NO_BROWSER_ENV_VARS = ("CI", "GITHUB_ACTIONS", "JOBHUNT_NO_BROWSER")


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


def pick_port(
    preferred: int = DEFAULT_PORT,
    *,
    is_free: Callable[[int], bool] = port_is_free,
    last: int = LAST_FALLBACK_PORT,
) -> int | None:
    """The preferred port if free, else the next free one up to `last`; None when all are taken."""
    if is_free(preferred):
        return preferred
    for candidate in range(max(preferred + 1, DEFAULT_PORT + 1), last + 1):
        if is_free(candidate):
            return candidate
    return None


def browser_allowed(*, no_browser: bool, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if no_browser:
        return False
    return not any(env.get(name) for name in NO_BROWSER_ENV_VARS)


def wait_until_healthy(port: int, *, timeout: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # fixed local URL
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        sleep(0.5)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunt.platform.launch")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args, serve_args = parser.parse_known_args(argv)

    port = pick_port(args.port)
    if port is None:
        print(f"No free port between {args.port} and {LAST_FALLBACK_PORT}. Close another app and retry.", file=sys.stderr)
        return 2
    if port != args.port:
        print(f"Port {args.port} is already in use (another app is running there); using port {port} instead.")
    url = f"http://127.0.0.1:{port}"
    print(f"Starting Jobhunt at {url}  (Ctrl+C to stop)")

    server = subprocess.Popen([sys.executable, "-m", "jobhunt", "serve", "--port", str(port), *serve_args])
    try:
        if browser_allowed(no_browser=args.no_browser) and wait_until_healthy(port):
            webbrowser.open(url)
        return server.wait()
    except KeyboardInterrupt:
        server.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
