Module 4 extraction helpers for building the 2.4/2.6 scaffolding.

Prereqs
- Ensure `ocrmypdf` and `pandoc` are installed and on PATH (used for OCR + DOCX generation).

Commands
- Single PDF (local): `python src/dictionary/pipeline.py /path/to/input.pdf /path/to/output_dir`
  - Writes `<pdf-name>.md`, `<pdf-name>.docx`, plus `images/` and `tables/` inside the output dir.
- Batch from S3: `python scripts/process_module4_pdfs.py --bucket YOUR_BUCKET --prefix "company/project/Module 4" --output-dir tmp/module4_extracted`
  - Add `--limit 5` to cap how many PDFs run; add `--force` to overwrite existing per-PDF folders.

Notes
- Each PDF gets its own folder named after the PDF stem under the chosen output dir.
- Outputs are ready to feed into the Module 2.4/2.6 dictionary builder. 
