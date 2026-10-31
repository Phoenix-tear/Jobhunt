from pathlib import Path

import pytest

from jobhunt.config import load_config
from jobhunt.db import connect
from jobhunt.matching.golden import capture_golden_snapshot, compare_golden_snapshots

def test_golden_snapshot_and_compare(tmp_path: Path):
    cfg = load_config()
    if not cfg.database_file.is_file():
        pytest.skip("golden snapshot needs a populated jobs database; none exists in this checkout")
    conn = connect(cfg.database_file)
    has_jobs_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
    if not has_jobs_table or conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'open'").fetchone()[0] == 0:
        conn.close()
        pytest.skip("golden snapshot needs a populated jobs database (no open jobs in this checkout)")
    snap1 = tmp_path / "snap1"
    snap2 = tmp_path / "snap2"

    stats1 = capture_golden_snapshot(conn, cfg, snap1)
    stats2 = capture_golden_snapshot(conn, cfg, snap2)

    assert stats1["open_jobs_count"] > 0
    assert stats1["top_300_count"] > 0
    assert stats1["open_jobs_sha256"] == stats2["open_jobs_sha256"]

    passed, report, diffs = compare_golden_snapshots(snap1, snap2, conn)
    assert passed is True
    assert "ZERO DIFFERENCES" in report
