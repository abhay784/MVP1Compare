"""HTTP client for the SOLIDWORKS Windows service (`swcompare`).

Single function `fetch_solidworks_changeset` — POSTs two file paths to the
service's /compare endpoint and returns the parsed response. The service is
expected to be reachable at `config.solidworks_service_url`.

Errors:
    SolidWorksUnavailable — service unreachable / connection error
    SolidWorksError       — service returned a non-2xx response
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from config import config

log = logging.getLogger(__name__)


class SolidWorksUnavailable(RuntimeError):
    """Raised when the SOLIDWORKS service cannot be reached."""


class SolidWorksError(RuntimeError):
    """Raised when the SOLIDWORKS service returns an error response."""


def fetch_solidworks_changeset(
    old_path: str,
    new_path: str,
    export_drawing_pdf: bool | None = None,
) -> dict[str, Any]:
    """Call the SOLIDWORKS service and return its parsed JSON response.

    Args:
        old_path: Path to Rev A file, as the service can resolve it.
        new_path: Path to Rev B file.
        export_drawing_pdf: If True, request base64-encoded PDF exports for
            .SLDDRW inputs. Defaults to `config.solidworks_export_drawings`.

    Returns: dict with at minimum:
        {
          "source": "solidworks",
          "old_file": str,
          "new_file": str,
          "file_kind": "part" | "assembly" | "drawing",
          "changes": [...],
          "exported_pdfs": {"old_b64": str, "new_b64": str} | None,
          "warnings": [str, ...]
        }
    """
    if not config.solidworks_service_url:
        raise SolidWorksUnavailable(
            "SOLIDWORKS_SERVICE_URL is not configured. Set it in .env to enable "
            "the SOLIDWORKS source path."
        )

    if export_drawing_pdf is None:
        export_drawing_pdf = config.solidworks_export_drawings

    payload = {
        "old_path": old_path,
        "new_path": new_path,
        "export_drawing_pdf": bool(export_drawing_pdf),
    }
    url = config.solidworks_service_url.rstrip("/") + "/compare"
    timeout = httpx.Timeout(config.solidworks_request_timeout_s, connect=5.0)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError) as exc:
            last_exc = exc
            log.warning("SOLIDWORKS service request failed (attempt %d): %s", attempt, exc)
            continue

        if resp.status_code >= 500 and attempt == 1:
            log.warning("SOLIDWORKS service returned %d, retrying once", resp.status_code)
            continue

        if resp.status_code >= 400:
            raise SolidWorksError(
                f"SOLIDWORKS service returned {resp.status_code}: {resp.text[:500]}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise SolidWorksError(f"Invalid JSON from SOLIDWORKS service: {exc}") from exc

    raise SolidWorksUnavailable(f"Could not reach SOLIDWORKS service at {url}: {last_exc}")
