"""
Redis job state management and S3 artifact staging for the DrawDiff API.

Redis schema (hash at key  job:{job_id}):
  status         JobState string value
  stage          current pipeline stage name (PROCESSING only)
  progress_pct   0-100 integer string (PROCESSING only)
  error          error message (FAILED only)
  summary_json   ChangeSummary JSON blob (COMPLETE only)
  created_at     ISO-8601 UTC timestamp
  completed_at   ISO-8601 UTC timestamp (COMPLETE / FAILED only)

Keys expire after config.artifact_ttl_hours so Redis self-cleans.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
import redis as redis_lib

from config import config
from api.models import ChangeSummary, JobState, JobStatus


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(config.redis_url, decode_responses=True)


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _presigned_url(job_id: str, kind: str) -> str:
    """Generate a time-limited S3 GET URL for a completed artifact."""
    return _presigned_url_for_key(config.s3_artifact_key(job_id, kind))


def _presigned_url_for_key(key: str) -> str:
    """Generate a time-limited S3 GET URL for an arbitrary S3 key."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        region_name=config.aws_region,
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.s3_bucket, "Key": key},
        ExpiresIn=config.artifact_ttl_hours * 3600,
    )


# ---------------------------------------------------------------------------
# Job lifecycle — called by API routes and pipeline worker
# ---------------------------------------------------------------------------

def new_job_id() -> str:
    return str(uuid.uuid4())


def create_job(job_id: str) -> None:
    r = _redis()
    r.hset(_job_key(job_id), mapping={
        "status": JobState.QUEUED,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    r.expire(_job_key(job_id), config.artifact_ttl_hours * 3600)


def stage_upload(job_id: str, kind: str, data: bytes) -> None:
    """Upload a raw PDF/image to S3 so the RQ worker can retrieve it by job_id."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        region_name=config.aws_region,
    )
    s3.put_object(
        Bucket=config.s3_bucket,
        Key=config.s3_artifact_key(job_id, kind),
        Body=data,
        ServerSideEncryption="AES256",
    )


def update_job_stage(job_id: str, stage: str, progress_pct: int) -> None:
    """Called by the pipeline worker as each stage completes."""
    _redis().hset(_job_key(job_id), mapping={
        "status": JobState.PROCESSING,
        "stage": stage,
        "progress_pct": str(progress_pct),
    })


def complete_job(job_id: str, summary: ChangeSummary) -> None:
    """Called by the pipeline worker on success."""
    _redis().hset(_job_key(job_id), mapping={
        "status": JobState.COMPLETE,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "summary_json": summary.model_dump_json(),
        "progress_pct": "100",
    })


def fail_job(job_id: str, error: str) -> None:
    """Called by the pipeline worker on unrecoverable error."""
    _redis().hset(_job_key(job_id), mapping={
        "status": JobState.FAILED,
        "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Status reader — called by GET /compare/{job_id}
# ---------------------------------------------------------------------------

def get_job_status(job_id: str) -> Optional[JobStatus]:
    """
    Read job state from Redis and return a populated JobStatus.
    Returns None if job_id is not found (caller should respond 404).

    Implement this function (~10 lines). Guidance:

    1. Call _redis().hgetall(_job_key(job_id)) — returns {} if key is missing.
    2. Build a JobStatus(job_id=job_id, status=JobState(data["status"])).
    3. Branch on state:
       - PROCESSING → set .stage and .progress_pct (cast to int).
       - COMPLETE   → set .report_url and .changeset_url via _presigned_url(),
                      deserialize .summary via ChangeSummary.model_validate_json().
       - FAILED     → set .error from data.
    4. Return the JobStatus. Return None if data is empty.

    Trade-off to consider: _presigned_url() makes a live AWS call on every poll.
    For MVP with low traffic this is fine; if poll rate is high, cache the URL
    in the Redis hash on first generation instead of regenerating each time.
    """
    job = _redis().hgetall(_job_key(job_id))
    if not job:
        return None

    state = JobState(job["status"])
    status = JobStatus(job_id=job_id, status=state)

    if state == JobState.PROCESSING:
        status.stage = job.get("stage")
        status.progress_pct = int(job["progress_pct"]) if "progress_pct" in job else None
    elif state == JobState.COMPLETE:
        status.report_url = _presigned_url(job_id, "report")
        status.changeset_url = _presigned_url(job_id, "changeset")
        if "summary_json" in job:
            status.summary = ChangeSummary.model_validate_json(job["summary_json"])
    elif state == JobState.FAILED:
        status.error = job.get("error")

    return status
