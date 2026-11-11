"""
The contract every parser must follow.

A parser gets a Document (already checked by the gate) and the file path,
and returns a list of Elements. That's all. It does NOT clean text,
score quality, save files or decide status: other stages do that.
"""
from abc import ABC, abstractmethod
from pathlib import Path

from ingestion.models import Document, Element


class BaseParser(ABC):
    name: str = "base"
    file_types: tuple[str, ...] = ()     # e.g. ("pdf",) or ("xlsx", "csv")

    @abstractmethod
    def parse(self, doc: Document, path: Path) -> list[Element]:
        """Read the file and return its content as Elements (in reading order)."""
