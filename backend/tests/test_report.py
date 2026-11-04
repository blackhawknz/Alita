"""Structured Report parsing from free-form worker output."""

from alita.executors.report import REPORT_KINDS, Report


def test_report_kinds_are_stable():
    assert REPORT_KINDS == ("progress", "waiting", "ready", "blocked", "failed", "dropped")


def test_to_dict_round_trips_fields():
    report = Report(kind="waiting", summary="need a choice", evidence=["a.log"], question="A or B?")
    assert report.to_dict() == {
        "kind": "waiting",
        "summary": "need a choice",
        "evidence": ["a.log"],
        "question": "A or B?",
    }


def test_parse_fenced_report_block():
    raw = 'All done.\n```report\n{"kind": "ready", "summary": "ok", "evidence": ["x.py:3"]}\n```'
    report = Report.parse(raw)
    assert report.kind == "ready"
    assert report.summary == "ok"
    assert report.evidence == ["x.py:3"]
    assert report.question is None


def test_parse_fenced_block_can_carry_a_question():
    raw = '```report\n{"kind": "waiting", "summary": "pick", "question": "A or B?"}\n```'
    report = Report.parse(raw)
    assert report.kind == "waiting"
    assert report.question == "A or B?"


def test_parse_whole_message_json():
    report = Report.parse('{"kind": "blocked", "summary": "no access", "evidence": "log"}')
    assert report.kind == "blocked"
    assert report.summary == "no access"
    assert report.evidence == ["log"]


def test_parse_report_line():
    report = Report.parse("REPORT: progress\nHalfway through the migration.")
    assert report.kind == "progress"
    assert report.summary == "Halfway through the migration."


def test_parse_falls_back_to_ready():
    report = Report.parse("Just some prose, no structured block.")
    assert report.kind == "ready"
    assert report.summary == "Just some prose, no structured block."


def test_parse_malformed_fenced_block_falls_back_to_ready():
    raw = '```report\n{not valid json}\n```'
    report = Report.parse(raw)
    assert report.kind == "ready"
    assert report.summary == raw


def test_parse_unknown_kind_falls_back_to_ready():
    report = Report.parse('{"kind": "banana", "summary": "odd"}')
    assert report.kind == "ready"
    assert report.summary == "odd"


def test_parse_empty_message_is_ready():
    report = Report.parse("")
    assert report.kind == "ready"
    assert report.summary == ""


def test_from_dict_tolerates_odd_shapes():
    report = Report.from_dict({"kind": "ready", "summary": 42, "evidence": 7, "question": "  "})
    assert report.summary == "42"
    assert report.evidence == ["7"]
    assert report.question is None
