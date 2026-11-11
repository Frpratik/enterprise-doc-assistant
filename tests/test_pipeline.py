"""Stage 7 tests: the whole pipeline end to end, saving, isolation and the run command."""
import json

from ingestion import pipeline
from ingestion.config import settings
from ingestion.manifest import Manifest
from ingestion.models import Document, Status
from ingestion.pipeline import process_file
from ingestion.run import main

EXPECTED = {
    "leave_policy.pdf": "done", "scanned_code_of_conduct.pdf": "done", "mixed_quarterly_report.pdf": "done",
    "long_handbook.pdf": "done", "employee_handbook.docx": "done", "onboarding_deck.pptx": "done",
    "salary_structure.xlsx": "done", "holidays_2026.csv": "done", "notice_board_photo.png": "done",
    "it_policy.txt": "done", "remote_work.md": "done", "hr_faq.html": "done",
    "canteen_notice_scan.png": "needs_review", "policy_with_blank_page.pdf": "needs_review",
    "faded_receipt_scan.png": "failed",
}


def test_every_sample_gets_the_right_status(samples, temp_data):
    m = Manifest(settings.manifest_path)
    for name, status in EXPECTED.items():
        doc = process_file(samples / "good" / name, m)
        assert doc.status.value == status, f"{name}: {doc.status_reason}"


def test_output_json_is_saved_and_complete(samples, temp_data):
    doc = process_file(samples / "good" / "leave_policy.pdf", Manifest(settings.manifest_path))
    saved = Document.model_validate_json((settings.output_dir / f"{doc.doc_id}_v1.json").read_text())
    assert saved.status == Status.DONE and saved.parser == "pdf_parser" and saved.parser_version
    assert saved.doc_label == "text-heavy" and saved.metadata.language == "en"
    assert saved.elements and saved.duration_ms is not None


def test_manifest_has_final_status(samples, temp_data):
    m = Manifest(settings.manifest_path)
    doc = process_file(samples / "good" / "canteen_notice_scan.png", m)
    record = Manifest(settings.manifest_path).records[f"{doc.doc_id}:v1"]
    assert record["status"] == "needs_review" and "OCR" in record["status_reason"]


def test_one_crashing_parser_does_not_stop_the_run(samples, temp_data, monkeypatch):
    real = pipeline.get_parser

    class Boom:
        name = "boom"
        def parse(self, doc, path):
            raise ValueError("simulated parser bug")

    monkeypatch.setattr(pipeline, "get_parser", lambda ft: Boom() if ft == "docx" else real(ft))
    m = Manifest(settings.manifest_path)
    bad = process_file(samples / "good" / "employee_handbook.docx", m)
    good = process_file(samples / "good" / "it_policy.txt", m)
    assert bad.status == Status.FAILED and "simulated parser bug" in bad.status_reason
    assert (settings.failed_dir / f"{bad.doc_id}_employee_handbook.docx.reason.txt").exists()
    assert good.status == Status.DONE


def test_logs_are_json_lines(samples, temp_data, monkeypatch):
    monkeypatch.setattr(settings, "log_path", temp_data / "logs" / "ingestion.jsonl")
    process_file(samples / "good" / "it_policy.txt", Manifest(settings.manifest_path))
    lines = (temp_data / "logs" / "ingestion.jsonl").read_text().strip().splitlines()
    event = json.loads(lines[-1])
    assert event["event"] == "document_processed" and event["status"] == "done" and "duration_ms" in event


def test_run_command(samples, temp_data, capsys):
    code = main(["--file", str(samples / "good" / "it_policy.txt"), str(samples / "bad" / "setup_tool.exe")])
    out = capsys.readouterr().out
    assert "[OK] DONE" in out and "[X] FAILED" in out and "TOTAL: 1 done, 1 failed" in out
    assert code == 1                         # non-zero exit when something failed (useful in scripts/CI)


def test_run_folder_summary(samples, temp_data, capsys):
    main(["--folder", str(samples / "bad"), "--summary"])
    out = capsys.readouterr().out
    assert "SUMMARY (6 files)" in out and "password-protected" in out and "skipped_duplicate" not in out
