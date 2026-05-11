from __future__ import annotations
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field


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


ChangeColumnRole = Literal[
    "severity",
    "field",
    "orig_value",
    "revised_value",
    "zone",
    "confidence",
    "description",
    "other",
]


class ChangeColumn(BaseModel):
    header: str
    role: ChangeColumnRole = "other"


class ChangeTableSpec(BaseModel):
    columns: list[ChangeColumn] = Field(default_factory=list)
    has_zone_column: bool = False


_DEFAULT_SEVERITY_LABELS = {
    "CRITICAL":  "Critical",
    "MAJOR":     "Significant",
    "MINOR":     "Minor",
    "UNCERTAIN": "Review",
}


class Blueprint(BaseModel):
    """Faithful template skeleton extracted from a user-supplied change-order PDF.

    `html_shell` is a complete HTML document mimicking the template's visual
    style, with `[[TOKEN]]` placeholders where dynamic content goes. The
    renderer substitutes those tokens with HTML fragments built from the
    comparison results, so the final report fills the template practically
    1:1 rather than imposing DrawDiff's own layout.
    """
    html_shell: str
    change_table: ChangeTableSpec = Field(default_factory=ChangeTableSpec)
    severity_labels: dict[str, str] = Field(default_factory=lambda: dict(_DEFAULT_SEVERITY_LABELS))


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
