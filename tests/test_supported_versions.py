"""The minimum Python / Node versions must agree in CI, scripts, doctor, pyproject and docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobhunt.platform import launch
from jobhunt.platform.versions import (
    CI_NODE_MAJOR,
    CI_PYTHON_VERSIONS,
    MIN_NODE,
    MIN_PYTHON,
    format_version,
    node_is_supported,
)

ROOT = Path(__file__).resolve().parent.parent
PY = format_version(MIN_PYTHON)  # "3.12"
NODE = format_version(MIN_NODE)  # "22.12"


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_pyproject_requires_the_minimum_python() -> None:
    assert f'requires-python = ">={PY}"' in _text("pyproject.toml")


def test_ci_matrix_and_node_match() -> None:
    ci = _text(".github/workflows/ci.yml")
    matrix = re.search(r"python-version:\s*\[(.*?)\]", ci)
    assert matrix is not None
    assert tuple(re.findall(r'"([\d.]+)"', matrix.group(1))) == CI_PYTHON_VERSIONS
    assert CI_PYTHON_VERSIONS[0] == PY
    assert re.search(rf"node-version:\s*{CI_NODE_MAJOR}\b", ci)
    assert CI_NODE_MAJOR == MIN_NODE[0]
    assert "cancel-in-progress: true" in ci
    assert 'pytest -m "not live and not live_gemini and not gpu and not ollama"' in ci


@pytest.mark.parametrize("script", ["scripts/setup.ps1", "scripts/setup.sh"])
def test_setup_scripts_state_the_minimums(script: str) -> None:
    text = _text(script)
    assert f">= {PY}" in text
    assert f">= {NODE}" in text
    assert "3.11" not in text and ">= 20" not in text


@pytest.mark.parametrize("doc", ["README.md", "docs/TESTER_GUIDE.md", "docs/OPERATIONS.md"])
def test_docs_state_the_minimums(doc: str) -> None:
    text = _text(doc)
    assert PY in text
    assert NODE in text
    assert not re.search(r"Python[^\n]{0,40}3\.11", text)
    assert not re.search(r"Node(\.js)?[^\n]{0,30}\b20\b", text)


def test_doctor_uses_the_shared_constants() -> None:
    doctor = _text("src/jobhunt/doctor.py")
    assert "MIN_PYTHON" in doctor and "MIN_NODE" in doctor
    assert "3.11" not in doctor and ">= 20" not in doctor


def test_node_version_parsing() -> None:
    assert node_is_supported("v22.12.0")
    assert node_is_supported("v24.16.0")
    assert not node_is_supported("v22.11.9")
    assert not node_is_supported("v20.19.0")
    assert not node_is_supported("garbage")


# ---- launcher (used by start.ps1 / start.sh) ----

def test_pick_port_prefers_default_then_next_free() -> None:
    assert launch.pick_port(8000, is_free=lambda p: True) == 8000
    assert launch.pick_port(8000, is_free=lambda p: p >= 8003) == 8003
    assert launch.pick_port(8000, is_free=lambda p: False) is None
    assert launch.pick_port(8000, is_free=lambda p: p == 8010) == 8010


def test_browser_never_opens_in_ci_or_with_flag() -> None:
    assert launch.browser_allowed(no_browser=False, environ={})
    assert not launch.browser_allowed(no_browser=True, environ={})
    assert not launch.browser_allowed(no_browser=False, environ={"CI": "true"})
    assert not launch.browser_allowed(no_browser=False, environ={"JOBHUNT_NO_BROWSER": "1"})


class _FakeServer:
    def wait(self) -> int:
        return 0


def test_launcher_uses_fallback_port_and_opens_only_its_own_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    started: list[list[str]] = []
    opened: list[str] = []
    monkeypatch.setattr(launch, "port_is_free", lambda port, host="127.0.0.1": port != 8000)
    monkeypatch.setattr(launch, "pick_port", lambda preferred=8000, **_: 8001)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd: started.append(cmd) or _FakeServer())
    monkeypatch.setattr(launch, "wait_until_healthy", lambda port, **_: True)
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("JOBHUNT_NO_BROWSER", raising=False)
    assert launch.main([]) == 0
    assert started and "--port" in started[0] and started[0][started[0].index("--port") + 1] == "8001"
    assert opened == ["http://127.0.0.1:8001"]
    assert "8001" in capsys.readouterr().out


def test_launcher_no_browser_flag_and_no_free_port(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launch, "pick_port", lambda preferred=8000, **_: 8000)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd: _FakeServer())
    monkeypatch.setattr(launch, "wait_until_healthy", lambda port, **_: True)
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    assert launch.main(["--no-browser"]) == 0
    assert opened == []
    monkeypatch.setattr(launch, "pick_port", lambda preferred=8000, **_: None)
    assert launch.main([]) == 2


@pytest.mark.parametrize(
    ("script", "entry"),
    [("scripts/start.ps1", "jobhunt.platform.launch"), ("scripts/start.sh", "jobhunt.platform.launch")],
)
def test_start_scripts_delegate_to_launcher_and_never_open_a_browser_directly(script: str, entry: str) -> None:
    text = _text(script)
    assert entry in text
    assert "Start-Process" not in text and "xdg-open" not in text and "open http" not in text


def test_setup_scripts_do_not_open_a_browser_or_hardcode_port_8000_setup() -> None:
    for script in ("scripts/setup.ps1", "scripts/setup.sh"):
        text = _text(script)
        assert "Start-Process" not in text
        assert "xdg-open" not in text
        assert "localhost:8000/setup" not in text


def test_start_scripts_forward_the_no_browser_flag() -> None:
    assert "--no-browser" in _text("scripts/start.ps1") and "NoBrowser" in _text("scripts/start.ps1")
    assert '"$@"' in _text("scripts/start.sh")
