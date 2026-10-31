"""One click of the staged writer: plan -> write -> edit, with the fallback ladder, per-stage cost and the quality badge.

Letters (v8): write with the configured model; if that fails (cut off, API error, validator or copy-detection rejection) retry ONCE on the
same model with the rejection reasons in the prompt, then on the fallback (flash) model with the same feedback. At most 6 calls and
$0.10 per click. If every rung fails the click ends with the reason in plain words and a clearly labelled facts outline: the old
deterministic template is never presented as a letter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jobhunt.matching.budget import estimate_cost, estimate_tokens
from jobhunt.matching.gemini import GeminiError
from jobhunt.timeutil import utc_now
from jobhunt.writing import letter_prompts, staged
from jobhunt.writing.copycheck import CopyReport, check_copying
from jobhunt.writing.letter import (
    body_word_count, company_sentences, evidence_note, greeting_line, normalise_letter, posting_mentions_company, split_letter,
)
from jobhunt.writing.quality import QualityReport, check_quality
from jobhunt.writing.validator import WritingVerdict, validate_letter, validate_text, word_count
from jobhunt.writing.voice import VoiceSamples

if TYPE_CHECKING:
    from jobhunt.writing.service import WritingResult, WritingService

MAX_CALLS_PER_CLICK = 6
MAX_USD_PER_CLICK = 0.10
REALISTIC_OUTPUT_TOKENS = 2500          # what the per-click dollar limit assumes for a call that has not happened yet
WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "cited_fact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 14},
        "refusal": {"type": "string"},
    },
    "required": ["text", "cited_fact_ids", "refusal"],
    "additionalProperties": False,
}


def describe_violation(violation: dict[str, str]) -> str:
    """One violation as an instruction for the retry ("your draft mentioned 85, which is not in the facts: remove it")."""
    rule, offending = violation.get("rule", ""), violation.get("offending", "")
    templates = {
        "number_not_in_evidence": f"it used the number {offending}, which is not in the evidence: remove it",
        "number_not_in_cited_facts": f"it used the number {offending}, which is not in the facts: remove it",
        "spelled_number_not_in_evidence": f"it wrote the number '{offending}', which is not in the evidence: remove it",
        "unknown_technology_or_name": f"it mentioned {offending}, which is not in the evidence: remove it",
        "employer_not_in_facts": f"it named the organisation {offending}, which is not in the evidence: remove it",
        "role_not_in_facts": f"it claimed the role '{offending}': remove it",
        "states_years_of_experience": "it stated years of experience: remove it",
        "copied_run_from_facts": f"a sentence copied eight or more words from the evidence ('{offending}'): rewrite it in your own words",
        "copied_run_from_posting": f"a sentence copied eight or more words from the posting ('{offending}'): rewrite it",
        "copied_run_from_sample": f"a sentence copied eight or more words from the style sample ('{offending}'): rewrite it",
        "too_much_copied_text": "too much of the letter was copied from the evidence, posting or sample: paraphrase everything",
        "instruction_like_sentence": f"a sentence read like an instruction ('{offending}'): remove it",
        "too_short": f"it was too short ({offending}): write more",
        "too_long": f"it was too long ({offending}): shorten it",
    }
    return templates.get(rule, f"{rule.replace('_', ' ')} ({offending})" if offending else rule.replace("_", " "))


@dataclass
class Candidate:
    text: str
    cited: list[str]
    verdict: WritingVerdict
    quality: QualityReport
    model: str
    edited: bool
    copy: CopyReport | None = None


@dataclass
class StagedRun:
    service: "WritingService"
    result: "WritingResult"
    job: Any
    job_view: dict[str, str]
    facts: dict[str, Any]
    kind: str
    question: str | None
    lower: int | None
    upper: int
    plan_model: str
    model: str
    voice: VoiceSamples
    profile_text: str = ""
    contact_name: str | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    calls: int = 0
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    blocked: str = ""
    declined: str = ""
    plan_blocks: list[dict[str, Any]] = field(default_factory=list)
    plan_source: str = ""
    job_angle: str = ""
    company_flag: bool = False
    best: Candidate | None = None
    last_rejected: list[dict[str, str]] = field(default_factory=list)
    last_failure: str = ""
    rounds: int = 0
    logged_ids: set[int] = field(default_factory=set)

    # -- bookkeeping ---------------------------------------------------------------------------------

    def _log(self, record: dict[str, Any]) -> None:
        if id(record) not in self.logged_ids:
            self.logged_ids.add(id(record))
            self.stages.append(record)

    def note(self, message: str) -> None:
        if message and message not in self.notes:
            self.notes.append(message)

    @property
    def call_limit(self) -> int:
        return min(MAX_CALLS_PER_CLICK, getattr(self.service, "call_limit", None) or MAX_CALLS_PER_CLICK)

    @property
    def usd_limit(self) -> float:
        return float(getattr(self.service.config.writing, "max_usd_per_click", MAX_USD_PER_CLICK))

    def _call(self, stage: str, name: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int,
              temperature: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """One model call on one model. Returns (parsed JSON or None, the stage record). A cut-off answer (finish reason MAX_TOKENS,
        or an output that used the whole budget) is never accepted, even if it happens to parse."""
        service = self.service
        record: dict[str, Any] = {"stage": stage, "model": name, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                  "accepted": False, "note": "", "attempt": self.rounds}
        self._log(record)
        if self.calls >= self.call_limit:
            self.blocked = f"call limit for one click reached ({self.call_limit})"
            record["note"] = self.blocked
            return None, record
        prompt_tokens = estimate_tokens(system) + estimate_tokens(prompt)
        if self.cost + estimate_cost(service.config.pricing, name, prompt_tokens, REALISTIC_OUTPUT_TOKENS,
                                     utc_now().date()) > self.usd_limit:
            self.blocked = f"cost limit for one click reached (${self.usd_limit:.2f})"
            record["note"] = self.blocked
            return None, record
        if service.tailor.guard is not None:
            decision = service.tailor.guard.check_generate(
                estimate_cost(service.config.pricing, name, prompt_tokens, max_tokens, utc_now().date()), interactive=True)
            if not decision:
                self.blocked = decision.reason
                record["note"] = f"over budget: {decision.reason}"
                self.note(f"over budget: {decision.reason}")
                return None, record
        client = service.tailor.client
        if name != self.model and name != self.plan_model and service.tailor.fallback_client is not None:
            client = service.tailor.fallback_client
        thinking = None if name.startswith(("gemini-2.5", "claude")) else service.config.models.score_thinking
        extra: dict[str, Any] = {}
        if name.startswith("gemini-2.5") and service.config.writing.thinking_budget:
            extra["thinking_budget"] = int(service.config.writing.thinking_budget)
        try:
            answer = client.generate_json(name, system_instruction=system, user_text=prompt, response_schema=schema,
                                          thinking_level=thinking, max_output_tokens=max_tokens, temperature=temperature, **extra)
        except GeminiError as error:
            record["note"] = f"API error ({str(error)[:80]})"
            self.last_failure = "the model call failed"
            self.note(f"{stage}: {name} failed ({str(error)[:80]})")
            return None, record
        spent = 0.0
        if service.tailor.ledger is not None:
            spent = service.tailor.ledger.record(name, self.kind, input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
                                                 note=f"{self.kind} {stage} job {self.job.job_id}")
            service.connection.commit()
        self.calls += 1
        self.cost += spent
        self.tokens_in += answer.input_tokens
        self.tokens_out += answer.output_tokens
        record.update(input_tokens=answer.input_tokens, output_tokens=answer.output_tokens, cost_usd=round(spent, 6))
        finish = getattr(answer, "finish_reason", None)
        if finish == "MAX_TOKENS" or answer.output_tokens >= max_tokens - 20:
            why = (f"output cut off at {answer.output_tokens} of {max_tokens} tokens (thinking "
                   f"{getattr(answer, 'thinking_tokens', 0)})")
            record["note"] = why
            self.last_failure = "the answer was cut off before it finished"
            self.note(f"{stage}: {name} returned no usable answer ({why})")
            return None, record
        try:
            data = json.loads(answer.text)
        except ValueError:
            record["note"] = "invalid JSON"
            self.last_failure = "the answer was not valid JSON"
            self.note(f"{stage}: {name} returned invalid JSON")
            return None, record
        if not isinstance(data, dict):
            record["note"] = "unexpected JSON shape"
            self.last_failure = "the answer had the wrong shape"
            return None, record
        return data, record

    # -- the run ---------------------------------------------------------------------------------------

    def execute(self) -> None:
        settings = self.service.config.writing
        if self.kind == "cover_letter":
            if not self._plan():
                return
        fallback = self.service.config.tailoring.fallback_model
        rungs = [(self.model, False), (self.model, True)]
        if fallback and fallback != self.model:
            rungs.append((fallback, True))
        extra_for_quality = 1
        for name, with_feedback in rungs:
            if self.blocked or self.declined:
                break
            if self.best is not None and self.best.verdict.ok and self.best.quality.ok:
                break
            if self.best is not None and self.best.verdict.ok:
                if extra_for_quality <= 0 or self.rounds >= max(1, settings.max_attempts) + 1:
                    break
                extra_for_quality -= 1
            self.rounds += 1
            self._round(name, self._feedback() if (with_feedback or self.best is not None) else "")

    def _feedback(self) -> str:
        parts = []
        if self.last_rejected:
            parts.append("Your previous draft was rejected: " + "; ".join(describe_violation(v) for v in self.last_rejected[:6])
                         + ". Fix exactly that and keep everything else.")
        elif self.last_failure:
            parts.append(f"Your previous attempt failed because {self.last_failure}. Write the complete letter again, concisely.")
        if self.best is not None and not self.best.quality.ok:
            parts.append("The previous draft read badly: " + "; ".join(
                f"{w.rule} ({w.detail})" for w in self.best.quality.warnings[:6]) + ". Rewrite so it reads well.")
        return " ".join(parts)

    # -- stage 1: plan ---------------------------------------------------------------------------------

    def _plan(self) -> bool:
        service = self.service
        usable = {fid for fid, fact in self.facts.items() if fact.kind != "skill"}
        prompt = service._plan_prompt(self.job, self.facts, self.lower or 220, self.upper)
        data, record = self._call("plan", self.plan_model, letter_prompts.LETTER_PLAN_SYSTEM, prompt, letter_prompts.LETTER_PLAN_SCHEMA,
                                  service.config.tailoring.max_output_tokens, 0.2)
        if data is None and self.blocked:
            return False
        blocks: list[dict[str, Any]] = []
        deterministic_company = posting_mentions_company(self.job.description, self.job.company)
        if data is not None:
            refusal = str(data.get("refusal", "")).strip()
            self.job_angle = str(data.get("job_angle", ""))[:240]
            if refusal and not data.get("paragraphs"):
                record["note"] = f"declined: {refusal[:120]}"
                self.declined = refusal
                self.note(f"the model declined to plan: {refusal[:200]}")
                return False
            blocks, check = staged.validate_plan_blocks(data, usable)
            if check.ok:
                record["accepted"] = True
                self.plan_source = "model"
                self.plan_blocks = blocks
                self.company_flag = bool(data.get("company_paragraph")) and deterministic_company
                return True
            record["note"] = "plan rejected: " + "; ".join(check.problems[:3])
            self.note("plan rejected (" + "; ".join(check.problems[:2]) + "); used the deterministic plan")
        else:
            self.note("no model plan; used the deterministic plan")
        self.plan_blocks = service._fallback_plan(self.job, self.facts)
        self.plan_source = "deterministic"
        self.company_flag = deterministic_company
        ok = len(staged.cited_from_plan(self.plan_blocks)) >= 2
        if not ok:
            self.note("the ticked facts are too few to plan a letter")
        return ok

    # -- stages 2 and 3: write, then edit ----------------------------------------------------------------

    def _evidence_lines(self, ids: list[str]) -> tuple[str, list[str]]:
        lines, all_ids = self.service._fact_lines(self.facts, ids)
        parents = {f.id: f for f in self.service.tailor.bases.merged}
        notes = []
        for fid in all_ids:
            fact = self.facts[fid]
            parent = parents.get(fact.entry)
            label = (parent.text.split(" | ")[0] if parent is not None and fact.kind.endswith("_bullet") else fact.kind)
            notes.append(f"{fid} {evidence_note(label, self.service_strip(fact.text))}")
        return "EVIDENCE NOTES (fragments to paraphrase, not sentences to copy):\n" + "\n".join(notes), all_ids

    @staticmethod
    def service_strip(text: str) -> str:
        from jobhunt.writing.service import _strip_label

        return _strip_label(text)[:320]

    def _letter_prompt(self, feedback: str) -> tuple[str, list[str]]:
        ids = staged.cited_from_plan(self.plan_blocks)
        evidence, allowed = self._evidence_lines(ids)
        lo, hi = (self.lower or 220), self.upper
        target = (lo + hi) // 2
        plan_lines = []
        for index, block in enumerate(self.plan_blocks, 1):
            why = f" | why it matters for this role (posting's words): {block['why_for_role']}" if block.get("why_for_role") else ""
            plan_lines.append(f"{index}. {block['intent']}: {block['point']}{why} [notes: {', '.join(block['fact_ids']) or 'none'}]")
        greeting = greeting_line(self.job.company, self.contact_name)
        company = ""
        if self.company_flag:
            info = company_sentences(self.job.description, self.job.company)
            company = "COMPANY PARAGRAPH: yes\nCOMPANY INFORMATION (from the posting):\n" + "\n".join(f"- {s}" for s in info)
        else:
            company = "COMPANY PARAGRAPH: no (the posting says nothing usable about the company; write a role-focused paragraph)"
        style = ""
        if self.voice.present:
            style = f"\n\n{staged.VOICE_INSTRUCTION}\n<style_reference>\n{self.voice.text}\n</style_reference>"
        extra = f"\n\n{feedback}" if feedback else ""
        task = (f"TASK: write the cover letter: a body of about {target} words, never under {lo} and never over {hi} "
                f"(greeting and sign-off not counted).\n"
                f"GREETING LINE (copy exactly): {greeting}\nPROFILE:\n{self.profile_text}\nJOB ANGLE (from the posting): "
                f"{self.job_angle or 'see the posting'}\nPLAN:\n" + "\n".join(plan_lines) + f"\n{company}")
        job = f"\n\n<job_posting>\ntitle: {self.job.title}\ncompany: {self.job.company}\n{self.job.compact}\n</job_posting>"
        return f"{task}\n\n{evidence}{style}{extra}{job}", allowed

    def _answer_prompt(self, feedback: str) -> tuple[str, list[str]]:
        lines = [f"{f.id} | {f.kind} | {self.service_strip(f.text)}" for f in self.facts.values() if f.kind != "skill"]
        skills = "; ".join(f"{f.id}={f.text[:40]}" for f in self.facts.values() if f.kind == "skill")[:1500]
        facts_block = "FACTS:\n" + "\n".join(lines) + (f"\nSKILLS (cite the skill ids if used): {skills}" if skills else "")
        style = ""
        if self.voice.present:
            style = f"\n\n{staged.VOICE_INSTRUCTION}\n<style_reference>\n{self.voice.text}\n</style_reference>"
        extra = f"\n\n{feedback}" if feedback else ""
        task = (f"TASK: answer the application question below in at most {self.upper} words, first person, one to three short "
                f"paragraphs, grounded only in the facts. Pick the facts that answer it best.")
        job = f"\n\n<job_posting>\ntitle: {self.job.title}\ncompany: {self.job.company}\n{self.job.compact}\n</job_posting>"
        return f"{task}\n\n{facts_block}{style}{extra}{job}\n<question>\n{self.question}\n</question>", [f.id for f in self.facts.values()]

    def _edit_prompt(self, text: str, warnings: list[str]) -> str:
        rng = f"{self.lower} to {self.upper} words in the body" if self.kind == "cover_letter" else f"at most {self.upper} words"
        problems = ("\nKnown problems to fix: " + "; ".join(warnings[:8]) + ".") if warnings else ""
        return (f"Edit the text inside <text>. Keep it {rng}. Role (context only): {self.job.title} at {self.job.company}."
                f"{problems}\n<text>\n{text}\n</text>")

    def exempt_names(self) -> list[str]:
        """Phrases a letter cannot paraphrase: degree, university, graduation line, job title, company, project and entry names."""
        names = [self.job.title, self.job.company]
        for line in self.profile_text.splitlines():
            names.append(line.split(":", 1)[-1].strip())
        for fact in self.facts.values():
            if fact.kind in {"project", "experience"}:
                names.append(fact.text.split(" | ")[0])
        return names

    def _check_letter(self, text: str, cited: list[str], samples: str) -> tuple[WritingVerdict, CopyReport]:
        """The letter validator plus copy detection on the body, merged into one verdict."""
        verdict = validate_letter(text, cited_ids=cited, facts=self.facts, job=self.job_view, profile_text=self.profile_text,
                                  min_words=self.lower, max_words=self.upper)
        _, body, _ = split_letter(text)
        parents = {f.id: f for f in self.service.tailor.bases.merged}
        fact_texts = []
        for fid in cited:
            if fid in self.facts:
                fact_texts.append(self.facts[fid].text)
                parent = parents.get(self.facts[fid].entry)
                if parent is not None:
                    fact_texts.append(parent.text)
        copy = check_copying(body, facts=fact_texts, posting=self.job_view.get("description", ""), samples=samples,
                             exempt=self.exempt_names())
        for violation in copy.violations:
            verdict.add(violation["rule"], violation["offending"], violation["source"])
        return verdict, copy

    def _round(self, name: str, feedback: str) -> bool:
        """One write (+ edit) on one model. Returns False when nothing more can be done (blocked, declined)."""
        service = self.service
        letter = self.kind == "cover_letter"
        prompt, _ = self._letter_prompt(feedback) if letter else self._answer_prompt(feedback)
        system = letter_prompts.LETTER_WRITE_SYSTEM if letter else staged.WRITE_SYSTEM
        max_tokens = service.config.writing.max_output_tokens
        data, record = self._call("write", name, system, prompt, WRITE_SCHEMA, max_tokens, 0.4)
        if data is None:
            self.last_rejected = []
            return not self.blocked
        text = str(data.get("text", "")).strip()
        cited = [str(c) for c in data.get("cited_fact_ids", []) if isinstance(c, str)]
        refusal = str(data.get("refusal", "")).strip()
        if not text:
            record["note"] = f"declined: {refusal[:120]}" if refusal else "empty text"
            if refusal:
                self.declined = refusal
                self.note(f"the model declined: {refusal[:200]}")
            self.last_failure = "the answer had no text"
            return not refusal
        samples = self.voice.text if self.voice.present else ""
        copy: CopyReport | None = None
        if letter:
            greeting = greeting_line(self.job.company, self.contact_name)
            text = normalise_letter(text, greeting)
            verdict, copy = self._check_letter(text, cited, samples)
        else:
            verdict = validate_text(text, kind=self.kind, cited_ids=cited, facts=self.facts, job=self.job_view,
                                    min_words=self.lower, max_words=self.upper)
        if not verdict.ok:
            self.last_rejected = verdict.violations
            self.last_failure = ""
            record["violations"] = verdict.violations[:8]
            record["draft"] = text[:2600]
            record["note"] = "rejected by the validator: " + ", ".join(v["rule"] for v in verdict.violations[:4])
            self.note(f"draft {self.rounds} rejected: " + ", ".join(v["rule"] for v in verdict.violations[:4]))
            return True
        record["accepted"] = True
        self.last_rejected, self.last_failure = [], ""
        final_text, edited = text, False
        quality_text = split_letter(text)[1] if letter else text
        quality = check_quality(quality_text, kind=self.kind, min_words=self.lower, max_words=self.upper)
        run_edit = service.config.writing.edit_enabled and (letter or not quality.ok)
        if run_edit and self.calls < self.call_limit:
            warnings = [f"{w.rule}: {w.detail}" if w.detail else w.rule for w in quality.warnings]
            edit_system = letter_prompts.LETTER_EDIT_SYSTEM if letter else staged.EDIT_SYSTEM
            edit_data, edit_record = self._call("edit", name, edit_system, self._edit_prompt(text, warnings), staged.EDIT_SCHEMA,
                                                max_tokens, 0.2)
            if edit_data is not None:
                edited_text = str(edit_data.get("text", "")).strip()
                if letter:
                    edited_text = normalise_letter(edited_text, greeting_line(self.job.company, self.contact_name)) if edited_text else ""
                judgement = staged.judge_edit(text, edited_text, cited_ids=cited, facts=self.facts, job=self.job_view, kind=self.kind,
                                              min_words=self.lower, max_words=self.upper, letter=letter,
                                              profile_text=self.profile_text)
                if judgement.accepted and letter:
                    again, _ = self._check_letter(edited_text, cited, samples)
                    if not again.ok:
                        judgement = staged.EditVerdict(False, "the edit failed the copy or letter checks: "
                                                       + ", ".join(v["rule"] for v in again.violations[:4]))
                if judgement.accepted:
                    edit_record["accepted"] = True
                    final_text, edited = edited_text, True
                else:
                    edit_record["note"] = judgement.reason
                    self.note(f"edit {self.rounds} not used: {judgement.reason}")
        quality_text = split_letter(final_text)[1] if letter else final_text
        final_quality = check_quality(quality_text, kind=self.kind, min_words=self.lower, max_words=self.upper)
        if letter:
            final_verdict, final_copy = self._check_letter(final_text, cited, samples)
        else:
            final_verdict = validate_text(final_text, kind=self.kind, cited_ids=cited, facts=self.facts, job=self.job_view,
                                          min_words=self.lower, max_words=self.upper)
            final_copy = None
        candidate = Candidate(final_text, cited, final_verdict, final_quality, name, edited, final_copy)
        if self.best is None or len(candidate.quality.warnings) < len(self.best.quality.warnings):
            self.best = candidate
        return True

    # -- the outcome -----------------------------------------------------------------------------------

    def finish(self) -> None:
        service, result = self.service, self.result
        result.input_tokens, result.output_tokens, result.cost_usd = self.tokens_in, self.tokens_out, self.cost
        result.stages, result.calls, result.pipeline = self.stages, self.calls, "staged"
        result.plan = {"source": self.plan_source, "job_angle": self.job_angle, "company_paragraph": self.company_flag,
                       "blocks": self.plan_blocks} if self.plan_blocks else {}
        result.rejected_violations = self.last_rejected
        write_stages = [s for s in self.stages if s["stage"] == "write" and s["input_tokens"]]
        if write_stages:
            result.model = write_stages[-1]["model"]
        if self.best is not None:
            best = self.best
            result.text, result.cited, result.source = best.text, best.cited, "model"
            result.validation = best.verdict.as_dict()
            if best.copy is not None:
                result.validation["copy_check"] = best.copy.as_dict()
            result.quality = best.quality.as_dict()
            result.model = best.model
            if not best.quality.ok:
                self.note("shown with language warnings: edit it, or regenerate")
            return
        reason = self._forced_reason()
        self.note(f"no letter was produced: {reason}")
        if self.kind == "cover_letter":
            service._outline(result, self.job, self.facts)
            result.quality = {}
        else:
            result.source = "refusal"
            result.text = (f"I can't draft a grounded answer for this one: {reason}. "
                           "Answer it yourself from your resume facts, or try again later.")
            result.validation = {"ok": True, "violations": [], "words": word_count(result.text),
                                 "note": "refusal message, no claims made"}
            result.quality = check_quality(result.text, kind="answer").as_dict()

    def _forced_reason(self) -> str:
        if self.blocked:
            return f"the run was stopped ({self.blocked})"
        if self.declined:
            return f"the model declined ({self.declined[:120]})"
        if self.last_rejected:
            return ("every draft was rejected by the checks (" + "; ".join(describe_violation(v) for v in self.last_rejected[:3]) + ")")
        if self.last_failure:
            return self.last_failure
        if not self.model:
            return "no model is configured"
        return "the model gave no usable text"

    def message(self) -> str:
        stage_text = ", ".join(f"{s['stage']} {'ok' if s['accepted'] else 'not used'}" for s in self.stages if s["input_tokens"] or s["note"])
        head = f"{self.calls} call(s): {stage_text}" if self.stages else ""
        return "; ".join(part for part in (head, *self.notes) if part)[:900]


__all__ = ["Candidate", "MAX_CALLS_PER_CLICK", "MAX_USD_PER_CLICK", "StagedRun", "WRITE_SCHEMA", "describe_violation"]
