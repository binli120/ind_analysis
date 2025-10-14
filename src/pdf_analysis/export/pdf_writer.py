# @author: Bin Lee
# @email: blee@filynai.com

from pathlib import Path


def markdown_to_pdf(markdown_text: str, pdf_path: Path) -> None:
    """
    Render markdown-ish text into a simple PDF representation.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required to emit modified PDF drafts. "
            "Install it or run `poetry install` with the default dependencies."
        ) from exc

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    width, height = letter
    x_margin = inch
    y_margin = inch
    line_height = 0.22 * inch

    y = height - y_margin
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip("\n")
        if not line:
            y -= line_height
        else:
            if y <= y_margin:
                c.showPage()
                y = height - y_margin
            c.drawString(x_margin, y, line)
            y -= line_height
    c.save()
