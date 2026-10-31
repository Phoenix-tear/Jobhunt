"""Check that every pin in requirements.lock has a wheel for Python 3.12/3.13 on Windows, macOS arm64 and Linux x86_64.

Nothing is installed: it only runs `pip download --only-binary=:all: --no-deps` into temp folders.
Usage: python scripts/check_lock_wheels.py [requirements.lock]   (exit 1 when a pin has no wheel)
"""
import re, subprocess, sys, tempfile
from pathlib import Path

lock = Path(sys.argv[1] if len(sys.argv) > 1 else "requirements.lock")
combos = [(v, p) for v in ("3.12", "3.13") for p in ("win_amd64", "macosx_11_0_arm64", "manylinux_2_28_x86_64+manylinux_2_17_x86_64+manylinux2014_x86_64")]
lines = [l for l in lock.read_text(encoding="utf-8").splitlines() if re.match(r"[A-Za-z0-9_.\-]+==", l)]
failed = False
for v, p in combos:
    remaining = list(lines)
    missing = []
    while True:
        tmp = Path(tempfile.mkdtemp(prefix="wc_"))
        req = tmp / "r.txt"; req.write_text("\n".join(remaining), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps", "--python-version", v,
                            *sum([["--platform", q] for q in p.split("+")], []), "-r", str(req), "-d", str(tmp / "out"), "-q"], capture_output=True, text=True)
        if r.returncode == 0:
            break
        m = re.search(r"No matching distribution found for ([A-Za-z0-9_.\-]+)==([^\s]+)", r.stderr)
        if not m:
            missing.append(("?", r.stderr[-300:])); break
        missing.append((m.group(1), m.group(2)))
        remaining = [l for l in remaining if not l.lower().startswith(m.group(1).lower() + "==")]
    failed = failed or bool(missing)
    print(v, p, "OK" if not missing else f"MISSING {missing}", flush=True)
sys.exit(1 if failed else 0)
