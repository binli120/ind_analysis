import pdfplumber
import os
from PIL import Image
import io

PDF_PATH = "images.pdf"
OUTPUT_DIR = "extract_images"

def clamp(val, minv, maxv):
    return max(minv, min(val, maxv))

def convert_to_pil(raw_image):
    """
    Convert pdfplumber to_image() output into a PIL Image.
    Handles:
      - raw bytes
      - raw PIL.Image.Image
      - objects with .original containing bytes or PIL.Image
    """
    # Case 1 – raw bytes
    if isinstance(raw_image, (bytes, bytearray)):
        return Image.open(io.BytesIO(raw_image))

    # Case 2 – raw PIL image
    if isinstance(raw_image, Image.Image):
        return raw_image

    # Case 3 – pdfplumber PageImage
    if hasattr(raw_image, "original"):
        orig = raw_image.original

        if isinstance(orig, (bytes, bytearray)):
            return Image.open(io.BytesIO(orig))

        if isinstance(orig, Image.Image):
            return orig

    raise TypeError("Unknown image type from pdfplumber.to_image() output.")


def extract_images(pdf_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    saved_images = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            images = page.images
            print(f"Page {page_idx}: found {len(images)} image(s).")

            for img_idx, img in enumerate(images, start=1):

                # Extract bounding box
                x0, top, x1, bottom = img["x0"], img["top"], img["x1"], img["bottom"]

                # Clamp bounding box into valid page coordinates
                px0, ptop, px1, pbottom = page.bbox
                x0 = clamp(x0, px0, px1)
                x1 = clamp(x1, px0, px1)
                top = clamp(top, ptop, pbottom)
                bottom = clamp(bottom, ptop, pbottom)

                # Ensure valid bbox
                if x1 <= x0 or bottom <= top:
                    print(f"Skipped invalid bbox on page {page_idx} image {img_idx}")
                    continue

                try:
                    # Crop and convert
                    cropped = page.crop((x0, top, x1, bottom))
                    raw = cropped.to_image(resolution=300)
                    pil_img = convert_to_pil(raw)
                except Exception as e:
                    print(f"Failed to extract image on page {page_idx}, image {img_idx}: {e}")
                    continue

                # Save image
                img_filename = f"page_{page_idx}_image_{img_idx}.png"
                img_path = os.path.join(output_dir, img_filename)
                pil_img.save(img_path)

                saved_images.append(img_path)
                print(f"Saved image: {img_path}")

    return saved_images


# Execute extraction
images = extract_images(PDF_PATH, OUTPUT_DIR)

print("\n=== COMPLETED ===")
print(f"Total images saved: {len(images)}")
for f in images:
    print(f)