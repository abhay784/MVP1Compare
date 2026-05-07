# DrawDiff Pipeline: View Understanding Failure Analysis

## Test Documents

All findings in this document are reproduced against the same pair of PDFs at the repo root:

- `DA1840189_NC (1).pdf` — original release (REV NC)
- `DA1840189_A (1).pdf` — revised release (REV A)

The illustrative examples below are taken from **page 2** of each PDF (mounting bracket, multi-view orthographic + sections A-A / B-B / C-C). Page 2 is the canonical exhibit for the view-duplication problem because a single physical edit shows up in four cropped views.

### The page-2 diff in plain English

Comparing NC → A on page 2, there is essentially **one engineering change** (a fillet redefinition on the inner pocket corners) plus a small note edit. The pipeline currently inflates this into many findings:

| Where it appears on the drawing | NC says | A says | What it actually is |
|---------------------------------|---------|--------|----------------------|
| Main top view, inner pocket corners | `16X R.030` | `8X R.030` + new `8X R.063` | Inner corners split into two fillet sizes |
| SECTION B-B, pocket bottom corners | `R .030 ALL AROUND` | `4X R .030` | Same fillets, viewed in cross-section |
| SECTION A-A, right face corners | (no callout) | `(4X R.030)` reference | Same fillets, viewed in cross-section |
| SECTION C-C, bottom corners | (no callout) | `4X R.030` | Same fillets, viewed in cross-section |
| Helicoil note | `TAP FOR #4-40 HELICOIL` | `TAP FOR #4-40 HELICAL INSERTS` | Wording-only change, same feature |

A human reads this as **two changes** (fillet redefinition + note rewording). The current pipeline reports it as 5–7, because each crop is compared in isolation and the aggregator only dedupes on string similarity.

---

## What We Observed

Two runs against the same pair of PDFs produced different results:

| Run | Critical | Significant | Minor | Uncertain |
|-----|----------|-------------|-------|-----------|
| report.pdf | 5 | 15 | 3 | 0 |
| draftp2.pdf | 2 | 19 | 4 | 0 |

Same documents. Different answers. This is not a data problem — it is an architectural one.

---

## Root Cause 1: The Pipeline Has No Concept of Drawing Structure

Engineering drawings are not a flat collection of pictures. They are a **hierarchical document**:

```
Page 2
├── Main top-down view (parent)
│   ├── Section cut A-A → generates SECTION A-A (right)
│   ├── Section cut B-B → generates SECTION B-B (left)
│   └── Section cut C-C → generates SECTION C-C (bottom)
```

SECTION B-B exists only to explain the pocket depth of the main view at cut line B-B. It is not an independent drawing. The same R.030 pocket corners that appear in the main top view also appear in SECTION B-B and SECTION A-A because they are the **same physical feature seen from different angles**.

The current pipeline treats all four rectangles as equal, independent views. It compares each one separately and reports findings for each. The aggregator deduplicates some of this, but misses cases where the LLM describes the same change in different words across views.

**Effect:** A single engineering change (e.g., R.030 → 8X R.030) gets reported 2-4 times with slightly different wording, severity, and confidence — once per view that contains it.

---

## Root Cause 2: The Segmentor Finds Rectangles, Not Views

The segmentor ([pipeline/segmentor.py](pipeline/segmentor.py)) has two detection paths:

- **Vector PDF path:** Finds rectangles in pymupdf draw commands with area ≥ 5% of page and aspect ratio between 0.2–5.0
- **Raster PDF path:** OpenCV morphological line detection to infer borders

Neither path knows what a section cut is, what a parent view is, or which rectangle corresponds to which annotation label. It finds borders and crops them. The label extraction is a best-effort regex over nearby text.

**Effect:** Views whose text labels run together ("SIDE SIDE" from two adjacent side views) get a single cropped region with a malformed label. The matcher ([pipeline/matcher.py](pipeline/matcher.py)) uses rapidfuzz string similarity, so "SIDE SIDE" fails to match "SIDE" in the revised drawing. This produces spurious REMOVED + ADDED findings for views that actually matched, inflating change counts.

---

## Root Cause 3: LLM Non-Determinism Causes Run-to-Run Variance

The comparator ([pipeline/comparator.py](pipeline/comparator.py)) sends each view crop to Claude with no temperature override. Claude's default temperature is not zero, so:

- The same crop sent twice produces slightly different descriptions
- Slightly different descriptions → different confidence scores
- Different confidence scores → different severity assignments (above/below 0.65 threshold)
- Different severities → different dedup decisions in the aggregator

This is why two runs on identical input produce a count difference of 3 Critical and 4 Significant changes.

---

## Root Cause 4: The Comparator Has No Part-Level Context

