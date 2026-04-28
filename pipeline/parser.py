"""PDF parsing and rendering — Stage 1 of the DrawDiff pipeline.

Produces a ParsedPage from raw PDF bytes: a rendered numpy image at
config.render_dpi plus text blocks with bounding boxes for the text layer.
For raster PDFs (very low text coverage), text_blocks will be sparse and
Stage 2 (OCR) should be invoked by the caller before segmentation.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import fitz  # pymupdf
import numpy as np
from PIL import Image

from config import config


@dataclass
class TextBlock:
    text: str
    bbox_x0: float
    bbox_y0: float
    bbox_x1: float
    bbox_y1: float


@dataclass
class ParsedPage:
    image: np.ndarray          # RGB numpy array at render_dpi
    text_blocks: list[TextBlock]
    raw_text: str
    page_width_pt: float       # original PDF points (1 pt = 1/72 inch)
    page_height_pt: float
    is_raster: bool            # True when text coverage is too low for regex extraction


def parse_pdf(pdf_bytes: bytes, page_index: int = 0) -> ParsedPage:
    """Render a single PDF page and extract its text layer.

    Args:
        pdf_bytes: Raw PDF file content.
        page_index: Zero-based page number (default 0 for single-sheet drawings).

    Returns:
        ParsedPage with rendered image, text blocks, and a raster flag.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if page_index >= len(doc):
        raise ValueError(f"PDF has {len(doc)} page(s); page_index {page_index} is out of range")

    page = doc[page_index]

    # Render at config.render_dpi (default 300)
    scale = config.render_dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    image_array = np.array(img)

    # Extract text blocks with word-level bounding boxes.
    # get_text("words") returns (x0, y0, x1, y1, word, block_no, line_no, word_no)
    word_tuples = page.get_text("words")
    text_blocks: list[TextBlock] = []
    for wt in word_tuples:
        x0, y0, x1, y1, word = wt[0], wt[1], wt[2], wt[3], wt[4]
        text_blocks.append(TextBlock(
            text=word,
            bbox_x0=x0,
            bbox_y0=y0,
            bbox_x1=x1,
            bbox_y1=y1,
        ))

    raw_text = page.get_text("text")

    # Capture page dimensions before closing the document — the page object is
    # invalidated by doc.close() and accessing page.rect afterwards raises.
    page_width_pt = page.rect.width
    page_height_pt = page.rect.height

    # A vector PDF always has at least a few text blocks from pymupdf's text layer.
    # A scanned/raster PDF produces zero text blocks. Character-density ratios
    # are unreliable for sparse drawings, so we use block presence as the signal.
    is_raster = len(text_blocks) == 0

    doc.close()

    return ParsedPage(
        image=image_array,
        text_blocks=text_blocks,
        raw_text=raw_text,
        page_width_pt=page_width_pt,
        page_height_pt=page_height_pt,
        is_raster=is_raster,
    )
