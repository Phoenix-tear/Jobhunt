"""Build a ready-to-use `Tailor` (compiler, advice client, fallback client, ledger, guard)."""

from __future__ import annotations

import sqlite3
from typing import Any

from jobhunt.config import Config
from jobhunt.envfile import load_secrets
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.gemini import GeminiClient
from jobhunt.tailoring.engine import Tailor
from jobhunt.tailoring.latex import SubprocessCompiler, find_engine
from jobhunt.tailoring.summaries import write_drafts


def engine_name(config: Config) -> str | None:
    found = find_engine(config.project_root)
    return found[0] if found else None


def make_tailor(connection: sqlite3.Connection, config: Config, *, use_llm: bool = True) -> tuple[Tailor, Any]:
    """The advice client follows `tailoring.provider` (gemini by default); the flash model on Gemini is the automatic
    fallback. Returns (tailor, closer) where closer has .close() or is None."""
    try:
        compiler = SubprocessCompiler(config.project_root)
    except FileNotFoundError:
        compiler = None
    secrets = load_secrets(config.project_root)
    client: Any = None
    fallback: Any = None
    if "demo" in str(config.database_path):
        from jobhunt.settings.demo import DemoGeminiStub
        client = DemoGeminiStub()
    elif use_llm and config.tailoring.llm_enabled:
        gemini_key = secrets.get("GEMINI_API_KEY", "")
        if config.tailoring.provider == "anthropic" and secrets.get("ANTHROPIC_API_KEY"):
            from jobhunt.matching.anthropic import AnthropicClient

            client = AnthropicClient(secrets["ANTHROPIC_API_KEY"])
            fallback = GeminiClient(gemini_key, config.gemini) if gemini_key else None
        elif gemini_key:
            client = GeminiClient(gemini_key, config.gemini)
    ledger = UsageLedger(connection, config.pricing)
    tailor = Tailor(connection, config, compiler, client, ledger=ledger, guard=BudgetGuard(ledger, config.budget),
                    fallback_client=fallback)
    return tailor, _Closer(client, fallback)


class _Closer:
    def __init__(self, *clients: Any) -> None:
        self._clients = [c for c in clients if c is not None]

    def close(self) -> None:
        for client in self._clients:
            client.close()


__all__ = ["engine_name", "make_tailor", "write_drafts"]