When the comparator receives the SECTION B-B crop, its prompt contains:
- The two crop images (original and revised)
- The view label ("SIDE")
- The part number and notes

It does not know:
- This is a cross-section taken at cut line B-B through the pocket region
- The pockets visible here correspond to the 2X .529 rectangular pockets in the parent top view
- The overall part is a mounting bracket with hinge bosses and threaded inserts
- The A-B-C annotation in the parent view defines datum structure, not separate features

Without this context, the LLM reasons about each crop as a standalone image. It hedges more, produces vaguer descriptions, and sometimes misclassifies severity because it cannot connect what it sees to the part's function.

---

## Guiding Section: Teaching the Pipeline How Views Relate

The four root causes above all share a single underlying cause: **the pipeline has no model of how the regions on a page relate to each other**. Feature extraction sees rectangles; image processing sees pixels; the LLM sees an isolated crop. Nothing in the system carries the fact that *SECTION B-B is a cross-section of the parent top view at cut line B-B*. Without that fact, deduplication, severity, and matching are all guessing.

This section defines the guiding structure that must be injected before per-view comparison. It is not optional polish — it is the missing primitive that makes every downstream stage work correctly.

### What the pipeline must know about each page, before comparing

For every page, the pipeline must produce and carry forward a **page graph** with three node types:

1. **Parent views** — orthographic projections that stand alone (top, front, side, isometric). They define the part's geometry directly.
2. **Derived views** — sections, details, auxiliary views. They exist *only* to clarify a region of a parent view. Each carries an explicit edge back to its parent and a `cut_id` (e.g., `A-A`) that ties it to a marker in the parent.
3. **Annotation regions** — title block, revision history, notes, BOM. Compared as text, not as geometry.

The page graph is what tells the comparator "the R.030 you are looking at in SECTION B-B is the same physical fillet you already saw in the top view at cut line B-B." Without it, the comparator has no choice but to report the change in both places.

### How the page graph is built (deterministic + LLM, in that order)

The pipeline should construct the graph in two passes, with the deterministic pass running first so the LLM pass has less room to hallucinate:

**Pass A — deterministic (vector PDF only):**
- Locate section-cut markers by looking for paired arrow glyphs with matching letter labels (`A`→`A`, `B`→`B`, `C`→`C`) in the same parent rectangle. pymupdf exposes these as draw commands + nearby text spans.
- Locate underlined view labels (`SECTION A-A`, `DETAIL D`, `SECTION B-B`) — they are visually distinct (underlined, centered under the crop) and parseable by font + position.
- Pair each `SECTION X-X` label with the cut markers that share its `X-X` id. The rectangle containing the label becomes the derived view; the rectangle containing the markers becomes its parent.
- Mark every remaining border-bounded rectangle as a candidate parent view.

**Pass B — LLM, only for what Pass A could not resolve:**
- Send a downscaled full-page render plus the partial graph from Pass A.
- Ask the LLM to (i) confirm each parent/derived assignment, (ii) fill in `describes` (one-line semantic role of each derived view), (iii) produce a one-sentence `part_summary`, and (iv) flag any view Pass A missed (detail views, auxiliary views, exploded assemblies — these have no standard cut-marker geometry).
- The LLM never overrides Pass A's geometric assignments; it only annotates them. This bounds the failure mode to "LLM wrote a bad description" rather than "LLM rearranged the topology."

Raster-only PDFs skip Pass A and go straight to Pass B with the full page image; accept the higher error rate as the cost of having no vector geometry to lean on.

### How the page graph reshapes the rest of the pipeline

| Stage | Without graph (today) | With graph (target) |
|-------|------------------------|---------------------|
| `segmentor` | Returns a flat list of cropped rectangles with regex-extracted labels | Returns the page graph; crops are nodes, not a list |
| `matcher` | Fuzzy string match on labels across NC vs. A | Match by `(view_role, cut_id, parent_role)` tuple first; string match only as tie-breaker. `SIDE SIDE` no longer fails to match `SIDE`. |
| `comparator` | Prompt has crop + label + part number | Prompt also has `part_summary`, `view_role`, `parent_label`, `cut_id`, `describes`. Page-2 example: when comparing SECTION B-B crops, the prompt explicitly says "this is a cross-section through the inner pocket corners of the main top view at cut B-B; the fillets you see here are the same physical feature as the `8X R.030` callout in the parent." |
| `aggregator` | String-similarity dedup across all findings | **Structural dedup first**: any finding on a derived view whose `(parent_label, cut_id, feature_descriptor)` collides with a finding on its parent is collapsed into the parent finding with a `confirmed_by: [SECTION B-B, SECTION A-A, SECTION C-C]` annotation. The page-2 R.030 change becomes one finding citing four views, not four findings. |

### What "good" looks like on the page-2 test case

After the guiding section is wired in, running NC vs. A on page 2 should produce roughly:

