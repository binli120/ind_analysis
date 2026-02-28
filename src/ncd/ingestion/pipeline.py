# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

import io
import json
import os
import re
import subprocess
import sys

import pandas as pd
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from PIL import Image, ImageOps
from utils.log import info

# OCR tuning (overridable via env vars)
OCR_LANG = os.getenv("OCR_LANG", "eng")
OCR_PSM = os.getenv("OCR_PSM", "6")  # 6 = assume a block of text
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
PREFER_DIRECT_OCR = os.getenv("PREFER_DIRECT_OCR", "0") == "1"
OCR_THRESHOLD = os.getenv("OCR_THRESHOLD")  # int value (0-255) if set
_PAGE_LIMIT = os.getenv("PAGE_LIMIT")
PAGE_LIMIT = int(_PAGE_LIMIT) if _PAGE_LIMIT and _PAGE_LIMIT.isdigit() else None
CLEAN_NOISE = os.getenv("CLEAN_NOISE", "1") == "1"


# ==========================================
# Utility functions
# ==========================================
def clamp(val, minv, maxv):
    """Clamp a numeric value into the inclusive range [minv, maxv]."""
    return max(minv, min(val, maxv))


def convert_to_pil(raw_image):
    """Normalize image types into PIL.Image.Image."""
    if isinstance(raw_image, (bytes, bytearray)):
        return Image.open(io.BytesIO(raw_image))
    if isinstance(raw_image, Image.Image):
        return raw_image
    if hasattr(raw_image, "original"):
        orig = raw_image.original
        if isinstance(orig, (bytes, bytearray)):
            return Image.open(io.BytesIO(orig))
        if isinstance(orig, Image.Image):
            return orig
    raise TypeError("Unknown image format from pdfplumber.to_image()")


