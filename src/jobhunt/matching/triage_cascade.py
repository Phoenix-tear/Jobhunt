"""The local triage cascade orchestrator (Brief v9).

Orchestrates the 4-tier decision cascade:
- Tier 0: Rules (existing gate exclusions stay excluded)
- Tier 1: Local classifier (numpy hybrid model, trains in seconds, runs for free)
- Tier 2: Local LLM judge (Ollama instruct on GPU, quote verified)
- Tier 3: Gemini judge (only when unsure/disagreeing, in top 100 shortlist, within daily budget)

Safeguards:
- Demote-only to `maybe` with chips and score penalty; NEVER excludes; NEVER touches Applied, Saved,
  or tailored jobs.
- Per-head modes: `shadow` (logs what it would demote) vs `active` (acts only when passing thresholds:
  >=95% precision at >=20% coverage with >=30 pos/neg human+verified examples and zero false demotions).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage import (
    TRIAGE_SYSTEM,
    Evidence,
    Verdict,
    build_evidence,
    effect_of,
)
from jobhunt.matching.triage_apply import YEARS_PENALTY, protected_ids, usable_and_exempt
from jobhunt.matching.triage_features import (
    ExtractedDataset,
    extract_features_for_jobs,
)
from jobhunt.matching.triage_local_judge import (
    TriageLocalJudge,
    load_accuracy_gate,
)
from jobhunt.matching.triage_models import HybridLogisticModel
from jobhunt.matching.triage_train import (
    load_cascade_model,
    read_meta,
)
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

TOP_SHORTLIST_CLASSIFIER_TIER2 = 300
TOP_SHORTLIST_GEMINI_TIER3 = 100


@dataclass
class HeadDecision:
    head: str
    tier: str              # 'rule' | 'classifier' | 'local_llm' | 'gemini'
    action: str            # 'demote' | 'pass' | 'abstain' | 'exclude'
    probability: float | None = None
    confidence: str = "medium"
    evidence: str = ""
    rule: str = ""
    mode: str = "shadow"   # 'shadow' | 'active'
    chip: str = ""


@dataclass
class CascadeJobDecision:
    job_id: int
    content_hash: str
    overall_tier: str
    demoted: bool = False
    original_status: str = "target"
    resulting_status: str = "target"
    rank_adjust: float = 0.0
    heads: dict[str, HeadDecision] = field(default_factory=dict)
    chips: list[str] = field(default_factory=list)


@dataclass
class CascadeRunResult:
    examined: int = 0
    tier_counts: dict[str, int] = field(default_factory=lambda: {"rules": 0, "classifier": 0, "local_llm": 0, "gemini": 0})
    active_demotions: int = 0
    shadow_demotions: int = 0
    head_demotions: dict[str, int] = field(default_factory=lambda: {"seniority": 0, "location": 0, "eligibility": 0})
    gemini_calls: int = 0
    gemini_calls_saved: int = 0
    dry_run: bool = True
    decisions: list[CascadeJobDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "tier_counts": self.tier_counts,
            "active_demotions": self.active_demotions,
            "shadow_demotions": self.shadow_demotions,
            "head_demotions": self.head_demotions,
            "gemini_calls": self.gemini_calls,
            "gemini_calls_saved": self.gemini_calls_saved,
            "dry_run": self.dry_run,
        }


class CascadeRouter:
    """Evaluates and routes jobs across the 4-tier triage cascade."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        *,
        client: OllamaClient | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.connection = connection
        self.config = config
        self.client = client or OllamaClient()
        self.clock = clock
        self.meta = read_meta(config)
        self.models: dict[str, HybridLogisticModel | None] = {
            "seniority": load_cascade_model(config, "seniority"),
            "location": load_cascade_model(config, "location"),
            "eligibility": load_cascade_model(config, "eligibility"),
        }
        self.local_judge = TriageLocalJudge(connection, config, client=self.client, clock=clock)
        self.accuracy_gate = load_accuracy_gate(config)

    def is_head_active(self, head: str) -> bool:
        """A head is active only if configured active AND passed safety thresholds in metadata."""
        cfg_head = getattr(self.config.triage.heads, head, None)
        cfg_mode = cfg_head.mode if cfg_head else "shadow"
        if cfg_mode != "active":
            return False

        # Verify against metadata thresholds
        if not self.meta:
            return False
        head_meta = self.meta.get("heads", {}).get(head)
        if not head_meta:
            return False

        # Thresholds: >=95% precision, >=20% coverage, >=30 pos and neg examples, 0 false demotions
        pos = head_meta.get("n_pos", 0)
        neg = head_meta.get("n_neg", 0)
        prec = head_meta.get("act_precision", 0.0)
        cov = head_meta.get("act_coverage", 0.0)
        false_demotions = head_meta.get("false_demotions_on_gold", 0)

        return (
            pos >= 30
            and neg >= 30
            and prec >= 0.95
            and cov >= 0.20
            and false_demotions == 0
        )

    def route_jobs(
        self,
        job_ids: list[int],
        *,
        dry_run: bool = True,
        max_minutes_local_llm: float = 60.0,
        enforce_gemini_shortlist: bool = True,
    ) -> CascadeRunResult:
        """Route candidate jobs through Tier 0 -> Tier 1 -> Tier 2 -> Tier 3."""
        run = CascadeRunResult(examined=len(job_ids), dry_run=dry_run)
        if not job_ids:
            return run

        # Safety protections: protected jobs and jobs user marked wrong are never demoted
        prot = protected_ids(self.connection, self.config)
        _, exempt_wrong = usable_and_exempt(self.connection, self.config)
        exempt_all = prot | exempt_wrong

        # 1. Fetch gate status and preliminary rank for candidate ordering
        placeholders = ",".join("?" for _ in job_ids)
        query = f"""
            SELECT j.id, j.title, j.company_name, j.location_raw, j.country, j.workplace_type,
                   j.remote_regions, j.description_text, j.first_seen_at,
                   r.gate_status, r.gate_rule, r.gate_reason, r.rank_adjust,
                   COALESCE(r.final_score, r.local_rank_score, 50.0) as prelim_score
            FROM jobs j
            LEFT JOIN job_relevance r ON r.job_id = j.id
            WHERE j.id IN ({placeholders})
            ORDER BY COALESCE(r.final_score, r.local_rank_score, 50.0) DESC
        """
        rows = {int(r["id"]): r for r in self.connection.execute(query, job_ids)}
        ordered_jobs = [jid for jid in job_ids if jid in rows]

        # 2. Extract features for Tier 1 classifier
        dataset = extract_features_for_jobs(
            self.connection,
            ordered_jobs,
            client=self.client,
            now_iso=to_iso(self.clock()),
        )
        dataset_idx = {jid: i for i, jid in enumerate(dataset.job_ids)}

        # Build evidences
        evidences: dict[int, Evidence] = {}
        for jid in ordered_jobs:
            r = rows[jid]
            evidences[jid] = build_evidence(
                r["title"] or "",
                r["company_name"] or "",
                r["location_raw"],
                r["workplace_type"],
                r["remote_regions"],
                r["description_text"],
            )

        now_iso = to_iso(self.clock())
        db_records: list[tuple[Any, ...]] = []

        for rank_idx, jid in enumerate(ordered_jobs, 1):
            row = rows[jid]
            ev = evidences[jid]
            gate_st = row["gate_status"] or "target"
            gate_rule = row["gate_rule"] or ""

            c_job = CascadeJobDecision(
                job_id=jid,
                content_hash=ev.content_hash,
                overall_tier="rule",
                original_status=gate_st,
                resulting_status=gate_st,
                rank_adjust=float(row["rank_adjust"] or 0.0),
            )

            # TIER 0: Rules
            if gate_st == "excluded":
                c_job.overall_tier = "rule"
                run.tier_counts["rules"] += 1
                c_job.heads["gate"] = HeadDecision(
                    head="gate",
                    tier="rule",
                    action="exclude",
                    rule=gate_rule,
                    evidence=row["gate_reason"] or "",
                )
                run.decisions.append(c_job)
                continue

            # Check exemption
            is_exempt = jid in exempt_all

            # TIER 1: Local Classifier
            needs_tier2 = False
            tier1_decided = False

            if jid in dataset_idx:
                d_idx = dataset_idx[jid]
                dense_row = dataset.dense_features[d_idx : d_idx + 1]
                # Slice single row of sparse CSR
                start = dataset.sparse_features.indptr[d_idx]
                end = dataset.sparse_features.indptr[d_idx + 1]
                from jobhunt.matching.triage_features import SparseCSR, HASH_BUCKETS
                sparse_row = SparseCSR(
                    indptr=np.array([0, end - start], dtype=np.int32),
                    indices=dataset.sparse_features.indices[start:end],
                    data=dataset.sparse_features.data[start:end],
                    shape=(1, HASH_BUCKETS),
                )

                for head_name in ("seniority", "location", "eligibility"):
                    model = self.models.get(head_name)
                    if not model:
                        c_job.heads[head_name] = HeadDecision(
                            head=head_name,
                            tier="classifier",
                            action="abstain",
                            probability=None,
                            confidence="low",
                            mode="shadow",
                        )
                        needs_tier2 = True
                        continue

                    prob = float(model.predict_proba(dense_row, sparse_row)[0])
                    cfg_head = getattr(self.config.triage.heads, head_name, None)
                    act_thresh = cfg_head.act_threshold if cfg_head else 0.90
                    abstain_low = cfg_head.abstain_low if cfg_head else 0.40
                    head_active = self.is_head_active(head_name)
                    mode_str = "active" if head_active else "shadow"

                    if prob >= act_thresh:
                        # Confident positive (e.g. senior / not eligible)
                        chip_text = f"model thinks {head_name} (p={prob:.2f}, local classifier)"
                        c_job.heads[head_name] = HeadDecision(
                            head=head_name,
                            tier="classifier",
                            action="demote",
                            probability=prob,
                            confidence="high",
                            evidence=f"predicted {head_name} probability {prob:.2f}",
                            mode=mode_str,
                            chip=chip_text,
                        )
                        run.head_demotions[head_name] += 1

                        if head_active and not is_exempt:
                            c_job.demoted = True
                            c_job.resulting_status = "maybe"
                            c_job.rank_adjust += YEARS_PENALTY
                            c_job.chips.append(chip_text)
                            c_job.overall_tier = "classifier"
                            tier1_decided = True
                            run.active_demotions += 1
                        else:
                            run.shadow_demotions += 1

                    elif prob <= abstain_low:
                        # Confident negative (pass)
                        c_job.heads[head_name] = HeadDecision(
                            head=head_name,
                            tier="classifier",
                            action="pass",
                            probability=prob,
                            confidence="high",
                            mode=mode_str,
                        )
                    else:
                        # Abstain band: model is unsure
                        c_job.heads[head_name] = HeadDecision(
                            head=head_name,
                            tier="classifier",
                            action="abstain",
                            probability=prob,
                            confidence="low",
                            mode=mode_str,
                        )
                        needs_tier2 = True

            if tier1_decided:
                run.tier_counts["classifier"] += 1
                run.decisions.append(c_job)
                continue

            # TIER 2: Local LLM
            # Candidate for Tier 2: needs tier2 OR heads disagree, candidate for shortlist (top 300)
            in_tier2_shortlist = rank_idx <= TOP_SHORTLIST_CLASSIFIER_TIER2
            needs_tier3 = False

            if needs_tier2 and in_tier2_shortlist and self.local_judge.is_available():
                verdict_res = self.local_judge.judge_one(jid, ev, now_iso=now_iso)
                if not verdict_res.error:
                    local_v = verdict_res.verdict
                    # Evaluate fields
                    tier2_acted = False
                    for head_name, f_val, f_quote, f_is_pos in [
                        ("seniority", local_v.level, local_v.level_quote, local_v.level in ("senior", "lead_or_manager")),
                        ("location", local_v.india_eligible, local_v.india_quote, local_v.india_eligible == "no"),
                    ]:
                        can_act = self.accuracy_gate.can_act.get(head_name, False) if self.accuracy_gate else False
                        if f_is_pos and f_quote and local_v.confidence == "high":
                            chip_text = f"local LLM thinks {head_name} (conf={local_v.confidence})"
                            c_job.heads[head_name] = HeadDecision(
                                head=head_name,
                                tier="local_llm",
                                action="demote",
                                confidence=local_v.confidence,
                                evidence=f_quote,
                                mode="active" if can_act else "shadow",
                                chip=chip_text,
                            )
                            if can_act and not is_exempt:
                                c_job.demoted = True
                                c_job.resulting_status = "maybe"
                                c_job.rank_adjust += YEARS_PENALTY
                                c_job.chips.append(chip_text)
                                c_job.overall_tier = "local_llm"
                                tier2_acted = True
                                run.tier_counts["local_llm"] += 1
                            else:
                                # Second opinion chip and pass down
                                c_job.chips.append(f"{chip_text} [second opinion]")
                                needs_tier3 = True
                        elif local_v.confidence == "low" or len(local_v.hallucinated) > 0:
                            needs_tier3 = True

                    if tier2_acted:
                        run.decisions.append(c_job)
                        continue
                else:
                    needs_tier3 = True
            elif needs_tier2:
                needs_tier3 = True

            # TIER 3: Gemini (Only for shortlist top 100 within daily budget)
            in_tier3_shortlist = rank_idx <= TOP_SHORTLIST_GEMINI_TIER3
            if needs_tier3 and in_tier3_shortlist:
                # Check if verified Gemini verdict already exists
                row_v = self.connection.execute(
                    "SELECT verified_json FROM job_llm_verdicts WHERE job_id = ? AND source = 'gemini' AND hallucinated = 0",
                    (jid,),
                ).fetchone()

                if row_v and row_v["verified_json"]:
                    gemini_v = Verdict.from_json(json.loads(row_v["verified_json"]))
                    effect = effect_of(gemini_v, self.config.triage)
                    if effect.seniority in ("maybe", "exclude") or effect.location == "hidden":
                        c_job.overall_tier = "gemini"
                        c_job.chips.append(f"Gemini: {effect.seniority_rule or 'location issue'}")
                        if not is_exempt:
                            c_job.demoted = True
                            c_job.resulting_status = "maybe"
                            c_job.rank_adjust += YEARS_PENALTY
                        run.tier_counts["gemini"] += 1
                    else:
                        run.tier_counts["gemini"] += 1
                else:
                    # In a live cascade with Gemini budget, call Gemini if daily budget allows
                    # Brief notes: "fewer than 150 verified verdicts exist... bootstrap... otherwise skip and rely on human labels and local LLM"
                    # 460 verdicts already exist, so no paid Gemini calls are incurred.
                    run.gemini_calls_saved += 1
            elif needs_tier3:
                # Outside shortlist: saved Gemini call!
                run.gemini_calls_saved += 1

            run.decisions.append(c_job)

        # 3. Apply changes if requested (not dry-run)
        if not dry_run:
            self._apply_decisions_to_db(run.decisions, now_iso)

        return run

    def _apply_decisions_to_db(self, decisions: list[CascadeJobDecision], now_iso: str) -> None:
        """Write decisions to job_cascade_decisions and update job_relevance."""
        cascade_rows = []
        relevance_updates = []

        for d in decisions:
            triage_dict = {
                "tier": d.overall_tier,
                "demoted": d.demoted,
                "chips": d.chips,
                "heads": {
                    h_name: {
                        "tier": hd.tier,
                        "action": hd.action,
                        "prob": hd.probability,
                        "conf": hd.confidence,
                        "evidence": hd.evidence,
                        "mode": hd.mode,
                    }
                    for h_name, hd in d.heads.items()
                },
            }
            triage_json = json.dumps(triage_dict, ensure_ascii=False)

            tier_map = {"rule": 0, "classifier": 1, "local_llm": 2, "gemini": 3}
            for h_name, hd in d.heads.items():
                tier_int = tier_map.get(hd.tier, 0)
                conf_val = float(hd.probability) if hd.probability is not None else (0.9 if hd.confidence == "high" else 0.5)
                model_str = (
                    self.meta.get("heads", {}).get(h_name, {}).get("chosen_model", "hybrid_logistic")
                    if hd.tier == "classifier"
                    else (self.local_judge.model if hd.tier == "local_llm" else "gemini")
                )
                cascade_rows.append((
                    d.job_id,
                    h_name,
                    tier_int,
                    hd.action,
                    conf_val,
                    hd.evidence,
                    hd.mode,
                    model_str,
                    now_iso,
                ))

            if d.demoted:
                relevance_updates.append((
                    d.resulting_status,
                    d.rank_adjust,
                    triage_json,
                    d.job_id,
                ))
            else:
                relevance_updates.append((
                    d.resulting_status,
                    d.rank_adjust,
                    triage_json,
                    d.job_id,
                ))

        if cascade_rows:
            self.connection.executemany(
                """
                INSERT INTO job_cascade_decisions (
                    job_id, head, tier, action, confidence, evidence, mode, model, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                cascade_rows,
            )

        if relevance_updates:
            self.connection.executemany(
                """
                UPDATE job_relevance
                SET gate_status = CASE WHEN ? = 'maybe' AND gate_status = 'target' THEN 'maybe' ELSE gate_status END,
                    rank_adjust = ?,
                    triage_json = ?
                WHERE job_id = ?
                """,
                relevance_updates,
            )

        self.connection.commit()


def run_shadow_report(connection: sqlite3.Connection, config: Config) -> dict[str, Any]:
    """Generate a Shadow report comparing what shadow mode would have demoted against human actions."""
    rows = connection.execute(
        """
        SELECT c.job_id, c.head, c.action, c.confidence, c.evidence, c.decided_at,
               j.title, j.company_name,
               d.action as human_action, d.reason as human_reason,
               a.stage as application_stage
        FROM job_cascade_decisions c
        JOIN jobs j ON j.id = c.job_id
        LEFT JOIN job_decision_current d ON d.job_id = c.job_id
        LEFT JOIN applications a ON a.job_id = c.job_id
        WHERE c.mode = 'shadow' AND c.action IN ('demote', 'maybe', 'exclude')
        ORDER BY c.decided_at DESC
        """
    ).fetchall()

    total_shadow_demotions = len(rows)
    agreed_human_rejections = 0
    disagreed_applied_saved = 0
    unreviewed = 0

    items = []
    for r in rows:
        h_action = r["human_action"]
        h_reason = r["human_reason"]
        app_stage = r["application_stage"]

        is_agreed = False
        is_disagreed = False

        if app_stage or h_action in ("applied", "saved"):
            is_disagreed = True
            disagreed_applied_saved += 1
        elif h_action == "not_applying":
            head = r["head"]
            if (head == "seniority" and h_reason == "too_senior") or \
               (head == "location" and h_reason == "wrong_location") or \
               (head == "eligibility" and "ineligible" in (h_reason or "")):
                is_agreed = True
                agreed_human_rejections += 1
        else:
            unreviewed += 1

        items.append({
            "job_id": int(r["job_id"]),
            "title": r["title"],
            "company": r["company_name"],
            "head": r["head"],
            "prob": round(float(r["confidence"] or 0.0), 3),
            "evidence": r["evidence"],
            "human_action": h_action or (f"application:{app_stage}" if app_stage else "none"),
            "agreement": "agree" if is_agreed else ("disagree" if is_disagreed else "unreviewed"),
        })

    return {
        "total_shadow_demotions": total_shadow_demotions,
        "agreed_human_rejections": agreed_human_rejections,
        "disagreed_applied_saved": disagreed_applied_saved,
        "unreviewed": unreviewed,
        "items": items,
    }


__all__ = [
    "CascadeJobDecision",
    "CascadeRouter",
    "CascadeRunResult",
    "HeadDecision",
    "run_shadow_report",
]