- **1 Significant** finding: "Inner pocket corner fillets redefined: `16X R.030` split into `8X R.030` (outer) + `8X R.063` (inner). Confirmed in SECTION A-A, SECTION B-B, SECTION C-C."
- **1 Minor** finding: "Helicoil insert note rewording (`HELICAL INSERTS` ↔ `HELICOIL`). No geometric impact."
- **0 spurious REMOVED/ADDED** findings from label corruption.
- **Run-to-run variance: zero** (with `temperature=0` from the quick fix).

If the pipeline still emits 4+ findings for the fillet change, the guiding section is not actually being threaded through — most likely the comparator prompt is not receiving the `parent_label` / `cut_id` fields, or the aggregator's structural dedup pass is running after string dedup instead of before.

---

## The Fix: A Drawing Intelligence Pre-Pass

Before per-view comparison, send each full rendered page to the LLM once to produce a structured understanding of the drawing:

```json
{
  "part_summary": "rectangular mounting bracket with two hinge bosses, pocketed slots, threaded helicoil inserts",
  "drawing_type": "multi-view orthographic with sections",
  "parent_views": [
    {
      "label": "main_top",
      "bbox": [x0, y0, x1, y1],
      "section_cuts": ["A-A", "B-B", "C-C"],
      "datums": ["A", "B", "C"]
    }
  ],
  "derived_views": [
    {
      "label": "SECTION A-A",
      "bbox": [...],
      "parent_label": "main_top",
      "cut_id": "A-A",
      "describes": "through-hole pattern, right face"
    },
    {
      "label": "SECTION B-B",
      "bbox": [...],
      "parent_label": "main_top",
      "cut_id": "B-B",
      "describes": "pocket depth profile, left cross-section"
    },
    {
      "label": "SECTION C-C",
      "bbox": [...],
      "parent_label": "main_top",
      "cut_id": "C-C",
      "describes": "slot width and depth, bottom cross-section"
    }
  ]
}
```

This JSON then flows through the rest of the pipeline as context.

---

## What the Pre-Pass Fixes

| Problem | Current | With Pre-Pass |
|---------|---------|---------------|
| Same change reported N times | Dedup misses cross-view duplicates | Section views grouped under parent; change reported once at parent level |
| "SIDE SIDE" label corruption | Fails matcher; reports REMOVED + ADDED | Pre-pass reads label from drawing structure, not border proximity |
| LLM sees crops without context | Hedges, produces vague severity | Each crop prompt includes part summary + which section it is + what feature it describes |
| Run-to-run count variance (LLM temp) | 3-4 count swing per run | Fixable independently: set temperature=0 in the comparator call |
| Added/removed views over-counted | Every unmatched label = ADDED/REMOVED | Pre-pass confirms which views are structurally equivalent despite label mismatch |

---

## Tradeoffs of the Pre-Pass Approach

### Cost
- 1 additional LLM call per page (full-page image, not a crop)
- Adds ~2–5 seconds per page at claude-sonnet-4-6 speed
- Increases token usage: full-page images at 300 DPI are large; may need to downscale to max_image_dimension before the pre-pass

### Risk
- The pre-pass can itself be wrong on non-standard drawing layouts (detail views, auxiliary views, exploded assemblies)
- Requires a fallback: if the pre-pass returns incomplete or malformed JSON, fall back to the current rectangle-detection path
- Adds a new prompt to maintain ([prompts/](prompts/)) with its own failure modes

### Benefit
- Eliminates the structural duplication problem entirely (highest-impact fix)
- Makes the per-view comparator significantly more accurate because it has context
- Makes the matcher label-independent for section views (match by structure, not by string)
- Part summary enables the comparator to reason about functional impact (e.g., "this fillet change is on a stress-concentration region" vs "this is a cosmetic corner")

---

## Quick Fix Available Now (No Architecture Change)

Set `temperature=0` in the comparator LLM call. This alone eliminates run-to-run count variance and makes results reproducible. It does not fix the structural duplication or context problems, but it makes the output deterministic while the larger fix is planned.

Location: [pipeline/comparator.py](pipeline/comparator.py) — the `anthropic.messages.create()` call. Add `temperature=0`.

---

## Recommended Implementation Order

1. **temperature=0** in comparator — immediate, 1-line fix, eliminates variance
2. **Drawing intelligence pre-pass** — new pipeline stage before segmentor; produces structured drawing metadata JSON
3. **Context injection** — pass pre-pass metadata into comparator prompt per view
4. **Structure-aware deduplication** — replace string-match dedup with section-grouping: changes in SECTION B-B that duplicate a finding in the parent top view are suppressed, not just string-matched
5. **Structure-aware matcher** — match section views by their cut-id relationship, not label fuzzy match
