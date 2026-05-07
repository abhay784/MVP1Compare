"""DrawDiff local CLI orchestrator.

Run the full comparison pipeline against two local PDFs without Redis,
FastAPI, or S3. Artifacts land under <out_dir>/<job_id>/.

Usage:
    python cli.py compare ORIGINAL.pdf REVISED.pdf \\
        [--part-number P] [--notes N] [--out-dir DIR] \\
        [--job-id ID] [--force] [--verbose]

Exit codes:
    0  success
    1  pipeline error (LLM failure, parser crash, etc.)
    2  bad CLI args / missing input / oversize / missing API key
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
import uuid
from pathlib import Path

from config import config


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _validate_pdf(path: Path, label: str) -> int:
    if not path.exists():
        _err(f"{label} PDF not found: {path}")
        sys.exit(2)
    if not path.is_file():
        _err(f"{label} path is not a file: {path}")
        sys.exit(2)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > config.max_file_size_mb:
        _err(f"{label} PDF is {size_mb:.1f} MB, exceeds limit {config.max_file_size_mb} MB")
        sys.exit(2)
    return path.stat().st_size


_SW_SUFFIXES = {".sldprt", ".sldasm", ".slddrw"}


def _validate_solidworks(path: Path, label: str) -> None:
    # Existence / size are enforced by the Windows-side service; we only
    # check the suffix so the user gets fast feedback on obvious typos.
    if path.suffix.lower() not in _SW_SUFFIXES:
        _err(f"{label} file does not have a SOLIDWORKS extension "
             f"(.SLDPRT/.SLDASM/.SLDDRW): {path}")
        sys.exit(2)


def _cmd_compare(args: argparse.Namespace) -> int:
    source = getattr(args, "source", "pdf")

    if source == "pdf":
        if not config.anthropic_api_key:
            _err("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example) and retry.")
            return 2
    elif source == "solidworks":
        if not config.solidworks_service_url:
            _err("SOLIDWORKS_SERVICE_URL is not set. Add it to .env to point at the "
                 "Windows-side SWCompare service.")
            return 2

    original_path = Path(args.original).expanduser().resolve()
    revised_path  = Path(args.revised).expanduser().resolve()

    if source == "pdf":
        _validate_pdf(original_path, "original")
        _validate_pdf(revised_path,  "revised")
    else:
        _validate_solidworks(original_path, "original")
        _validate_solidworks(revised_path,  "revised")

    job_id  = args.job_id or str(uuid.uuid4())
    out_dir = Path(args.out_dir).expanduser().resolve()
    job_dir = out_dir / job_id

    if job_dir.exists() and any(job_dir.iterdir()) and not args.force:
        _err(f"job dir {job_dir} already exists and is non-empty. Pass --force to overwrite.")
        return 2

    job_dir.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    log = logging.getLogger("drawdiff.cli")
    log.info("Job %s [source=%s] — original=%s revised=%s",
             job_id, source, original_path.name, revised_path.name)
    log.info("Output → %s", job_dir)

    started = time.time()
    try:
        if source == "pdf":
            # Imported here so logging.basicConfig above wins over any handler set
            # at module import time inside the pipeline modules.
            from pipeline.local_runner import run_comparison_local
            result = run_comparison_local(
                job_id=job_id,
                original_pdf=original_path.read_bytes(),
                revised_pdf=revised_path.read_bytes(),
                part_number=args.part_number,
                notes=args.notes,
                job_dir=job_dir,
            )
        else:
            from pipeline.solidworks_runner import run_comparison_solidworks
            result = run_comparison_solidworks(
                job_id=job_id,
                original_path=original_path,
                revised_path=revised_path,
                part_number=args.part_number,
                notes=args.notes,
                job_dir=job_dir,
            )
    except Exception as exc:
        elapsed = time.time() - started
        _err(f"pipeline failed after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        if args.verbose:
            traceback.print_exc()
        else:
            tb_path = job_dir / "error.log"
            tb_path.write_text(traceback.format_exc())
            print(f"  traceback: {tb_path}", file=sys.stderr)
        return 1

    elapsed = time.time() - started
    summary = result["summary"]

    print(f"\n✓ Comparison complete in {elapsed:.1f}s")
    print(f"  Report:    {result['report_path']}")
    print(f"  Changeset: {result['changeset_path']}")
    if result["highlighted_pages"]["original"]:
        print(f"  Highlighted (original):")
        for p in result["highlighted_pages"]["original"]:
            print(f"    {p}")
    if result["highlighted_pages"]["revised"]:
        print(f"  Highlighted (revised):")
        for p in result["highlighted_pages"]["revised"]:
            print(f"    {p}")
    print(
        f"  Summary: critical={summary.critical} significant={summary.significant} "
        f"minor={summary.minor} uncertain={summary.uncertain} "
        f"views={summary.total_views_compared}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drawdiff",
        description="Run the DrawDiff comparison pipeline locally (no Redis/S3/API).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    cmp = sub.add_parser("compare", help="Compare two PDFs and produce a report.")
    cmp.add_argument("original", help="Path to the original PDF.")
    cmp.add_argument("revised",  help="Path to the revised PDF.")
    cmp.add_argument("--part-number", default="UNKNOWN",
                     help="Part number for the report header (default: UNKNOWN).")
    cmp.add_argument("--notes", default="",
                     help="Free-text notes shown in the report (default: empty).")
    cmp.add_argument("--out-dir", default="./out",
                     help="Directory under which <job_id>/ artifacts are written (default: ./out).")
    cmp.add_argument("--job-id", default=None,
                     help="Override the generated job_id (default: random uuid4).")
    cmp.add_argument("--force", action="store_true",
                     help="Overwrite an existing non-empty job directory.")
    cmp.add_argument("--verbose", action="store_true",
                     help="Enable DEBUG-level logging and print tracebacks to stderr.")
    cmp.add_argument("--source", choices=["pdf", "solidworks"], default="pdf",
                     help="Input source: 'pdf' (vision pipeline, default) or 'solidworks' "
                          "(geometry deltas via SWCompare service over HTTP).")
    cmp.set_defaults(func=_cmd_compare)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
