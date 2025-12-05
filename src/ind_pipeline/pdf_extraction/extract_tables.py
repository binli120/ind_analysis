import pdfplumber
import pandas as pd
import os

PDF_PATH = "test.pdf"
OUTPUT_DIR = "extracted_tables"


def extract_all_tables_to_csv(pdf_path, output_dir):
    """Detect all tables in a PDF, convert to DataFrames, and save each as CSV."""

    os.makedirs(output_dir, exist_ok=True)
    saved_files = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            page_tables = page.extract_tables()

            if page_tables:
                for t_idx, table in enumerate(page_tables, start=1):

                    # Convert table to DataFrame
                    df = pd.DataFrame(table)

                    # Auto-use first row as header if looks like headers
                    if df.shape[0] > 1:
                        df.columns = df.iloc[0]  # set header
                        df = df.drop(df.index[0]).reset_index(drop=True)

                    # Build filename
                    filename = f"page_{page_idx}_table_{t_idx}.csv"
                    filepath = os.path.join(output_dir, filename)

                    # Save as CSV
                    df.to_csv(filepath, index=False)

                    saved_files.append(filepath)

                    print(f"Saved table: {filepath}")

    return saved_files


# Run extraction + saving
saved = extract_all_tables_to_csv(PDF_PATH, OUTPUT_DIR)

print("\n=== Completed ===")
print(f"Total tables saved: {len(saved)}")
for f in saved:
    print(f)