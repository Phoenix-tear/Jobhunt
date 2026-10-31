"""Deterministic language-quality checks for cover letters and answers. Pure Python, no model.

The grounding validator (`validator.py`) proves a text only says things the cited facts support. It cannot tell whether
the text reads well. These checks look at the writing itself and return warnings (never a rejection): sentence length,
repeated sentence openers, repeated words and phrases, double spaces, unfinished sentences, sentences without a verb
(a heuristic), leftover placeholders, clichés and filler openers, and sentences that are a pasted resume bullet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_AVERAGE_SENTENCE_WORDS = 22
MAX_SENTENCE_WORDS = 35
MIN_SENTENCE_WORDS_FOR_VERB_CHECK = 4
REPEATED_WORD_LIMIT = 4            # a content word used this many times or more
REPEATED_PHRASE_LIMIT = 3          # a three-word phrase used this many times or more (a role title may appear twice)
CONSECUTIVE_OPENER_LIMIT = 3

CLICHES = (
    "passionate", "thrilled", "excited to leverage", "dynamic", "fast-paced", "fast paced", "leverage", "synergy",
    "team player", "hard-working", "hardworking", "results-driven", "go-getter", "think outside the box",
    "cutting-edge", "proven track record", "self-starter", "rockstar", "ninja", "game-changer", "perfect fit",
    "ideal candidate", "unique opportunity", "i am excited", "i'm excited", "i am eager", "dream job",
)
FILLER_OPENERS = (
    "i am writing in", "i would like to take this opportunity", "with great enthusiasm",
    "it is with great", "to whom it may concern", "i am delighted to", "please accept this letter",
    "i am pleased to apply", "allow me to introduce",
)
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,60}\]|\{[^}\n]{1,60}\}|<[^>\n]{1,60}>|\bTODO\b|\bTBD\b|\bX{3,}\b|\blorem ipsum\b",
                          re.IGNORECASE)
_ABBREVIATIONS = ("Pvt.", "Ltd.", "Inc.", "vs.", "e.g.", "i.e.", "etc.", "B.Tech", "M.Tech", "Dr.", "Mr.", "Ms.", "St.",
                  "Co.", "approx.", "No.", "Jr.", "Sr.")
_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'+#.\-]*|\d[\d,.%+]*")
_STOP = frozenset("""a an and are as at be but by for from has have i in is it its my of on or so that the their this to
    was were which with you your we our can will would should could may might also into than then them these those been
    being both each other such who whom what when where while about after over under more most very just not only""".split())

_VERBS = frozenset("""am is are was were be been being have has had do does did can could will would shall should may might
    must build built builds building write wrote work worked works working design designed develop developed deliver
    delivered ship shipped create created lead led learn learned use used using bring brought add added apply applied
    help helped make made run ran test tested measure measured improve improved reduce reduced raise raised cut handle
    handled own owned serve served process processed deploy deployed integrate integrated automate automated train
    trained evaluate evaluated analyse analyze analysed analyzed contribute contributed collaborate collaborated join
    joined want wants like enjoy enjoyed see saw show showed shows fit fits match matches need needs ask asks require
    requires look looks find found think thought know knew believe believes combine combines connect connected focus
    focused care cares prefer prefers keep kept mean means offer offers support supports provide provides seek seeks
    welcome welcomes appreciate appreciates thank thanks hope hopes discuss discussed explain explained expect expects
    study studying studied graduate graduating complete completed start started finish finished plan planned review
    reviewed debug debugged document documented maintain maintained monitor monitored scale scaled secure secured finds routes uses runs gets takes gives keeps lets turns sends returns reads writes checks
    stores tracks limits blocks caches serves handles reaches covers calls lists sorts groups ranks maps rises falls grows
    drops matters wins fails passes holds sits stands feels seems becomes remains exists appears happens works builds""".split())
_SUBJECTS = frozenset({"i", "we", "you", "they", "he", "she", "it"})
_DANGLING_END = frozenset("""and or but with to of the a an for in on at by from as that which who whose because while if
    than into using via per""".split())
_BULLET_PASTE = re.compile(r"^[A-Z][\w &'\-]{2,60}(?:\s[-–]\s[\w &'\-]{2,40})?:\s+(?:Built|Designed|Added|Owned|Served|"
                           r"Developed|Hardened|Shipped|Trained|Created|Implemented|Exposed|Worked)\b")


@dataclass
class Warning_:
    rule: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "detail": self.detail[:200]}


@dataclass
class QualityReport:
    ok: bool = True
    warnings: list[Warning_] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add(self, rule: str, detail: str = "") -> None:
        self.ok = False
        self.warnings.append(Warning_(rule, detail))

    @property
    def rules(self) -> set[str]:
        return {w.rule for w in self.warnings}

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "warnings": [w.as_dict() for w in self.warnings], "stats": self.stats}


def split_sentences(text: str) -> list[str]:
    """Sentences of `text` (paragraph breaks always split). Common abbreviations do not end a sentence."""
    protected = text
    for abbreviation in _ABBREVIATIONS:
        protected = protected.replace(abbreviation, abbreviation.replace(".", "․"))
    protected = re.sub(r"(?<=\d)\.(?=\d)", "․", protected)
    sentences: list[str] = []
    for paragraph in re.split(r"\n\s*\n|\n", protected):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for part in _SPLIT.split(paragraph):
            part = part.strip().replace("․", ".")
            if part:
                sentences.append(part)
    return sentences


def _words(sentence: str) -> list[str]:
    return _WORD.findall(sentence)


def _has_verb(sentence: str) -> bool:
    tokens = [t.lower().strip(".,'") for t in _words(sentence)]
    for index, token in enumerate(tokens):
        if token in _VERBS:
            return True
        if len(token) > 4 and token.endswith("ed") and not token.endswith("eed"):
            return True
        if index > 0 and tokens[index - 1] in _SUBJECTS and token.isalpha() and token not in _STOP:
            return True                               # "I build", "we ship": a word after a subject pronoun
        if len(token) > 5 and token.endswith("ing") and index > 0:
            return True
    return False


def check_quality(text: str, *, kind: str = "cover_letter", min_words: int | None = None,
                  max_words: int | None = None, extra_banned: tuple[str, ...] = ()) -> QualityReport:
    """Run every check and collect warnings. Empty text is one warning."""
    report = QualityReport()
    body = text.strip()
    if not body:
        report.add("empty_text")
        return report
    sentences = split_sentences(body)
    counts = [len(_words(s)) for s in sentences]
    total_words = len(body.split())
    average = sum(counts) / len(counts) if counts else 0.0
    report.stats = {"words": total_words, "sentences": len(sentences), "average_sentence_words": round(average, 1),
                    "longest_sentence_words": max(counts) if counts else 0}

    # length of sentences
    for sentence, count in zip(sentences, counts):
        if count > MAX_SENTENCE_WORDS:
            report.add("sentence_too_long", f"{count} words: {' '.join(sentence.split()[:10])}...")
    if average > MAX_AVERAGE_SENTENCE_WORDS:
        report.add("average_sentence_too_long", f"average {average:.1f} words, target under {MAX_AVERAGE_SENTENCE_WORDS}")
    if min_words is not None and total_words < min_words:
        report.add("too_short", f"{total_words} words, minimum {min_words}")
    if max_words is not None and total_words > max_words:
        report.add("too_long", f"{total_words} words, maximum {max_words}")

    # repeated openers
    openers = [(_words(s) or [""])[0].lower() for s in sentences]
    if len(sentences) >= 4:
        tally: dict[str, int] = {}
        for opener in openers:
            tally[opener] = tally.get(opener, 0) + 1
        for opener, count in tally.items():
            if opener and count >= 3 and count > 0.5 * len(sentences):
                report.add("repeated_sentence_starter", f"{count} of {len(sentences)} sentences start with \"{opener}\"")
    run = 1
    for previous, current in zip(openers, openers[1:]):
        run = run + 1 if previous and previous == current else 1
        if run >= CONSECUTIVE_OPENER_LIMIT:
            report.add("repeated_sentence_starter", f"{run} sentences in a row start with \"{current}\"")
            break

    # repeated words and phrases
    tokens = [t.lower().strip(".,'") for t in _words(body)]
    content = [t for t in tokens if t.isalpha() and t not in _STOP and len(t) >= 5]
    seen: dict[str, int] = {}
    for token in content:
        seen[token] = seen.get(token, 0) + 1
    for token, count in sorted(seen.items(), key=lambda item: -item[1]):
        if count >= REPEATED_WORD_LIMIT:
            report.add("repeated_word", f"\"{token}\" used {count} times")
            break
    trigrams: dict[tuple[str, ...], int] = {}
    for index in range(len(tokens) - 2):
        gram = tuple(tokens[index:index + 3])
        if sum(1 for t in gram if t in _STOP) <= 1 and all(t.isalpha() for t in gram):
            trigrams[gram] = trigrams.get(gram, 0) + 1
    for gram, count in sorted(trigrams.items(), key=lambda item: -item[1]):
        if count >= REPEATED_PHRASE_LIMIT:
            report.add("repeated_phrase", f"\"{' '.join(gram)}\" appears {count} times")
            break

    # form
    if re.search(r"[^\S\n]{2,}", body):
        report.add("double_space")
    if _PLACEHOLDER.search(body):
        report.add("placeholder_left_in", _PLACEHOLDER.search(body).group(0))      # type: ignore[union-attr]

    # unfinished sentences
    for paragraph in [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]:
        if not re.search(r"[.!?][\"')]*$", paragraph):
            report.add("unfinished_sentence", f"paragraph ends without a full stop: ...{paragraph[-30:]}")
    for sentence in sentences:
        last = (_words(sentence) or [""])[-1].lower().strip(".,'")
        if last in _DANGLING_END and len(sentence.split()) > 3 and not re.search(r"[.!?][\"')]*$", sentence):
            report.add("unfinished_sentence", f"ends on \"{last}\": ...{sentence[-30:]}")
        if re.search(r"(?:,|:|;|\.\.\.|…)\s*$", sentence):
            report.add("unfinished_sentence", f"ends on punctuation: ...{sentence[-30:]}")

    # sentences without a verb (heuristic) and pasted resume bullets
    for sentence in sentences:
        if len(_words(sentence)) >= MIN_SENTENCE_WORDS_FOR_VERB_CHECK and not _has_verb(sentence):
            report.add("sentence_without_verb", " ".join(sentence.split()[:10]))
        if _BULLET_PASTE.match(sentence):
            report.add("pasted_resume_bullet", " ".join(sentence.split()[:10]))

    # clichés and filler
    lowered = body.lower()
    for phrase in (*CLICHES, *extra_banned):
        if re.search(rf"(?<![a-z]){re.escape(phrase.lower())}(?![a-z])", lowered):
            report.add("cliche", phrase)
    for phrase in FILLER_OPENERS:
        if lowered.startswith(phrase) or any(s.lower().startswith(phrase) for s in sentences):
            report.add("filler_opener", phrase)
    _ = kind
    return report


__all__ = ["CLICHES", "FILLER_OPENERS", "QualityReport", "check_quality", "split_sentences"]
