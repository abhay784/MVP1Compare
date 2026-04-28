"""DrawDiff global config. Values pinned by the Step 1 memory anchor (drawdiff_config.md)."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Model strategy (§7.3)
    base_model: str = "claude-sonnet-4-6"
    escalation_model: str = "claude-opus-4-6"

    # Thresholds
    confidence_threshold: float = 0.65       # below this → severity rewritten to UNCERTAIN
    view_match_threshold: float = 0.80       # rapidfuzz ratio floor in matcher

    # Rendering
    render_dpi: int = 300
    max_image_dimension: int = 4096          # downscale guard before LLM call

    # LLM budget
    max_tokens_per_view: int = 2000

    # Artifact lifecycle
    artifact_ttl_hours: int = 24

    # Upload guard
    max_file_size_mb: int = 50

    # API auth (Bearer token; leave empty to disable auth in dev)
    api_key: str = os.getenv("API_KEY", "")

    # Secrets / infra (resolved from env)
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    s3_bucket: str = os.getenv("S3_BUCKET", "")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    def s3_artifact_key(self, job_id: str, kind: str) -> str:
        ext = {"report": "pdf", "changeset": "json", "original": "pdf", "revised": "pdf"}[kind]
        return f"{kind}s/{job_id}.{ext}"
        

config = Config()
