"""Copy detection for cover letters (deterministic, no model).

A letter must paraphrase: it fails when a sentence shares a run of 8 or more consecutive words with a cited fact, the job
posting or one of my own writing samples, when more than 25% of its words sit inside such runs, or when a sentence reads
like an instruction ("Work from my projects ...", "Use the following ...", "As an AI ...") or like leftover markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RUN_WORDS = 8
MAX_COPIED_SHARE = 0.25
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.']*")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_INSTRUCTION = re.compile(
    r"^\s*(?:work from\b|use the following\b|using the following\b|as an ai\b|as a language model\b|here is\b|here's\b|below is\b|"
    r"note:|the following\b.*\b(?:facts|evidence)\b|write (?:a|the) (?:letter|paragraph)\b|replace\b|insert\b|you are\b|"
    r"i cannot\b|i can't\b|i am unable\b)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,60}\]|\{[^}\n]{1,60}\}|<[^>\n]{1,60}>|\bTODO\b|\bTBD\b|\blorem ipsum\b", re.IGNORECASE)
_MARKUP = re.compile(r"(\*\*|__|`|^\s{0,3}#{1,6}\s|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.MULTILINE)
SIGNATURE_PLACEHOLDER = "[Your Name]"
# stock openers and closers that are allowed on purpose (the style reference uses them): never counted as copying
STOCK_PHRASES = (
    "i am writing to express my interest in the", "i am writing to express my interest in", "i would welcome the opportunity to discuss how",
    "i would welcome the opportunity to discuss", "thank you for your time and consideration", "thank you for your time",
    "i would be excited to bring", "i would welcome the opportunity to",
)
_STOCK = re.compile("|".join(re.escape(p) for p in sorted(STOCK_PHRASES, key=len, reverse=True)), re.IGNORECASE)


def exempt_pattern(phrases: list[str]) -> re.Pattern[str] | None:
    """Names that cannot be paraphrased (my degree and university, the job title, the company, project names) are never copying."""
    cleaned = sorted({" ".join(p.lower().split()) for p in phrases if p and len(p.split()) >= 2}, key=len, reverse=True)
    return re.compile("|".join(re.escape(p) for p in cleaned)) if cleaned else None


def words_of(text: str, tag: str = "", exempt: re.Pattern[str] | None = None) -> list[str]:
    """Lower-case words. With a `tag`, every stock phrase becomes a unique break token so no run can cross or match it."""
    lowered = text.lower().replace("\u2019", "'")
    if tag:
        counter = [0]

        def mark(_match: re.Match[str]) -> str:
            counter[0] += 1
            return f" zzbreak{tag}{counter[0]}zz "

        lowered = _STOCK.sub(mark, lowered)
        if exempt is not None:
            lowered = exempt.sub(mark, lowered)
    return _WORD.findall(lowered)


def _ngrams(words: list[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


@dataclass
class CopyReport:
    ok: bool = True
    violations: list[dict[str, str]] = field(default_factory=list)
    copied_share: float = 0.0
    longest_run: int = 0

    def add(self, rule: str, offending: str, source: str = "") -> None:
        self.ok = False
        self.violations.append({"rule": rule, "offending": offending[:200], "source": source[:200]})

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "violations": self.violations, "copied_share": round(self.copied_share, 3),
                "longest_run": self.longest_run}


def _copied_positions(words: list[str], reference: set[tuple[str, ...]], size: int) -> set[int]:
    """Indexes of `words` that sit inside a run of `size` or more consecutive words found in `reference`."""
    inside: set[int] = set()
    for start in range(len(words) - size + 1):
        if tuple(words[start:start + size]) in reference:
            inside.update(range(start, start + size))
    return inside


def _longest_run(words: list[str], reference_words: list[str]) -> int:
    """Longest run of consecutive words of `words` that also appears (in order, consecutively) in `reference_words`."""
    best = 0
    index: dict[str, list[int]] = {}
    for position, word in enumerate(reference_words):
        index.setdefault(word, []).append(position)
    for start in range(len(words)):
        for ref_start in index.get(words[start], []):
            length = 0
            while (start + length < len(words) and ref_start + length < len(reference_words)
                   and words[start + length] == reference_words[ref_start + length]):
                length += 1
            best = max(best, length)
    return best


def check_copying(letter_body: str, *, facts: list[str], posting: str, samples: str = "", run: int = RUN_WORDS,
                  max_share: float = MAX_COPIED_SHARE, exempt: list[str] | None = None) -> CopyReport:
    """`letter_body` is the letter without greeting and sign-off. `facts` are the cited fact texts."""
    report = CopyReport()
    skip = exempt_pattern(exempt or [])
    words = words_of(letter_body, "L", skip)
    if not words:
        return report
    sources = [("fact", words_of(" ".join(facts), "F", skip)), ("posting", words_of(posting, "P", skip)),
               ("sample", words_of(samples, "S", skip))]
    copied: set[int] = set()
    per_source: dict[str, set[int]] = {}
    for name, ref_words in sources:
        if len(ref_words) < run:
            per_source[name] = set()
            continue
        inside = _copied_positions(words, _ngrams(ref_words, run), run)
        per_source[name] = inside
        copied |= inside
        report.longest_run = max(report.longest_run, _longest_run(words, ref_words) if inside else report.longest_run)
    # name the offending sentences
    for sentence in [s.strip() for s in _SENTENCE.split(letter_body) if s.strip()]:
        sentence_words = words_of(sentence, "L", skip)
        for name, ref_words in sources:
            if len(sentence_words) >= run and len(ref_words) >= run and _ngrams(sentence_words, run) & _ngrams(ref_words, run):
                rule = {"fact": "copied_run_from_facts", "posting": "copied_run_from_posting",
                        "sample": "copied_run_from_sample"}[name]
                report.add(rule, " ".join(sentence.split()[:14]) + "...", f"{run}+ consecutive words in the {name}")
    report.copied_share = len(copied) / len(words)
    if report.copied_share > max_share:
        report.add("too_much_copied_text", f"{round(100 * report.copied_share)}% of the words",
                   f"more than {round(100 * max_share)}% sit in {run}+ word runs copied from the facts, posting or samples")
    for sentence in [s.strip() for s in _SENTENCE.split(letter_body) if s.strip()]:
        if _INSTRUCTION.match(sentence):
            report.add("instruction_like_sentence", " ".join(sentence.split()[:12]))
    cleaned = letter_body.replace(SIGNATURE_PLACEHOLDER, "")
    match = _PLACEHOLDER.search(cleaned)
    if match:
        report.add("placeholder_left_in", match.group(0))
    match = _MARKUP.search(cleaned)
    if match:
        report.add("not_plain_text", match.group(0).strip() or "markup")
    return report


__all__ = ["exempt_pattern", "CopyReport", "MAX_COPIED_SHARE", "RUN_WORDS", "SIGNATURE_PLACEHOLDER", "check_copying", "words_of"]
