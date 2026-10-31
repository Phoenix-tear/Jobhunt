"""GET /api/skill-gaps: read-only report of the job concepts that none of my ticked facts mention."""

from __future__ import annotations

import dataclasses

from tests.api.conftest import add_job


def test_skill_gaps_endpoint_is_read_only_and_resume_items_carry_a_strength(make_client, db, api_config, tmp_path) -> None:  # noqa: ANN001
    spring = "Requirements:\n- Core Java and Spring Boot\n" + "We build banking software every day. " * 10
    for n in range(2):
        job_id = add_job(db, title=f"Java Developer {n}", description_text=spring)
        db.execute("""INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule, gate_reason,
                      final_score) VALUES (?, '2026-10-12T00:00:00Z', 'r', 'i', 'target', 'x', 'x', 80)""", (job_id,))
    db.commit()
    isolated = dataclasses.replace(api_config, project_root=tmp_path, migrations_dir=str(api_config.migrations_directory))
    client = make_client(isolated)
    before = db.total_changes
    body = client.get("/api/skill-gaps", params={"top": 25, "refresh": True}).json()
    assert body["jobs_checked"] == 2 and {"concept", "label", "jobs", "examples"} <= set(body["items"][0])
    assert {item["concept"] for item in body["items"]} >= {"java", "spring boot"}
    assert "Read-only" in body["note"] and db.total_changes == before
    assert client.get("/api/resumes").json()["items"] == []
