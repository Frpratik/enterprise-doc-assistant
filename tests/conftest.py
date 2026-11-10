"""Shared test setup: make sure sample files exist, and use a temp data folder per test."""
import subprocess
import sys
from pathlib import Path

import pytest

from ingestion.config import settings

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"


@pytest.fixture(scope="session")
def samples() -> Path:
    if not (SAMPLES / "good").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_data.py")], check=True)
    return SAMPLES


@pytest.fixture
def temp_data(tmp_path, monkeypatch):
    """Point all pipeline folders at a temp folder so tests never touch real data."""
    for name in ("inbox_dir", "originals_dir", "output_dir", "failed_dir"):
        monkeypatch.setattr(settings, name, tmp_path / name.replace("_dir", ""))
    monkeypatch.setattr(settings, "manifest_path", tmp_path / "manifest.json")
    monkeypatch.setattr(settings, "file_ready_wait_sec", 0.01)
    settings.ensure_dirs()
    return tmp_path
