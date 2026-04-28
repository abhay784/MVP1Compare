from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class JobState(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class CompareAccepted(BaseModel):
    job_id: str
    status_url: str
    estimated_seconds: int = 120


class ChangeSummary(BaseModel):
    critical: int = 0
    significant: int = 0
    minor: int = 0
    uncertain: int = 0
    total_views_compared: int = 0


class JobStatus(BaseModel):
    job_id: str
    status: JobState
    report_url: Optional[str] = None
    changeset_url: Optional[str] = None
    summary: Optional[ChangeSummary] = None
    error: Optional[str] = None
    # populated while status == PROCESSING
    stage: Optional[str] = None
    progress_pct: Optional[int] = None