# ==========================================
# 0. Detect need for OCR → run OCRmyPDF
# ==========================================
def needs_ocr(pdf_path, pages_to_check=3, min_text_len=50, page_limit=None):
    """Check a few pages for extractable text to decide whether OCR is needed."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            check_pages = min(pages_to_check, len(pdf.pages))
            if page_limit:
                check_pages = min(check_pages, page_limit)
            for p in pdf.pages[:check_pages]:
                txt = p.extract_text() or ""
                if len(txt.strip()) > min_text_len:
                    return False
        return True
    except Exception:
        return True


def fallback_ocr_text(
    pdf_path,
    dpi=OCR_DPI,
    lang=OCR_LANG,
    psm=OCR_PSM,
    reason="ocrmypdf failed",
    page_limit=None,
):
    """OCR each page to text with pytesseract/pdftoppm when ocrmypdf is unavailable or unwanted."""
    info(f"[OCR] Using direct pytesseract fallback ({reason}).")
    kwargs = {"dpi": dpi}
    if page_limit:
        kwargs["first_page"] = 1
        kwargs["last_page"] = page_limit
    images = convert_from_path(pdf_path, **kwargs)
    texts = []
    total = len(images)
    config = f"--psm {psm}"
    threshold_val = int(OCR_THRESHOLD) if OCR_THRESHOLD is not None else None

    for idx, img in enumerate(images, start=1):
        gray = img.convert("L")
        if threshold_val is not None:
            gray = ImageOps.autocontrast(gray)
            gray = gray.point(lambda p: 255 if p > threshold_val else 0)

        texts.append(pytesseract.image_to_string(gray, lang=lang, config=config))
        info(f"[OCR] Fallback OCR page {idx}/{total}")
    return texts


def clean_text_block(text):
    """Lightweight denoising: remove bullet dots, collapse long repeats, drop lines with almost no alphanumerics."""
    if not text:
        return ""
    cleaned_lines = []
    for raw_line in text.splitlines():
        line = raw_line
        line = re.sub(r"[·•◦●∙]", "", line)
        line = re.sub(r"(.)\\1{4,}", r"\\1\\1", line)  # collapse 5+ repeats to 2
        line = re.sub(r"([.!?,;-])\\1{2,}", r"\\1\\1", line)
        line = re.sub(r"\\s{2,}", " ", line)

        alnum = sum(ch.isalnum() for ch in line)
        total = len(line)
        if total >= 25 and total > 0 and (alnum / total) < 0.25:
            continue  # drop noisy lines

        stripped = line.strip()
        if stripped:
            cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines)


def collect_page_texts_for_metadata(
    pdf_path, text_overrides=None, max_pages=3, page_limit=None
):
    """Get text for the first pages, respecting fallback OCR text if provided."""
    limit = page_limit or max_pages
    if text_overrides:
        return text_overrides[:limit]
    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:limit]:
            texts.append(page.extract_text() or "")
    return texts


def extract_metadata_from_texts(page_texts):
    """
    Heuristic metadata extraction from the first few pages.
    Looks for common labels; falls back to first non-empty line for title.
    """
    meta = {
        "title": None,
        "study_no": None,
        "facility": None,
        "sponsor": None,
        "date": None,
    }

    lines = []
    for text in page_texts:
        lines.extend([ln.strip() for ln in (text or "").splitlines() if ln.strip()])

    def find_match(patterns):
        for line in lines:
            for pat in patterns:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
        return None

    meta["study_no"] = find_match(
        [
            r"study\s*(?:no\.?|number|id)[:\-\s]+(.+)",
        ]
    )
    meta["facility"] = find_match(
        [
            r"(?:facility|test\s*site|laboratory)[:\-\s]+(.+)",
        ]
    )
    meta["sponsor"] = find_match(
        [
            r"(?:sponsor|study\s*sponsor)[:\-\s]+(.+)",
        ]
    )
    meta["date"] = find_match(
        [
            r"(?:date|study\s*date|report\s*date|start\s*date|final\s*report\s*date)[:\-\s]+(.+)",
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\s+\d{1,2},?\s+\d{2,4})",
            r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        ]
    )

    title_candidate = find_match(
        [
            r"(?:title|study\s*title|protocol\s*title)[:\-\s]+(.+)",
        ]
    )
    if not title_candidate:
        # fallback: first non-empty line with a few words
        for ln in lines:
            if len(ln.split()) >= 3:
                title_candidate = ln
                break
    meta["title"] = title_candidate

    return meta


def ensure_ocr_pdf(
    pdf_path,
    output_dir,
    prefer_direct=False,
    lang=OCR_LANG,
    psm=OCR_PSM,
    page_limit=None,
):
    """
    Runs OCR if PDF has no extractable text.
    Returns (pdf_to_use, text_overrides) where text_overrides is a list of OCR
    strings per page when falling back to pytesseract.
    """
    if not needs_ocr(pdf_path, page_limit=page_limit):
        info("[OCR] PDF already contains text → skipping OCR")
        return pdf_path, None

    if prefer_direct:
        texts = fallback_ocr_text(
            pdf_path,
            lang=lang,
            psm=psm,
            reason="direct OCR requested",
            page_limit=page_limit,
        )
        text_dump = os.path.join(output_dir, "fallback_ocr_text.txt")
        with open(text_dump, "w", encoding="utf-8") as f:
            for idx, t in enumerate(texts, start=1):
                f.write(f"--- Page {idx} ---\n{t}\n\n")
        info("[OCR] Fallback text saved →", text_dump)
        return pdf_path, texts

    ocr_path = os.path.join(output_dir, "ocr_" + os.path.basename(pdf_path))
    info("[OCR] Running OCRmyPDF… this may take a moment.")

    cmd = [
        "ocrmypdf",
        "--skip-text",
        "--deskew",
        "-l",
        lang,
        "--tesseract-pagesegmode",
        psm,
    ]
    if page_limit:
        cmd.extend(["--pages", f"1-{page_limit}"])
    cmd.extend([pdf_path, ocr_path])
    try:
        subprocess.run(cmd, check=True)
        info("[OCR] OCR complete →", ocr_path)
        return ocr_path, None
    except subprocess.CalledProcessError as e:
        info(
            f"[OCR] ocrmypdf failed ({e}) → falling back to pytesseract text-only OCR"
        )
        texts = fallback_ocr_text(
            pdf_path,
            lang=lang,
            psm=psm,
            reason="ocrmypdf failed",
            page_limit=page_limit,
        )
        text_dump = os.path.join(output_dir, "fallback_ocr_text.txt")
        with open(text_dump, "w", encoding="utf-8") as f:
            for idx, t in enumerate(texts, start=1):
                f.write(f"--- Page {idx} ---\n{t}\n\n")
        info("[OCR] Fallback text saved →", text_dump)
        return pdf_path, texts


# ==========================================
# 1. Extract Tables → CSV + HTML
# ==========================================
def extract_tables(pdf_path, table_dir, page_limit=None):
    """Extract tables from the PDF into CSV/HTML snippets and return references."""
    os.makedirs(table_dir, exist_ok=True)
    table_refs = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
            if page_limit and page_idx > page_limit:
                break
            tables = page.find_tables()
            for t_idx, tb in enumerate(tables, start=1):
                df = pd.DataFrame(tb.extract())
                if df.shape[0] > 1:
                    df.columns = df.iloc[0]
                    df = df.drop(df.index[0]).reset_index(drop=True)

                csv_name = f"table_page_{page_idx}_idx_{t_idx}.csv"
                csv_path = os.path.join(table_dir, csv_name)
                df.to_csv(csv_path, index=False)

                table_refs.append(
                    {
                        "page": page_idx,
                        "idx": t_idx,
                        "csv": csv_path,
                        "html": df.to_html(index=False, border=1),
                        "bbox": tb.bbox,
                    }
                )

                info(f"[TABLE] Saved {csv_path}")

    return table_refs


# ==========================================
# 2. Extract Images + Captions
# ==========================================
def extract_image_caption(page, bbox, caption_window=60):
    """Find text below the image up to caption_window pixels."""
    x0, top, x1, bottom = bbox
    words = page.extract_words() or []

    caption_words = [w for w in words if bottom <= w["top"] <= bottom + caption_window]

    caption_words.sort(key=lambda w: (w["top"], w["x0"]))
    caption = " ".join(w["text"] for w in caption_words).strip()
    return caption or None


def extract_images(pdf_path, image_dir, page_limit=None):
    """Extract images from the PDF pages, save PNGs, and capture simple captions."""
    os.makedirs(image_dir, exist_ok=True)
    image_refs = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
            if page_limit and page_idx > page_limit:
                break
            for img_idx, img in enumerate(page.images, start=1):
                page_x0, page_y0, page_x1, page_y1 = page.bbox

                x0 = clamp(img["x0"], page_x0, page_x1)
                x1 = clamp(img["x1"], page_x0, page_x1)
                top = clamp(img["top"], page_y0, page_y1)
                bottom = clamp(img["bottom"], page_y0, page_y1)

                if x1 <= x0 or bottom <= top:
                    continue

                crop = page.crop((x0, top, x1, bottom))
                raw = crop.to_image(resolution=200)
                pil = convert_to_pil(raw)

                img_name = f"image_page_{page_idx}_idx_{img_idx}.png"
                img_path = os.path.join(image_dir, img_name)
                pil.save(img_path)

                caption = extract_image_caption(page, (x0, top, x1, bottom))
                if not caption:
                    caption = f"Image page {page_idx} #{img_idx}"

                image_refs.append(
                    {
                        "page": page_idx,
                        "idx": img_idx,
                        "file": img_path,
                        "caption": caption,
                    }
                )

                info(f"[IMAGE] Saved {img_path} (caption: {caption})")

    return image_refs


# ==========================================
# 3. Build Markdown (with HTML tables + image links)
# ==========================================
def build_markdown(
    pdf_path, table_refs, image_refs, md_path, text_overrides=None, page_limit=None
):
    """Assemble page text, tables (HTML), and image links into a Markdown document."""
    tables_by_page = {}
    images_by_page = {}

    for t in table_refs:
        tables_by_page.setdefault(t["page"], []).append(t)

    for im in image_refs:
        images_by_page.setdefault(im["page"], []).append(im)

    output = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
            if page_limit and page_idx > page_limit:
                break
            if text_overrides and len(text_overrides) >= page_idx:
                text = text_overrides[page_idx - 1] or ""
            else:
                text = page.extract_text() or ""
            if CLEAN_NOISE:
                text = clean_text_block(text)
            output.append(f"\n\n<!-- Page {page_idx} -->\n\n{text}\n\n")

            # HTML Tables
            for t in tables_by_page.get(page_idx, []):
                output.append(f"**Table (source: `{t['csv']}`)**\n\n")
                output.append(t["html"])
                output.append("\n\n")

            # Images
            for im in images_by_page.get(page_idx, []):
                output.append(f"![{im['caption']}]({im['file']})\n\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("".join(output))

    info("[TEXT] Markdown saved →", md_path)


# ==========================================
# 4. Pandoc → DOCX
# ==========================================
def pandoc_to_docx(md_path, docx_path):
    """Convert a Markdown file to DOCX using pandoc."""
    cmd = ["pandoc", md_path, "-o", docx_path]
    subprocess.run(cmd, check=True)
    info("[DOCX] Generated:", docx_path)


# ==========================================
# MAIN PIPELINE
# ==========================================
def run_pipeline(pdf_path, out_dir, page_limit=None):
    """Full pipeline: OCR if needed, extract tables/images, build Markdown, and output DOCX/metadata."""
    os.makedirs(out_dir, exist_ok=True)
    effective_page_limit = page_limit or PAGE_LIMIT

    # Step 0 – Ensure OCR if needed
    pdf_for_work, text_overrides = ensure_ocr_pdf(
        pdf_path,
        out_dir,
        prefer_direct=PREFER_DIRECT_OCR,
        lang=OCR_LANG,
        psm=OCR_PSM,
        page_limit=effective_page_limit,
    )

    # Step 0.5 – Extract basic metadata
    meta_texts = collect_page_texts_for_metadata(
        pdf_for_work,
        text_overrides=text_overrides,
        page_limit=effective_page_limit,
    )
    metadata = extract_metadata_from_texts(meta_texts)
    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(metadata, mf, indent=2)
    info("[META] Saved metadata →", meta_path)

    # Step 1 – Tables
    table_dir = os.path.join(out_dir, "tables")
    tables = extract_tables(pdf_for_work, table_dir, page_limit=effective_page_limit)

    # Step 2 – Images
    image_dir = os.path.join(out_dir, "images")
    images = extract_images(pdf_for_work, image_dir, page_limit=effective_page_limit)

    # Step 3 – Markdown assembling
    md_path = os.path.join(out_dir, "document.md")
    build_markdown(
        pdf_for_work,
        tables,
        images,
        md_path,
        text_overrides=text_overrides,
        page_limit=effective_page_limit,
    )

    # Step 4 – Pandoc docx
    docx_path = os.path.join(out_dir, "document.docx")
    pandoc_to_docx(md_path, docx_path)

    info("\n=== PIPELINE COMPLETE ===")
    info("Output folder:", out_dir)
    info("DOCX file:", docx_path)
    info("Metadata file:", meta_path)


# ==========================================
# CLI ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if len(sys.argv) != 3:
        info("\nUsage:")
        info("  python pipeline.py input.pdf output_folder\n")
        info("Env vars to tune OCR:")
        info("  OCR_LANG (default: eng)")
        info("  OCR_PSM (default: 6)")
        info("  OCR_DPI (default: 300)")
        info("  OCR_THRESHOLD (optional 0-255; apply binarization in fallback)")
        info("  PREFER_DIRECT_OCR=1 (skip ocrmypdf, go straight to pytesseract)\n")
        info("  PAGE_LIMIT (optional; process only the first N pages)\n")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_folder = sys.argv[2]

    run_pipeline(input_pdf, output_folder)
