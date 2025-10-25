from pathlib import Path
import uvicorn

from pdf_analysis.api.server import export_openapi_to_file, app

# from main import app, export_openapi_to_file

if __name__ == "__main__":
    out = Path("docs") / "openapi.json"
    out.parent.mkdir(exist_ok=True)
    export_openapi_to_file(app, out)
