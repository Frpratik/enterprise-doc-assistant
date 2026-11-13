"""The shapes of what goes in and out of the API (FastAPI turns these into validation and /docs)."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000, description="The employee's question, in plain words.")

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        return v


class CitationOut(BaseModel):
    n: int = Field(..., description="The [n] marker used in the answer text")
    file_name: str
    heading_path: str
    where: str = Field("", description="e.g. 'page 2', 'slide 3', \"sheet 'Salary Bands'\"")
    chunk_id: str


class AskResponse(BaseModel):
    status: Literal["answered", "refused_gate", "refused_llm", "uncited", "llm_unavailable"]
    text: str = Field(..., description="Show this to the user. When status is not 'answered' it is a plain 'not found' / 'busy' message.")
    citations: list[CitationOut] = []
    model: str | None = None
    fell_back: bool = False
    latency_ms: int = 0
    prompt_version: str
    request_id: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    embedding_model: bool
    reranker: bool
    llm_key_configured: bool
