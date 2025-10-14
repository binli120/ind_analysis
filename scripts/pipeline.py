import logging
from pathlib import Path

from pdf_analysis.pipeline import PDFProcessingPipeline, PipelineConfig

logging.basicConfig(level=logging.DEBUG)
pipeline = PDFProcessingPipeline(PipelineConfig())
result = pipeline.run(
    Path(
        "data/42-stud-rep/421-pharmacol/4211-prim-pd/lt3114-pha-001-r/lt3114-pha-001-r.pdf"
    )
)

print("Text engine:", result.text_engine)
print("OCR strategy:", result.ocr_strategy)
print("Tables:", len(result.tables))
print("OCR strategy:", result.ocr_strategy)
print("Tables:", len(result.tables))
