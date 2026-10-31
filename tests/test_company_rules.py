"""Company grade rules: parsing, matching, gate effects, and the two explicit file edits."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.matching import company_rules as cr
from jobhunt.matching.gate import EXCLUDED, MAYBE, TARGET, GateJob, RelevanceGate, rules_hash

TODAY = date(2026, 10, 21)
BARCLAYS = cr.parse_company_rules(cr.TEMPLATE)


@pytest.fixture(scope="module")
def gate(config: Config) -> RelevanceGate:
    return RelevanceGate(config.relevance, (), BARCLAYS)


def _judge(gate: RelevanceGate, title: str, description: str = "Build Python services.", company: str = "Barclays"):  # noqa: ANN202
    return gate.evaluate(GateJob(job_id=1, title=title, company_name=company, description=description), TODAY)


def test_template_ships_the_barclays_rule_exactly() -> None:
    (rule,) = BARCLAYS
    assert rule.company == "Barclays"
    assert rule.allow_level_codes == ("BA4",)
    assert rule.allow_title_patterns == ("Analyst", "Technology Developer")
    assert rule.deny_title_patterns == ("Assistant Vice President", "Vice President", "AVP", "VP", "Director")
    assert rule.deny_level_codes == ("BA3", "BA2", "BA1")
    assert rule.note == "Only the BA-4 grade is open to me (user rule)"
    assert rule.deny_text_patterns == ()


@pytest.mark.parametrize("title", [
    "Assistant Vice President - Java Developer", "Vice President, Technology", "AVP Data Engineer", "VP Engineering",
    "Director, Software Engineering", "Associate Director - Data", "Data Analyst - Vice-President",
])
def test_deny_title_excludes_with_the_note(gate: RelevanceGate, title: str) -> None:
    decision = _judge(gate, title, "Analyst role. BA4")
    assert decision.status == EXCLUDED and decision.rule == "company_grade_rule"
    assert "Only the BA-4 grade is open to me (user rule)" in decision.reason and "Barclays" in decision.reason


@pytest.mark.parametrize("text", ["Grade: BA3", "Level BA-2 role", "this is a ba 1 position", "BA3/BA4 role"])
def test_deny_level_code_in_the_text_excludes(gate: RelevanceGate, text: str) -> None:
    decision = _judge(gate, "Software Engineer", f"Build Python services. {text}")
    assert decision.status == EXCLUDED and decision.rule == "company_grade_rule"


def test_deny_code_in_the_title_excludes(gate: RelevanceGate) -> None:
    assert _judge(gate, "Software Engineer - BA3").status == EXCLUDED


@pytest.mark.parametrize(("title", "text"), [
    ("Software Engineer - BA4", "Build Python services."),
    ("Software Engineer", "Software Engineer - BA-4 Role Overview"),
    ("Analyst, Software Engineer", "Build Python services."),
    ("Technology Developer", "Build Python services."),
    ("Technology Developer - Python", "Python"),
])
def test_allow_match_leaves_the_job_unaffected(gate: RelevanceGate, title: str, text: str) -> None:
    assert _judge(gate, title, text).status == TARGET


def test_role_words_at_the_top_of_the_posting_count_as_allow(gate: RelevanceGate) -> None:
    assert _judge(gate, "Full Stack Engineer", "Technology Developer role. Python services.").status == TARGET
    far = "x " * 400 + "Analyst"
    assert _judge(gate, "Full Stack Engineer", far).status == MAYBE


def test_no_allow_match_is_at_best_maybe_never_hidden(gate: RelevanceGate) -> None:
    decision = _judge(gate, "Full Stack Engineer")
    assert decision.status == MAYBE and decision.rule == "company_grade_unverified"
    assert "Only the BA-4 grade" in decision.reason
    assert _judge(gate, "AI Engineer").status == MAYBE


def test_other_companies_are_untouched(gate: RelevanceGate) -> None:
    assert _judge(gate, "Software Engineer", company="Acme Ltd").status == TARGET
    assert _judge(gate, "Software Engineer", company="Barclay Hardware").status == TARGET   # not the same company


@pytest.mark.parametrize("name", ["Barclays", "BARCLAYS", "Barclays Bank PLC", "barclays capital"])
def test_company_name_matching_is_case_insensitive_and_ignores_suffixes(gate: RelevanceGate, name: str) -> None:
    assert _judge(gate, "Vice President Engineer", company=name).status == EXCLUDED


def test_aliases_and_merged_entries() -> None:
    rules = cr.parse_company_rules(
        "- company: Acme\n  aliases: [Acme Corp Global]\n  deny_title_patterns: [Director]\n  note: a\n"
        "- company: ACME LTD\n  deny_title_patterns: [Principal]\n  allow_title_patterns: [Analyst]\n  note: b\n"
        "- company: Other\n  deny_level_codes: [G7]\n")
    assert len(rules) == 2
    acme = rules[0]
    assert acme.deny_title_patterns == ("Director", "Principal") and acme.note == "a; b"
    assert acme.entry_indexes == (0, 1)
    assert cr.company_matches(acme, "Acme Corp Global Inc")


def test_deny_wins_over_allow(config: Config) -> None:
    rules = cr.parse_company_rules("- company: Acme\n  allow_title_patterns: [Analyst]\n  deny_title_patterns: [Director]\n")
    gate = RelevanceGate(config.relevance, (), rules)
    assert _judge(gate, "Analyst Director Software", company="Acme").status == EXCLUDED


def test_deny_text_patterns_are_optional_and_work() -> None:
    rules = cr.parse_company_rules(
        "- company: Barclays\n  deny_text_patterns: [Assistant Vice President Expectations]\n")
    gate_rules = cr.CompanyRuleSet(rules)
    verdict = gate_rules.judge("Barclays", "Full Stack Engineer", "x Assistant Vice President Expectations - To advise")
    assert verdict.outcome == "deny"
    assert gate_rules.judge("Barclays", "Full Stack Engineer", "Analyst Expectations").outcome == "allowed"


def test_a_company_with_only_deny_rules_never_goes_to_maybe(config: Config) -> None:
    rules = cr.parse_company_rules("- company: Acme\n  deny_title_patterns: [Director]\n")
    assert _judge(RelevanceGate(config.relevance, (), rules), "Software Engineer", company="Acme").status == TARGET


def test_deny_never_overwrites_an_earlier_exclusion_reason(gate: RelevanceGate) -> None:
    decision = _judge(gate, "Senior Vice President Engineer")
    assert decision.rule in {"seniority_title", "company_grade_rule"} and decision.status == EXCLUDED


def test_malformed_files_are_ignored_not_fatal(tmp_path: Path) -> None:
    assert cr.parse_company_rules("not: [valid") == []
    assert cr.parse_company_rules("- just a string\n- company: ''\n- 12\n") == []
    assert cr.parse_company_rules("") == []
    assert cr.load_company_rules(tmp_path / "missing.yaml") == []
    odd = cr.parse_company_rules("- company: X\n  deny_title_patterns: 5\n  allow_level_codes: {a: b}\n")
    assert odd[0].deny_title_patterns == () and odd[0].allow_level_codes == ()


def test_level_codes_normalise_separators() -> None:
    rx = cr._code_regex("BA4")
    assert rx is not None
    for text in ("BA4", "BA-4", "BA 4", "ba_4", "(BA4)"):
        assert rx.search(text), text
    for text in ("BA40", "XBA4", "BA4X", "BAA4"):
        assert not rx.search(text), text
    assert cr._code_regex("not a code") is None


def test_template_is_created_once_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "data" / "profile" / "company_rules.yaml"
    assert cr.ensure_template(path) is True and "Barclays" in path.read_text(encoding="utf-8")
    path.write_text("# mine\n", encoding="utf-8")
    assert cr.ensure_template(path) is False and path.read_text(encoding="utf-8") == "# mine\n"


def test_loading_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(cr.TEMPLATE, encoding="utf-8")
    before = path.stat().st_mtime_ns
    cr.load_company_rules(path)
    assert path.read_text(encoding="utf-8") == cr.TEMPLATE and path.stat().st_mtime_ns == before


def test_signature_and_rules_hash_change_with_the_rules(config: Config) -> None:
    other = cr.parse_company_rules("- company: Barclays\n  deny_title_patterns: [Director]\n")
    assert cr.signature(BARCLAYS) != cr.signature(other) and cr.signature(()) == ""
    assert rules_hash(config.relevance, TODAY, "|" + cr.signature(BARCLAYS)) != rules_hash(
        config.relevance, TODAY, "|" + cr.signature(other))


@pytest.mark.parametrize(("title", "word"), [
    ("Vice President, Technology", "Vice President"),
    ("Assistant Vice President - Java", "Assistant Vice President"),
    ("AVP Data Engineer", "AVP"),
    ("Software Engineer, VP", "VP"),
    ("Director of Engineering", "Director"),
    ("Associate Director Data", "Associate Director"),
    ("Principal Engineer", "Principal"),
    ("Data Analyst", None),
    ("Software Engineer", None),
])
def test_level_word_found_in_the_title(title: str, word: str | None) -> None:
    assert cr.level_word_in_title(title) == word


def test_append_and_remove_edit_only_their_own_lines(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(cr.TEMPLATE, encoding="utf-8")
    entry = cr.render_deny_entry("Citi", "Vice President", today=TODAY)
    assert entry == ('\n- company: "Citi"\n  deny_title_patterns: ["Vice President"]\n'
                     '  note: "Added from Not applying (ineligible_level) on 2026-10-21"\n')
    cr.append_deny_entry(path, entry)
    rules = cr.load_company_rules(path)
    assert [rule.company for rule in rules] == ["Barclays", "Citi"]
    assert rules[1].deny_title_patterns == ("Vice President",)
    assert cr.already_denied(rules, "Citi Bank", "vice-president")
    assert not cr.already_denied(rules, "Citi", "Director")
    assert cr.remove_entry(path, 1) is True
    text = path.read_text(encoding="utf-8")
    assert "Citi" not in text and text.startswith("# Company grade rules") and "Barclays" in text
    assert [rule.company for rule in cr.load_company_rules(path)] == ["Barclays"]
    assert cr.remove_entry(path, 5) is False
    assert cr.remove_entry(path, 0) is True
    assert cr.load_company_rules(path) == [] and "# Company grade rules" in path.read_text(encoding="utf-8")


def test_remove_keeps_neighbours_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("# header\n- company: A\n  deny_title_patterns: [X]\n\n# about B\n- company: B\n  note: n\n"
                    "- company: C\n  deny_level_codes: [G7]\n", encoding="utf-8")
    assert cr.remove_entry(path, 1)
    assert path.read_text(encoding="utf-8") == ("# header\n- company: A\n  deny_title_patterns: [X]\n\n# about B\n"
                                                "- company: C\n  deny_level_codes: [G7]\n")


def test_quotes_in_names_cannot_break_the_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    cr.append_deny_entry(path, cr.render_deny_entry('Ev"il\nCo', 'Vice "Pres"', today=TODAY))
    rules = cr.load_company_rules(path)
    assert any(rule.deny_title_patterns for rule in rules)


def test_the_avp_vp_text_markers_exclude_with_the_users_note(config: Config) -> None:
    rules = cr.parse_company_rules(
        "- company: Barclays\n  allow_level_codes: [BA4]\n  allow_title_patterns: [Analyst, Technology Developer]\n"
        "  deny_title_patterns: [AVP, Vice President]\n  deny_level_codes: [BA3]\n"
        "  deny_text_patterns: [Assistant Vice President Expectations, Vice President Expectations]\n"
        "  note: \"AVP/VP grade markers in the job text (user rule: only BA4 is open)\"\n")
    gate = RelevanceGate(config.relevance, (), rules)
    avp = _judge(gate, "Full Stack Engineer", "Purpose of the role. Assistant Vice President Expectations - To advise and influence")
    vp = _judge(gate, "AI Engineer", "Vice President Expectations - To contribute or set strategy")
    for decision in (avp, vp):
        assert decision.status == EXCLUDED and decision.rule == "company_grade_rule"
        assert "AVP/VP grade markers in the job text" in decision.reason
    analyst = _judge(gate, "Full Stack Engineer", "Analyst Expectations - Requires in-depth technical knowledge")
    assert analyst.status == TARGET            # the Analyst section is an allowed role word: unaffected
    assert _judge(gate, "Software Engineer - BA4", "Build services.").status == TARGET
    assert _judge(gate, "Software Engineer", "Assistant Vice President Expectations", company="Acme").status == TARGET
