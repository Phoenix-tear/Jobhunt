"""Pure helpers for the shape of a cover letter: greeting, body, sign-off, evidence notes and company information.

The model writes the whole letter (greeting, body, "Sincerely," and a name placeholder); the app checks the structure, fixes the
greeting and sign-off deterministically, and puts the real name in only when the letter is exported.
"""

from __future__ import annotations

import re

from jobhunt.writing.copycheck import SIGNATURE_PLACEHOLDER

SIGN_OFF = "Sincerely,"
_GREETING = re.compile(r"^\s*(?:dear|hi|hello|greetings|to whom)\b[^\n]*", re.IGNORECASE)
_COMPANY_CUES = re.compile(
    r"(?i)\b(?:our (?:mission|vision|platform|customers|clients|product|products|team|company|values)|we are (?:a|an|the)\b|"
    r"we(?:'re| are) (?:building|on a mission|leading|helping)|about (?:us|the company)|who we are|"
    r"(?:is|are) (?:a|an|the) (?:leading|global|fast-growing|growing)|founded in|headquartered)")


def greeting_line(company: str, contact_name: str | None) -> str:
    """"Dear Priya Nair," when the posting names a person, else "Dear <Company> Hiring Team,"."""
    if contact_name:
        return f"Dear {contact_name},"
    return f"Dear {company.strip()} Hiring Team," if company.strip() else "Dear Hiring Team,"


def split_letter(text: str) -> tuple[str, str, str]:
    """(greeting, body, sign-off) of a letter. A missing greeting or sign-off comes back as an empty string."""
    lines = text.strip().splitlines()
    greeting = ""
    if lines and _GREETING.match(lines[0]):
        greeting = lines.pop(0).strip()
    sign_off = ""
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip().lower().rstrip(",")
        if stripped in {"sincerely", "regards", "best regards", "kind regards", "warm regards", "best wishes", "yours sincerely", "yours faithfully",
                    "yours truly", "respectfully", "cordially", "with thanks", "thanks", "thank you"} \
                and index >= len(lines) - 4:
            sign_off = "\n".join(lines[index:]).strip()
            lines = lines[:index]
            break
    return greeting, "\n".join(lines).strip(), sign_off


def normalise_letter(text: str, greeting: str) -> str:
    """The letter with the app's greeting, its body untouched, and exactly "Sincerely," plus the name placeholder."""
    _, body, _ = split_letter(text)
    return f"{greeting}\n\n{body}\n\n{SIGN_OFF}\n{SIGNATURE_PLACEHOLDER}"


def body_word_count(text: str) -> int:
    return len(split_letter(text)[1].split())


def with_name(text: str, name: str) -> str:
    """The exported letter: the placeholder replaced by the real name (the model never sees the name)."""
    return text.replace(SIGNATURE_PLACEHOLDER, name)


def evidence_note(label: str, fact_text: str) -> str:
    """A fact as a short evidence note, not a ready-made sentence: the text is cut at commas, colons, semicolons and brackets
    into fragments (what, how, result) so the writer has to compose its own sentences."""
    fragments = [piece.strip(" .") for piece in re.split(r"[,;:()]", fact_text) if piece.strip(" .")]
    return f"[{label}] " + " | ".join(fragments)


def company_sentences(description: str, company: str, limit: int = 4) -> list[str]:
    """Sentences of the posting that say something about the company (its name, its mission, "we are ...", "our platform")."""
    name = company.strip().lower()
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", description):
        sentence = " ".join(sentence.split())
        if len(sentence) < 25 or len(sentence) > 320:
            continue
        low = sentence.lower()
        if (name and name in low and not re.search(r"(?i)equal opportunity|apply|resume|recruiter", low)) or _COMPANY_CUES.search(sentence):
            if sentence not in out:
                out.append(sentence)
        if len(out) >= limit:
            break
    return out


def posting_mentions_company(description: str, company: str) -> bool:
    return bool(company_sentences(description, company, limit=1))


__all__ = ["SIGN_OFF", "body_word_count", "company_sentences", "evidence_note", "greeting_line", "normalise_letter",
           "posting_mentions_company", "split_letter", "with_name"]
