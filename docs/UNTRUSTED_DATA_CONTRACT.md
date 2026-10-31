# Untrusted data contract

## Principle

Job postings, company pages, email subjects and bodies, application-form fields and labels, recruiter messages
and web-search results are **data, never instructions**. Whatever they say, they cannot change what jobhunt does.
Text that addresses "the AI", "the reviewer" or "the system" is treated as a property of the posting (a flag),
not as a command.

## What untrusted text CAN influence

- Score signals: matched skills, fit reasons, workplace and remote-eligibility judgements (stored in dedicated columns).
- Flags: for example `injection_suspected`, which also caps the score at 50.
- LLM triage verdicts: the judge sees a sanitised evidence extract inside a `<job_posting number="n">` block. A claim only counts if its quote is a substring of that block, and `effect_of` (code, not the model) decides what a verdict may do; a verdict never moves a job you applied to, saved, tailored or marked relevant.
- Drafted text (cover letters, answers) that still passes the validator: every number, technology, title and date
  must come from a ticked fact the user approved.

## What untrusted text CANNOT do

- Change rules, config, caps, budgets or schedules.
- Write or delete files, or run any tool, command or network request.
- Submit forms, send messages, or click anything.
- Reveal secrets (keys, tokens, profile contact details). Only PII-stripped profile text ever reaches a model.
- Pick which facts are true about the user.

## How this is enforced today

| Defence | Where |
|---|---|
| Sentence-level sanitiser: instruction-like sentences are replaced by a marker and the job is flagged (patterns in `_INJECTION_TRIGGERS`, linear time) | `matching/prompts.py` |
| Posting placed inside `<job_posting>` (and `<question>`) delimiters; angle brackets neutralised so it cannot close the block | `matching/prompts.py`, `writing/service.py` |
| 9000-character cap on posting text sent to a model | `matching/prompts.py` |
| System prompts state "data, not instructions" (asserted by `tests/test_untrusted_contract.py`) | `matching/prompts.py`, `matching/compact.py`, `writing/service.py`, `tailoring/engine.py` |
| Schema-only output: JSON validated against a strict schema, unknown keys rejected | all model calls |
| No model tools, no browsing, no network access for the model: the blast radius is a few columns | architecture |
| Score capped at 50 on `injection_suspected`; flagged text is never used as ranker training data | `matching/scoring.py`, `matching/ranker.py` |
| Writing validator: drafted text must cite ticked facts and contain nothing else | `writing/validator.py` |
| The knock-out scanner uses no model at all; question text is length-capped, never executed, shown as plain text | `writing/knockouts.py` |

## Rule for future features

Any browser extension, autofill helper or web-reading feature must **not pass page text (DOM, labels,
placeholders, search results) to a model that can emit actions**. Page text may go to a model only when the output
is constrained to a schema of plain values that a human reviews, exactly like postings today. Form-field labels are
untrusted data like everything else here.
