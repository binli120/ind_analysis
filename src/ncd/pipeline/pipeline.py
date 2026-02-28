# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

import io
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pdfplumber
from PIL import Image
from utils.log import info


# ==========================================
# Utility functions
# ==========================================
def clamp(val, minv, maxv):
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
def needs_ocr(pdf_path, pages_to_check=3, min_text_len=50):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for p in pdf.pages[:pages_to_check]:
                txt = p.extract_text() or ""
                if len(txt.strip()) > min_text_len:
                    return False
        return True
    except :
        return True


def ensure_ocr_pdf(pdf_path, output_dir):
    """Runs OCR if PDF has no extractable text."""
    if not needs_ocr(pdf_path):
        info("[OCR] PDF already contains text → skipping OCR")
        return pdf_path

    ocr_path = os.path.join(output_dir, "ocr_" + os.path.basename(pdf_path))
    info("[OCR] Running OCRmyPDF… this may take a moment.")

    cmd = ["ocrmypdf", "--skip-text", "--deskew", pdf_path, ocr_path]
    subprocess.run(cmd, check=True)
    info("[OCR] OCR complete →", ocr_path)
    return ocr_path


# ==========================================
# 1. Extract Tables → CSV + HTML
# ==========================================
def extract_tables(pdf_path, table_dir):
    os.makedirs(table_dir, exist_ok=True)
    table_refs = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
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


def extract_images(pdf_path, image_dir):
    os.makedirs(image_dir, exist_ok=True)
    image_refs = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
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
def build_markdown(pdf_path, table_refs, image_refs, md_path):
    tables_by_page = {}
    images_by_page = {}

    for t in table_refs:
        tables_by_page.setdefault(t["page"], []).append(t)

    for im in image_refs:
        images_by_page.setdefault(im["page"], []).append(im)

    output = []

    with pdfplumber.open(pdf_path) as pdfdoc:
        for page_idx, page in enumerate(pdfdoc.pages, start=1):
            text = page.extract_text() or ""
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
    cmd = ["pandoc", md_path, "-o", docx_path]
    subprocess.run(cmd, check=True)
    info("[DOCX] Generated:", docx_path)


# ==========================================
# MAIN PIPELINE
# ==========================================
def run_pipeline(pdf_path, out_dir, output_basename=None):
    os.makedirs(out_dir, exist_ok=True)
    base_name = output_basename or Path(pdf_path).stem

    # Step 0 – Ensure OCR if needed
    pdf_for_work = ensure_ocr_pdf(pdf_path, out_dir)

    # Step 1 – Tables
    table_dir = os.path.join(out_dir, "tables")
    tables = extract_tables(pdf_for_work, table_dir)

    # Step 2 – Images
    image_dir = os.path.join(out_dir, "images")
    images = extract_images(pdf_for_work, image_dir)

    # Step 3 – Markdown assembling
    md_path = os.path.join(out_dir, f"{base_name}.md")
    build_markdown(pdf_for_work, tables, images, md_path)

    # Step 4 – Pandoc docx
    docx_path = os.path.join(out_dir, f"{base_name}.docx")
    pandoc_to_docx(md_path, docx_path)

    info("\n=== PIPELINE COMPLETE ===")
    info("Output folder:", out_dir)
    info("DOCX file:", docx_path)


# ==========================================
# CLI ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if len(sys.argv) != 3:
        info("\nUsage:")
        info("  python pipeline.py input.pdf output_folder\n")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_folder = sys.argv[2]

    run_pipeline(input_pdf, output_folder)
