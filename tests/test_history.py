from datetime import date, datetime, timezone

from utils.history import list_report_history, read_report_snapshot, save_report_snapshot


def save_sample(tmp_path, source_digest="a" * 64, cutoff=date(2026, 8, 18)):
    return save_report_snapshot(
        b"%PDF-1.4\nsynthetic report\n%%EOF",
        source_digest=source_digest,
        cutoff=cutoff,
        instrument_count=12,
        total_amount=7_160_000,
        rule_version="test-rule-v1",
        directory=tmp_path,
        created_at=datetime(2026, 8, 18, 15, 30, tzinfo=timezone.utc),
    )


def test_report_history_persists_and_can_be_downloaded(tmp_path):
    saved, created = save_sample(tmp_path)

    assert created is True
    assert saved.pdf_path.exists()
    assert read_report_snapshot(saved).startswith(b"%PDF")

    entries = list_report_history(tmp_path)
    assert len(entries) == 1
    assert entries[0].instrument_count == 12
    assert entries[0].total_amount == 7_160_000
    assert entries[0].source_id == "a" * 12


def test_same_source_cutoff_and_rule_does_not_duplicate_history(tmp_path):
    first, first_created = save_sample(tmp_path)
    second, second_created = save_sample(tmp_path)

    assert first_created is True
    assert second_created is False
    assert first.report_id == second.report_id
    assert len(list_report_history(tmp_path)) == 1


def test_updated_source_creates_a_new_history_version(tmp_path):
    save_sample(tmp_path, source_digest="a" * 64)
    save_sample(tmp_path, source_digest="b" * 64)

    assert len(list_report_history(tmp_path)) == 2
