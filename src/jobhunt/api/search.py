"""Turn free-form user input into a safe FTS5 MATCH expression.

FTS5 has its own query language (AND/OR/NOT/NEAR, `*`, `^`, `:`, parentheses, quotes...).
Passing raw user text to MATCH would let a stray `"` or `(` raise a syntax error (a 500) and lets
users reach operators and column filters. Instead the input is reduced to plain word tokens and
every token is emitted as a double-quoted string (inner quotes doubled, per the FTS5 docs), so
no input can be interpreted as syntax. Tokens are AND-ed; the last one is a prefix match so the
search works while typing.
"""

from __future__ import annotations

import re

MAX_INPUT_CHARS = 200
MAX_TOKENS = 8
MAX_TOKEN_CHARS = 40
# Letters/digits in any script, plus the joiners that occur inside tech terms (c++, node.js, ci/cd).
_TOKEN = re.compile(r"[^\W_](?:[\w+#.\-/]*[^\W_+#.\-/])?\+*|[^\W_]", re.UNICODE)


def tokenize_query(raw: str | None) -> list[str]:
    """Word tokens from user input; never raises, never returns operators."""
    if not raw:
        return []
    cleaned = raw[:MAX_INPUT_CHARS]
    tokens: list[str] = []
    for match in _TOKEN.finditer(cleaned):
        token = match.group(0)[:MAX_TOKEN_CHARS]
        if token:
            tokens.append(token)
        if len(tokens) >= MAX_TOKENS:
            break
    return tokens


def to_match_expression(raw: str | None) -> str | None:
    """FTS5 MATCH string for `raw`, or None when nothing searchable remains."""
    tokens = tokenize_query(raw)
    if not tokens:
        return None
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens]
    # Prefix-match the word being typed, but only from 3 letters up: "c" or "r" as a prefix would
    # match half the index, while "c++" and "r" are legitimate exact terms.
    if sum(character.isalnum() for character in tokens[-1]) >= 3:
        quoted[-1] += " *"
    return " ".join(quoted)
