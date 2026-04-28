# DrawDiff — Product Requirements Document
**AI-Powered Mechanical Engineering Drawing Comparison — Minimum Viable Product**

| Field | Value |
|---|---|
| Document Status | DRAFT — Internal Review |
| Version | 1.0 |
| Product | DrawDiff — Mechanical Drawing Comparison MVP |
| Primary Market | Mechanical engineering teams with CAD revision workflows |
| Secondary Market (Target) | HVAC submittal review (post-MVP validation) |
| Last Updated | April 2026 |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals & Success Metrics](#3-goals--success-metrics)
4. [Users & Use Cases](#4-users--use-cases)
5. [Functional Requirements](#5-functional-requirements)
6. [Change Classification Definitions](#6-change-classification-definitions)
7. [Technical Architecture](#7-technical-architecture)
8. [Non-Functional Requirements](#8-non-functional-requirements)
9. [Out of Scope for MVP](#9-out-of-scope-for-mvp)
10. [MVP Build Plan](#10-mvp-build-plan)
11. [Future Roadmap](#11-future-roadmap)
12. [Open Questions](#12-open-questions)

---

## 1. Executive Summary

Engineers waste hours manually comparing two revisions of the same mechanical drawing — searching for changed dimensions, updated GD&T callouts, modified notes, and new title-block entries across dense, multi-view sheets. A missed change becomes a production error, a warranty claim, or a safety event.

**DrawDiff automates this review.** Upload two PDF revisions of any mechanical engineering drawing. The system:
- Segments every view on each sheet
- Extracts all text and dimensions with spatial coordinates
- Matches corresponding views between revisions
- Calls a multimodal LLM to identify every meaningful change
- Returns a structured PDF report with side-by-side annotated images, a severity-ranked change table, and confidence flags on uncertain detections

> **MVP Scope:** v1.0 targets mechanical engineering drawings in native vector PDF or raster PDF format. The preprocessing, view segmentation, and LLM comparison pipeline is intentionally domain-agnostic and will extend to HVAC submittals and structural drawings in future iterations.

---

## 2. Problem Statement

### 2.1 The Manual Review Burden

A single mechanical drawing revision review requires an engineer to:

- Open both PDFs side-by-side and manually scan every view
- Cross-reference the revision block description against actual drawing changes
- Verify every dimension, GD&T callout, note, and title-block field
- Document findings in a separate markup or written summary

For a complex multi-view drawing (8–15 views, 50–200 dimensions), this takes **45 minutes to 3 hours per review**. Engineering teams perform hundreds of reviews per year. Human reviewers miss 15–25% of changes in dense technical documents when reviewing under time pressure.

### 2.2 The Stakes

- A missed dimension change causes the wrong part to be manufactured — typically discovered only at first article inspection or assembly
- A missed material specification change triggers a re-order cycle and potentially a design nonconformance record
- A missed GD&T callout change propagates to inspection plans and quality records, creating downstream audit risk
- Re-work cost of a single missed critical change: **$2,000 (simple re-machine) to $50,000+ (scrapped casting or forging)**

### 2.3 Market Context

The mechanical engineering drawing comparison problem is universal across aerospace, automotive, medical devices, industrial equipment, and consumer products. Every company with a CAD revision workflow has this pain. No purpose-built AI solution exists today — the only options are manual review or expensive PLM integrations that compare 3D models, not 2D drawings.

---

## 3. Goals & Success Metrics

### 3.1 MVP Goals

| ID | Goal | Target | Measurement |
|---|---|---|---|
| G1 | Detect the majority of real changes in a drawing revision | >85% recall | Tested against 20 labeled drawing pairs |
| G2 | Keep false positive rate acceptable | <20% FPR | Expert review of output reports |
| G3 | End-to-end processing time under 3 minutes per sheet | <3 min/sheet | Automated timing logs |
| G4 | Working API endpoint | Accepts two PDFs, returns report | Integration test |
| G5 | Report readable and actionable by a non-technical manager | User satisfaction | 5 structured feedback sessions |

### 3.2 What Success Is NOT

- 100% recall with zero false positives — impossible for a multimodal LLM baseline; engineer review of flagged items is always expected
- Replacing the engineer — the MVP is a review **assistant**, not an autonomous reviewer
- Real-time comparison — batch processing with a 2–3 minute turnaround is acceptable
- 3D model comparison — scope is 2D engineering drawings only

---

## 4. Users & Use Cases

### 4.1 Primary Users

| User | Role | Primary Pain | MVP Value |
|---|---|---|---|
| Design Engineer | Creates and revises drawings; responds to ECRs | Spending 1–2 hrs verifying own revisions match intent | Instant confirmation before release |
| QA / Checker Engineer | Reviews released drawings before manufacturing | Dense review of 10–30 drawings/week | Automated first pass; focus on flagged items only |
| Supplier / Contract Manufacturer | Receives revised drawings; must re-plan machining | Finding what changed in a drawing they received | Immediate diff without re-reading entire drawing |
| Engineering Manager | Approves ECRs and drawing releases | Signing off without full technical depth | Summary report for informed approval |

### 4.2 Core Use Cases

#### UC-01 — Engineer-to-Engineer Revision Verification
A design engineer completes a revision (Rev B → Rev C) based on an ECR. Before releasing, they upload both revisions to DrawDiff. The report confirms every change in the ECR is present in the drawing, and flags two additional unintended changes — a dimension accidentally moved and a note deleted. The engineer corrects both before release.

#### UC-02 — Incoming Drawing Review at Supplier
A contract machining shop receives a revised customer drawing by email. Instead of manually comparing to their floor copy, the shop manager uploads both to DrawDiff. The report shows three changed dimensions and a new surface finish callout. The shop updates their work order and inspection plan accordingly.

#### UC-03 — QA Pre-Release Check
A QA checker reviews 12 drawings for a product release. Using DrawDiff, they process all 12 pairs in under 30 minutes and receive severity-ranked reports for each. They focus manual attention on CRITICAL-flagged changes and spot-check a sample of MINOR changes. Total review time drops from 8 hours to 2 hours.

---

## 5. Functional Requirements

### 5.1 Input Handling

| ID | Requirement | Notes |
|---|---|---|
| F-01 | Accept two PDF files as input | Via REST API upload or CLI; max 50MB per file |
| F-02 | Support native vector PDFs from CAD exports | AutoCAD, SolidWorks, CATIA, NX, Creo PDF output |
| F-03 | Support scanned raster PDFs | Min 150 DPI acceptable; 300 DPI recommended |
| F-04 | Support multi-page drawing packages | Process each page independently; match by sheet number from title block |
| F-05 | Accept TIFF and PNG in addition to PDF | Single-sheet raster images |

### 5.2 Preprocessing & View Segmentation

| ID | Requirement | Notes |
|---|---|---|
| F-06 | Render all PDFs at 300 DPI for processing | Balance between text readability and memory/cost |
| F-07 | Extract title block from bottom-right quadrant of each sheet | Parse: part number, revision level, material, general tolerances, date |
| F-08 | Extract revision block from top-right quadrant | Parse: revision letter, date, description, approval |
| F-09 | Segment drawing into individual views | Use explicit rectangular borders for vector PDFs; line detection for rasters |
| F-10 | Label each view with its title text | e.g. SECTION A-A, DETAIL C (2:1), FRONT VIEW |
| F-11 | Extract all dimension values with page coordinates | Format support: X.XXX, XX.XX, fractions, ±tolerances, ∅ and R prefixes |
| F-12 | Extract all text blocks with bounding box positions | Preserve font size for label vs. dimension classification |

### 5.3 View Matching

| ID | Requirement | Notes |
|---|---|---|
| F-13 | Match views between original and revised drawing by title text | Primary: exact match. Fallback: fuzzy match (≥80% similarity via rapidfuzz) |
| F-14 | Flag views present in one drawing but absent in the other | Report as ADDITION or DELETION at view level |
| F-15 | Match title block and revision block as special named views | Always compared regardless of other view matching |
| F-16 | Log match confidence per view pair | Low-confidence matches flagged for human verification in report |

### 5.4 LLM Comparison Engine

| ID | Requirement | Notes |
|---|---|---|
| F-17 | Compare each matched view pair using a multimodal LLM | Default: `claude-sonnet-4-6`. Escalate to `claude-opus-4-6` for CRITICAL severity pass |
| F-18 | Pass both image crops AND extracted text to the LLM | Images provide spatial context; text provides exact dimension values |
| F-19 | Use a structured mechanical-engineering-specific system prompt | Hierarchy: title block → revision block → dimensions → GD&T → notes → BOM → surface finish |
| F-20 | Return structured JSON for every change detected | Fields: `type`, `severity`, `description`, `old_value`, `new_value`, `location`, `confidence` |
| F-21 | Classify every change by type | `GEOMETRY`, `ANNOTATION`, `ADDITION`, `DELETION`, `SPECIFICATION` |
| F-22 | Classify every change by severity | `CRITICAL`, `SIGNIFICANT`, `MINOR` (definitions in §6) |
| F-23 | Include a confidence score (0.0–1.0) per change | Changes below 0.65 flagged as `UNCERTAIN` in report |
| F-24 | Require the LLM to list unchanged elements it confirmed | Reduces hallucination rate by forcing active attention to both drawings |
| F-25 | Do not report revision clouds from prior revisions as current changes | Explicit instruction in system prompt |

#### LLM JSON Output Schema

```json
{
  "changes": [
    {
      "type": "GEOMETRY | ANNOTATION | ADDITION | DELETION | SPECIFICATION",
      "severity": "CRITICAL | SIGNIFICANT | MINOR",
      "description": "plain English description of the change",
      "old_value": "what it was (if applicable)",
      "new_value": "what it is now (if applicable)",
      "location": "where on the drawing (e.g. top-right, grid B-4, SECTION A-A)",
      "confidence": 0.0
    }
  ],
  "summary": "one sentence overall summary of this view",
  "unchanged_confirmed": [
    "list of elements explicitly verified as unchanged"
  ]
}
```

#### LLM System Prompt — Review Hierarchy

The following hierarchy must be embedded in the system prompt and followed in order:

```
1. TITLE BLOCK CHANGES (always check first)
   - Part number change → CRITICAL
   - Revision level change → expected, note it
   - Material change → CRITICAL
   - General tolerance change → CRITICAL
   - Surface finish change → SIGNIFICANT

2. REVISION BLOCK
   - What does the revision description say changed?
   - Use as a checklist to verify changes are present in drawing

3. DIMENSIONS
   - Any changed numerical value → CRITICAL if critical feature, SIGNIFICANT otherwise
   - Focus: bore diameters, thread specs, hole-to-hole distances, envelope dimensions
   - Format: X.XXX or X.XX±0.XX

4. GD&T FEATURE CONTROL FRAMES
   - Changes to geometric tolerances → CRITICAL
   - Datum reference changes → CRITICAL

5. NOTES BLOCK (numbered notes, usually upper left)
   - Added/removed notes → SIGNIFICANT
   - Changed note text → depends on content

6. SECTION AND DETAIL VIEWS
   - New/removed section cuts → SIGNIFICANT
   - Changed geometry in section → CRITICAL

7. SURFACE FINISH CALLOUTS (√ symbols)
   - Changed Ra values → SIGNIFICANT

8. BILL OF MATERIALS
   - Added/removed/changed parts → SIGNIFICANT

DO NOT FLAG:
- Revision clouds from previous revisions
- Leader line path changes when callout value unchanged
- Drawing sheet formatting changes (border, logo)
- Minor text alignment shifts that don't change content
```

### 5.5 Output & Report Generation

| ID | Requirement | Notes |
|---|---|---|
| F-26 | Generate a PDF comparison report | Human-readable; suitable for engineering sign-off workflows |
| F-27 | Report includes executive summary table: counts by severity | CRITICAL / SIGNIFICANT / MINOR / UNCERTAIN totals |
| F-28 | Report includes per-view sections with side-by-side image crops | Original on left, revised on right |
| F-29 | Annotate images with bounding box highlights per change | Color coded: red = CRITICAL, amber = SIGNIFICANT, blue = MINOR |
| F-30 | Report includes a ranked change table (CRITICAL first) | Columns: severity, type, view, description, old value, new value, confidence |
| F-31 | Return JSON changeset via API in addition to PDF report | Enables downstream integration with PLM or ERP systems |
| F-32 | Report includes a section of UNCERTAIN changes requiring human review | Separated from confirmed changes; not counted in totals |

### 5.6 API & Integration

| ID | Requirement | Notes |
|---|---|---|
| F-33 | Expose REST endpoint: `POST /compare` | Accepts two multipart file uploads + optional metadata |
| F-34 | Return processing status via `GET /compare/{job_id}` | Async processing for large or multi-page PDFs |
| F-35 | Return PDF report and JSON changeset as downloadable artifacts | URLs valid for 24 hours after job completion |
| F-36 | Support API key authentication | Bearer token for MVP; OAuth2 in v2 |
| F-37 | Provide a minimal web upload UI | Drag-and-drop two PDFs; download report; no login required for MVP beta |

#### API Contract

```
POST /compare
Content-Type: multipart/form-data

Fields:
  original    (file, required)   — original revision PDF
  revised     (file, required)   — revised revision PDF
  part_number (string, optional) — for report metadata
  notes       (string, optional) — context for the LLM

Response 202 Accepted:
{
  "job_id": "uuid",
  "status_url": "/compare/{job_id}",
  "estimated_seconds": 120
}

GET /compare/{job_id}
Response 200 (complete):
{
  "status": "complete",
  "report_url": "https://...",
  "changeset_url": "https://...",
  "summary": {
    "critical": 2,
    "significant": 5,
    "minor": 3,
    "uncertain": 1,
    "total_views_compared": 7
  }
}
```

---

## 6. Change Classification Definitions

> These definitions are embedded verbatim in the LLM system prompt and in the report legend.

### 6.1 Change Types

| Type | Definition |
|---|---|
| `GEOMETRY` | A physical dimension, feature location, shape, or size changed. Includes all numerical dimension values, hole sizes, radii, and envelope dimensions. |
| `ANNOTATION` | A note, label, callout text, or leader line target changed. The annotation refers to an existing feature; the feature itself did not change. |
| `ADDITION` | An element (view, feature, callout, note, BOM row) present in the revised drawing that does not appear in the original. |
| `DELETION` | An element present in the original drawing that does not appear in the revised drawing. |
| `SPECIFICATION` | A material, surface finish, GD&T tolerance, or performance requirement changed. Does not include changes to physical geometry. |

### 6.2 Severity Levels

| Severity | Criteria | Examples |
|---|---|---|
| `CRITICAL` 🔴 | Affects structural integrity, safety, interchangeability, or regulatory compliance. Manufacturing the part from the revised drawing would produce a non-conforming or unsafe result. | Bore diameter changed; material grade changed; GD&T datum reference changed; general tolerance tightened; thread specification changed |
| `SIGNIFICANT` 🟡 | Affects manufacturing scope, cost, or lead time. No safety risk but requires re-planning of machining, inspection, or procurement. | Non-critical dimension changed; surface finish Ra value changed; BOM quantity changed; section view added; new note added |
| `MINOR` 🔵 | Clarity, formatting, or documentation change. No manufacturing impact. | Note reworded without technical content change; view label renamed; leader line path changed |
| `UNCERTAIN` ⚪ | Confidence score below 0.65. LLM detected a possible change but could not confirm. Requires human review. | Ambiguous linework change; text partially obscured in raster scan; overlapping dimensions in dense view |

---

## 7. Technical Architecture

### 7.1 Pipeline Overview

> **Design Principle:** The MVP pipeline is intentionally simple and sequential. Every stage produces an inspectable intermediate artifact (extracted text, segmented view images, JSON changes). This makes debugging fast and establishes ground truth for future model training.

```
Input: PDF A (original) + PDF B (revised)
              │
              ▼
┌─────────────────────────────┐
│  Stage 1: PDF Parser        │  pymupdf — render 300 DPI, extract text+positions
│  Stage 2: OCR (if raster)   │  AWS Textract — bounding boxes per word
│  Stage 3: View Segmentor    │  OpenCV — detect borders, crop, label views
│  Stage 4: Dimension Extract │  Regex on text layer — all dim values + coords
└─────────────────────────────┘
              │
       [view_crops + text + dims]
              │
              ▼
┌─────────────────────────────┐
│  Stage 5: View Matcher      │  rapidfuzz — match views by title text
└─────────────────────────────┘
              │
       [matched view pairs]
              │
              ▼
┌─────────────────────────────┐
│  Stage 6: LLM Comparator    │  Claude API — image + text → JSON changeset
│  Stage 7: Change Aggregator │  Deduplicate, sort by severity
│  Stage 8: Annotator         │  PIL — draw colored bounding boxes on images
│  Stage 9: Report Generator  │  Jinja2 → WeasyPrint → PDF
│  Stage 10: API Layer        │  FastAPI + Redis — async job management
└─────────────────────────────┘
              │
              ▼
Output: PDF Report + JSON Changeset
```

### 7.2 File & Module Structure

```
drawdiff/
├── api/
│   ├── main.py                  # FastAPI app, routes
│   ├── models.py                # Pydantic request/response schemas
│   └── jobs.py                  # Redis queue management
├── pipeline/
│   ├── parser.py                # PDF rendering + text extraction (pymupdf)
│   ├── ocr.py                   # AWS Textract wrapper (raster PDFs)
│   ├── segmentor.py             # View border detection + cropping (OpenCV)
│   ├── extractor.py             # Dimension + text extraction with coordinates
│   ├── matcher.py               # View matching (rapidfuzz)
│   ├── comparator.py            # LLM API calls + prompt management
│   ├── aggregator.py            # Deduplication + severity sorting
│   └── annotator.py             # Bounding box rendering (PIL)
├── report/
│   ├── generator.py             # Jinja2 → HTML → WeasyPrint PDF
│   └── templates/
│       └── report.html.j2       # Report template
├── prompts/
│   └── mechanical_comparison.py # System prompt + hierarchy definitions
├── config.py                    # Settings (model names, thresholds, DPI)
└── tests/
    ├── test_parser.py
    ├── test_segmentor.py
    ├── test_matcher.py
    ├── test_comparator.py
    └── fixtures/                # Sample drawing PDF pairs for tests
```

### 7.3 LLM Model Strategy

| Scenario | Model | Rationale | Est. Cost/Sheet |
|---|---|---|---|
| Standard view comparison (<8 views, single sheet) | `claude-sonnet-4-6` | Best cost/accuracy balance for structured extraction | ~$0.08–0.15 |
| Complex multi-view sheet (>8 views) | `claude-sonnet-4-6` per view | Per-view calls keep token counts manageable | ~$0.20–0.40 |
| CRITICAL severity escalation pass | `claude-opus-4-6` | Higher accuracy for life-safety relevant changes | +~$0.15 |
| Title block comparison only | `claude-sonnet-4-6` | Structured text; vision adds little here | ~$0.02 |

### 7.4 Technology Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| PDF Processing | `pymupdf` (fitz) | 1.24+; fastest Python PDF library; handles vector + raster |
| OCR | AWS Textract | Superior on engineering drawings; returns bounding boxes per word |
| Image Processing | OpenCV + PIL/Pillow | View segmentation, line detection, annotation rendering |
| String Matching | `rapidfuzz` | View title matching; 10–100x faster than fuzzywuzzy |
| LLM API | Anthropic Claude API | `claude-sonnet-4-6` primary; `claude-opus-4-6` escalation |
| API Framework | FastAPI + Uvicorn | Async; auto-generates OpenAPI docs |
| Job Queue | Redis + RQ | Async processing for large PDFs |
| Report Generation | Jinja2 + WeasyPrint | HTML template → PDF; no headless browser needed |
| Infrastructure | AWS (ECS + S3 + Textract) | Containerized; S3 for artifact storage and 24h URLs |
| Frontend (MVP) | React + Tailwind CSS | Drag-and-drop upload + report download |

### 7.5 Key Implementation Notes for Claude Code

#### View Segmentation — Two Paths

**Path A: Native vector PDF** — use pymupdf rectangle detection
```python
import fitz

def find_view_borders_vector(page: fitz.Page) -> list[fitz.Rect]:
    """Find view bounding boxes from explicit rectangles in vector PDFs."""
    drawings = page.get_drawings()
    page_area = page.rect.width * page.rect.height
    borders = []
    for d in drawings:
        rect = d["rect"]
        rect_area = rect.width * rect.height
        # Views are large rectangles (>5% of page) with aspect ratios typical of engineering views
        if rect_area > page_area * 0.05 and 0.3 < rect.width / rect.height < 3.5:
            borders.append(rect)
    return borders
```

**Path B: Raster PDF** — use OpenCV line detection
```python
import cv2
import numpy as np

def find_view_borders_raster(img: np.ndarray) -> list[tuple]:
    """Find view bounding boxes by detecting horizontal + vertical border lines."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (80, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 80))
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    grid = cv2.add(h_lines, v_lines)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > img.shape[1] * 0.08 and h > img.shape[0] * 0.08:
            boxes.append((x, y, x + w, y + h))
    return boxes
```

#### Dimension Extraction — Regex Pattern
```python
import re

DIMENSION_PATTERN = re.compile(
    r'^[∅Rr]?'           # optional diameter/radius prefix
    r'\d+\.?\d*'          # integer or decimal
    r'(?:[/\-]\d+\.?\d*)?' # optional fraction or negative tolerance
    r'(?:\s*[±+\-]\s*\d+\.?\d*)?' # optional tolerance
    r'$'
)
```

#### Title Block Extraction — Region + Regex
```python
def extract_title_block(page: fitz.Page) -> dict:
    """Title block is always bottom-right ~30% width, ~20% height."""
    r = page.rect
    tb_rect = fitz.Rect(r.width * 0.65, r.height * 0.80, r.width, r.height)
    text = page.get_textbox(tb_rect)

    patterns = {
        "part_number": r"(?:PART\s*(?:NO|NUMBER|#)?)[:\s]+([A-Z0-9\-]+)",
        "revision":    r"(?:REV(?:ISION)?)[:\s]+([A-Z0-9]+)",
        "material":    r"(?:MATERIAL)[:\s]+(.+?)(?:\n|$)",
        "tolerance":   r"(?:TOLERANC(?:E|ES))[:\s]+(.+?)(?:\n|$)",
        "drawn_by":    r"(?:DRAWN\s*BY|DRN)[:\s]+([A-Z\s]+?)(?:\n|$)",
        "date":        r"(?:DATE)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    }

    result = {}
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        result[field] = match.group(1).strip() if match else None
    return result
```

#### LLM Call — Core Function
```python
import anthropic
import base64
import json

def compare_view_pair(
    img_original: bytes,
    img_revised: bytes,
    text_original: str,
    text_revised: str,
    view_title: str,
    system_prompt: str,
    model: str = "claude-sonnet-4-6"
) -> dict:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Compare these two versions of: **{view_title}**"},
                {"type": "text", "text": f"ORIGINAL DRAWING TEXT:\n{text_original}"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img_original).decode()
                    }
                },
                {"type": "text", "text": f"REVISED DRAWING TEXT:\n{text_revised}"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img_revised).decode()
                    }
                },
                {"type": "text", "text": "Return ONLY valid JSON matching the output schema. No prose."}
            ]
        }]
    )

    return json.loads(response.content[0].text)
```

#### Confidence Threshold & Escalation Logic
```python
CONFIDENCE_THRESHOLD = 0.65
ESCALATION_MODEL = "claude-opus-4-6"
BASE_MODEL = "claude-sonnet-4-6"

def run_comparison_with_escalation(view_pair, system_prompt) -> dict:
    # First pass with base model
    result = compare_view_pair(*view_pair, system_prompt=system_prompt, model=BASE_MODEL)

    # Escalate CRITICAL changes to opus for verification
    critical_changes = [c for c in result["changes"] if c["severity"] == "CRITICAL"]
    if critical_changes:
        escalated = compare_view_pair(
            *view_pair,
            system_prompt=system_prompt + "\n\nFocus ONLY on CRITICAL changes. Verify each one carefully.",
            model=ESCALATION_MODEL
        )
        # Merge: keep escalated CRITICAL assessments, keep base SIGNIFICANT/MINOR
        result["changes"] = [
            c for c in result["changes"] if c["severity"] != "CRITICAL"
        ] + escalated["changes"]

    # Flag uncertain
    for change in result["changes"]:
        if change["confidence"] < CONFIDENCE_THRESHOLD:
            change["severity"] = "UNCERTAIN"

    return result
```

---

## 8. Non-Functional Requirements

| ID | Category | Requirement | Target |
|---|---|---|---|
| NF-01 | Performance | Single-sheet comparison end-to-end | < 3 minutes |
| NF-02 | Performance | 10-sheet package comparison | < 20 minutes |
| NF-03 | Reliability | API uptime during business hours | > 99% |
| NF-04 | Security | Uploaded drawings encrypted at rest | AES-256, S3 SSE |
| NF-05 | Security | Drawings purged after report delivery | 24-hour retention max |
| NF-06 | Security | No drawing content sent to third parties beyond Claude API | Anthropic zero-retention API option |
| NF-07 | Scalability | Concurrent comparisons supported | 10 concurrent jobs on MVP infra |
| NF-08 | Observability | Log every stage with timing and token counts | CloudWatch / Datadog |
| NF-09 | Cost | Fully-loaded cost per single-sheet comparison | < $0.50 at MVP scale |

---

## 9. Out of Scope for MVP

> These are deliberate scope cuts — not permanently excluded features. Each will be evaluated for v1.1 or v2.0 based on MVP learnings.

- 3D CAD model comparison (STEP, IGES, SolidWorks files)
- HVAC submittal comparison against spec documents (different product logic — spec ≠ drawing)
- Structural, civil, or architectural drawing support
- PDM/PLM system integration (SolidWorks PDM, Windchill, Teamcenter, Vault)
- ERP or quality system integration (SAP, Oracle, Arena)
- User accounts, projects, and drawing version history
- Automated ECR / change order generation
- Fine-tuned model (requires labeled training data from MVP usage — see §11)
- Native CAD file ingestion (DWG, DXF, SLDDRW) — PDF only for MVP
- Batch processing via folder upload — single pair per API call for MVP
- Mobile application

---

## 10. MVP Build Plan

### 10.1 Milestones

| Week | Milestone | Deliverables | Success Gate |
|---|---|---|---|
| 1–2 | M1: Core Extraction | PDF parser, view segmentor, dimension extractor working on 5 sample drawings | View crop images are clean and correctly labeled |
| 3 | M2: View Matching | View matcher tested on 10 revision pairs; title block extractor complete | Match rate >90% on test set |
| 4–5 | M3: LLM Integration | Single view comparison working end-to-end; JSON output validated | LLM detects >80% of seeded changes on 5 test pairs |
| 6 | M4: Report Generation | PDF report with side-by-side images and annotated bounding boxes | Report readable by a non-engineer |
| 7 | M5: API Layer | FastAPI endpoint with async processing; downloadable artifacts | End-to-end API call returns report in <3 min |
| 8 | M6: Beta | Minimal web UI; 5 beta users with real drawings; feedback collected | At least 3/5 users rate report as useful or very useful |

### 10.2 Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Low-quality scanned PDFs yield poor OCR and missed dimensions | HIGH | Set minimum DPI requirements; flag low-quality inputs; fallback to LLM-only vision for unreadable text |
| LLM hallucinates changes in dense linework areas | MEDIUM | Pass extracted text alongside images; require confidence scores; flag uncertain items separately |
| View matching fails when view titles differ between revisions | MEDIUM | Fuzzy matching + position-based fallback; flag unmatched views prominently in report |
| Processing cost exceeds $0.50/sheet at scale | LOW | Monitor per-sheet token usage from day 1; route simple views to `claude-sonnet-4-6` only |
| Engineers do not trust AI-generated change reports | MEDIUM | Position as assistant; require human sign-off; show confidence scores; track false positive rate transparently |
| Customer drawings contain proprietary IP — data security concern | MEDIUM | Zero-retention Anthropic API; 24-hour deletion policy; publish SOC2 roadmap at launch |

### 10.3 Definition of Done — MVP

The MVP is shippable when:

- [ ] End-to-end comparison works on at least 20 real mechanical drawing pairs
- [ ] Recall >85% and FPR <20% verified on a held-out labeled test set of 10 pairs
- [ ] API endpoint documented and tested with Postman/pytest
- [ ] PDF report passes readability review with 3 mechanical engineers who were not involved in building it
- [ ] Processing cost confirmed below $0.50/sheet on real inputs
- [ ] Data deletion policy implemented and verified (24h purge)
- [ ] Feedback capture mechanism in place (engineer marks each change as Correct / False Positive / Missed)

---

## 11. Future Roadmap

### v1.1 — Feedback Loop & Quality (Weeks 9–14)

- **Human verification UI:** engineers mark each detected change as Correct / False Positive / Missed — generates labeled training data for future fine-tuning
- Feedback-driven prompt refinement based on first 50 real comparison sessions
- Expanded view type support: exploded assembly views, manufacturing notes blocks
- Batch comparison: upload ZIP of multiple drawing pairs

### v2.0 — HVAC Submittal Review (Months 4–8)

> This is a **new product mode**, not just a feature. The core pipeline carries over; the product logic is different.

- New mode: compare a contractor's submitted equipment cut sheet against the engineer's specification schedule
- Spec section parser: ingest Div 23 HVAC specs, extract structured equipment requirements (CFM, static pressure, EER, MCA, MOCP, sound rating, MERV)
- Attribute matching engine: extract same attributes from submittal PDFs; produce pass/fail/uncertain per requirement
- Procore Marketplace integration: submit review results directly into Procore submittal workflow
- Target ICP: large mechanical subcontractors ($100M–$1B revenue) with centralized preconstruction teams

### v2.1 — Fine-Tuned Model (Months 6–12)

> **Why not now:** Fine-tuning requires labeled training pairs (original, revised, ground-truth changeset). Those don't exist yet. The v1.1 feedback loop generates them.

- Train specialized dimension-extraction model on labeled dataset from v1.1 feedback loop
- Target failure modes identified by MVP: small dimension changes in dense strings, revision cloud false positives, GD&T frame changes
- Replace LLM-based dimension comparison with specialized model → 5–10x cost reduction on extraction step
- Keep LLM for semantic reasoning ("does this change affect interchangeability") — fine-tuned model handles pattern recognition only

---

## 12. Open Questions

| # | Question | Resolution Needed By |
|---|---|---|
| Q1 | Do we gate beta access behind login, or allow anonymous upload with a unique link? | Week 6 (before beta launch) |
| Q2 | What is the Anthropic zero-retention API pricing vs. standard? Does it fit our cost model? | Week 4 (before LLM integration) |
| Q3 | Should the report PDF be watermarked as "AI-generated — requires human verification"? | Week 6 (legal/liability review) |
| Q4 | Is AWS Textract the right OCR choice, or does a self-hosted alternative (PaddleOCR, EasyOCR) perform acceptably on engineering drawings at lower cost? | Week 2 (extraction testing) |
| Q5 | When we expand to HVAC submittals, is that a new SKU/price point or a feature of the same product? | Month 3 (roadmap planning) |
| Q6 | What is the minimum viable security posture required by target customers? SOC 2? ISO 27001? | Week 4 (sales qualification) |

---

## Appendix A — Environment Setup

```bash
# Python dependencies
pip install pymupdf rapidfuzz pillow opencv-python-headless \
            anthropic fastapi uvicorn redis rq \
            boto3 jinja2 weasyprint python-multipart

# Environment variables required
ANTHROPIC_API_KEY=sk-ant-...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
REDIS_URL=redis://localhost:6379
S3_BUCKET=drawdiff-artifacts
```

## Appendix B — Configuration

```python
# config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Models
    base_model: str = "claude-sonnet-4-6"
    escalation_model: str = "claude-opus-4-6"

    # Thresholds
    confidence_threshold: float = 0.65
    view_match_threshold: float = 0.80
    min_view_area_fraction: float = 0.05

    # Processing
    render_dpi: int = 300
    max_image_dimension: int = 4096  # tile larger images
    max_file_size_mb: int = 50

    # Storage
    artifact_ttl_hours: int = 24

    # Cost guard
    max_tokens_per_view: int = 2000

    class Config:
        env_file = ".env"

settings = Settings()
```

## Appendix C — Sample Drawing Types Supported at MVP Launch

| Drawing Type | View Types | Primary Change Targets |
|---|---|---|
| Machined part drawing | Front/top/side orthographics, sections, details | Dimensions, GD&T, surface finish, material |
| Sheet metal part drawing | Flat pattern, formed views, bend tables | Bend radii, k-factor, hole patterns, material gauge |
| Weldment drawing | Assembly views, weld symbols, BOM | Weld callouts, material, joint prep |
| Purchased part drawing | Dimensional envelope, interface features | Critical interface dimensions, port specs |
| Assembly drawing | Exploded view, BOM, interface callouts | BOM quantities, assembly notes, torque specs |

---

*DrawDiff — Product Requirements Document v1.0 — Confidential — April 2026*
