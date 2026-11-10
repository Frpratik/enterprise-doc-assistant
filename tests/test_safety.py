"""Stage 2 tests: the gate accepts good files and rejects bad ones with a clear reason."""
import shutil

import pytest

from ingestion.config import settings
from ingestion.intake import admit, is_temp_file
from ingestion.manifest import Manifest
from ingestion.models import Status

GOOD = ["leave_policy.pdf", "scanned_code_of_conduct.pdf", "mixed_quarterly_report.pdf",
        "employee_handbook.docx", "onboarding_deck.pptx", "salary_structure.xlsx",
        "holidays_2026.csv", "notice_board_photo.png", "it_policy.txt", "remote_work.md", "hr_faq.html"]

BAD = {
    "salary_details.pdf": "content is really 'exe'",
    "corrupted_report.pdf": "corrupted",
    "password_protected.pdf": "password-protected",
    "empty_notes.txt": "empty",
    "setup_tool.exe": "not allowed",
}


@pytest.mark.parametrize("name", GOOD)
def test_good_files_pass(samples, temp_data, name):
    doc = admit(samples / "good" / name, Manifest(settings.manifest_path))
    assert doc.status == Status.QUEUED, doc.status_reason
    assert doc.hash and doc.original_path


@pytest.mark.parametrize("name,reason", BAD.items())
def test_bad_files_fail_with_reason(samples, temp_data, name, reason):
    doc = admit(samples / "bad" / name, Manifest(settings.manifest_path))
    assert doc.status == Status.FAILED
    assert reason in doc.status_reason
    assert (settings.failed_dir / f"{doc.doc_id}_{name}.reason.txt").exists()


def test_duplicate_is_skipped(samples, temp_data):
    m = Manifest(settings.manifest_path)
    first = admit(samples / "good" / "leave_policy.pdf", m)
    dup = admit(samples / "bad" / "leave_policy_copy.pdf", m)
    assert dup.status == Status.SKIPPED_DUPLICATE
    assert dup.doc_id == first.doc_id


def test_same_name_new_content_is_new_version(samples, temp_data, tmp_path):
    m = Manifest(settings.manifest_path)
    f = tmp_path / "policy.txt"
    f.write_text("Notice period is 30 days.")
    v1 = admit(f, m)
    f.write_text("Notice period is 60 days.")       # HR updated the file
    v2 = admit(f, m)
    assert (v1.doc_id, v1.version) == (v2.doc_id, 1) and v2.version == 2


def test_inbox_mode_moves_file_out(samples, temp_data):
    f = settings.inbox_dir / "it_policy.txt"
    shutil.copy(samples / "good" / "it_policy.txt", f)
    doc = admit(f, Manifest(settings.manifest_path), move=True)
    assert doc.status == Status.QUEUED and not f.exists()


def test_user_file_is_never_touched(samples, temp_data):
    f = samples / "good" / "it_policy.txt"
    admit(f, Manifest(settings.manifest_path), move=False)
    assert f.exists()


def test_manifest_survives_restart(samples, temp_data):
    admit(samples / "good" / "leave_policy.pdf", Manifest(settings.manifest_path))
    fresh = Manifest(settings.manifest_path)              # like restarting the app
    dup = admit(samples / "bad" / "leave_policy_copy.pdf", fresh)
    assert dup.status == Status.SKIPPED_DUPLICATE


@pytest.mark.parametrize("name,expected", [
    ("~$handbook.docx", True), (".hidden.pdf", True), ("report.pdf.crdownload", True),
    ("upload.tmp", True), ("leave_policy.pdf", False),
])
def test_temp_files(name, expected):
    assert is_temp_file(name) is expected
