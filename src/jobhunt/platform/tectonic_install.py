"""Download and verify Tectonic binary for the host platform (Brief v13)."""

from __future__ import annotations

import argparse
import io
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from jobhunt.platform.paths import (
    find_tectonic_executable,
    get_executable_name,
    get_os_arch,
)

logger = logging.getLogger(__name__)

TECTONIC_VERSION = "0.15.0"

# Official release assets from github.com/tectonic-typesetting/tectonic/releases
# format: (url, is_zip)
DOWNLOAD_URLS: dict[str, str] = {
    "windows-x86_64": f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-x86_64-pc-windows-msvc.zip",
    "macos-aarch64": f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-aarch64-apple-darwin.tar.gz",
    "macos-x86_64": f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-x86_64-apple-darwin.tar.gz",
    "linux-x86_64": f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz",
    "linux-aarch64": f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-aarch64-unknown-linux-musl.tar.gz",
}


def verify_tectonic_runs(exe_path: Path) -> bool:
    """Check that the tectonic executable runs and reports its version."""
    if not exe_path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(exe_path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0 and "Tectonic" in proc.stdout
    except Exception:
        return False


def install_tectonic(
    target_dir: Path | None = None,
    *,
    os_arch: str | None = None,
    force: bool = False,
) -> Path:
    """Download and install Tectonic binary for the given os-arch into target_dir.
    
    If target_dir is None, defaults to `tools/tectonic/<os-arch>/`.
    """
    arch = os_arch or get_os_arch()
    exe_name = get_executable_name("tectonic")

    if target_dir is None:
        target_dir = Path.cwd() / "tools" / "tectonic" / arch

    target_dir.mkdir(parents=True, exist_ok=True)
    dest_exe = target_dir / exe_name

    if not force and dest_exe.is_file() and verify_tectonic_runs(dest_exe):
        logger.info("Tectonic binary already exists and works at: %s", dest_exe)
        return dest_exe

    url = DOWNLOAD_URLS.get(arch)
    if not url:
        raise RuntimeError(
            f"No prebuilt Tectonic release available for architecture '{arch}'. "
            "Please install tectonic manually (e.g., brew install tectonic or cargo install tectonic)."
        )

    logger.info("Downloading Tectonic %s from %s ...", TECTONIC_VERSION, url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "jobhunt-setup/1.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()

    # Extract binary from archive in memory
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if Path(member).name.lower() in ("tectonic.exe", "tectonic"):
                    with zf.open(member) as src, open(dest_exe, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break
    elif url.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if Path(member.name).name == "tectonic":
                    f = tf.extractfile(member)
                    if f is not None:
                        with open(dest_exe, "wb") as dst:
                            shutil.copyfileobj(f, dst)
                        break

    if not dest_exe.is_file():
        raise RuntimeError(f"Failed to find tectonic binary in downloaded archive from {url}")

    # Set execute permissions on POSIX
    if os.name != "nt":
        current_perms = dest_exe.stat().st_mode
        dest_exe.chmod(current_perms | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if not verify_tectonic_runs(dest_exe):
        raise RuntimeError(f"Downloaded Tectonic binary at {dest_exe} failed execution verification test.")

    logger.info("Tectonic %s installed successfully at %s", TECTONIC_VERSION, dest_exe)
    return dest_exe


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Tectonic binary for current platform.")
    parser.add_argument("--target-dir", type=Path, default=None, help="Directory to save tectonic executable")
    parser.add_argument("--force", action="store_true", help="Force download even if already present")
    parser.add_argument("--check-only", action="store_true", help="Only check if tectonic is available")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.check_only:
        found = find_tectonic_executable()
        if found and verify_tectonic_runs(found):
            print(f"PASS: Tectonic found at {found}")
            sys.exit(0)
        print("FAIL: Tectonic not found or not working")
        sys.exit(1)

    try:
        dest = install_tectonic(target_dir=args.target_dir, force=args.force)
        print(f"Installed Tectonic: {dest}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

