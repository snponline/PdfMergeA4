"""Merge PDFs and images into one PDF where every page is A4.

Each source page is scaled to fit an A4 page (aspect ratio kept, centred,
nothing cropped). Images are EXIF-rotated first, so phone photos come out
upright. Used by pdf_merge.pyw; also runnable from the command line:

    python pdfmerge_core.py out.pdf in1.pdf photo.jpg in2.pdf
"""
import io
import os
import re
import sys

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation

__version__ = "1.0.1"

A4_W, A4_H = 595.276, 841.890  # points (210 x 297 mm)
IMG_MAX_SIDE = 3000             # px; ~250 dpi on A4, keeps phone photos small
JPEG_QUALITY = 90

PDF_EXTS = {".pdf"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    IMG_EXTS |= {".heic", ".heif"}
except ImportError:
    pass
SUPPORTED_EXTS = PDF_EXTS | IMG_EXTS


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMG_EXTS:
        return "image"
    return None


def natural_key(path):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def expand_paths(paths):
    """Files stay as given; folders expand to their supported files (natural order)."""
    out, skipped = [], []
    for p in paths:
        if os.path.isdir(p):
            inner = [os.path.join(p, f) for f in os.listdir(p)]
            out.extend(sorted((f for f in inner if os.path.isfile(f) and kind_of(f)),
                              key=natural_key))
        elif os.path.isfile(p) and kind_of(p):
            out.append(p)
        else:
            skipped.append(p)
    return out, skipped


def open_reader(path):
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF มีรหัสผ่าน")
    return reader


def page_count(path):
    return len(open_reader(path).pages) if kind_of(path) == "pdf" else 1


def load_image(path, rotation=0):
    """Open an image upright (EXIF), then rotate clockwise by `rotation` degrees."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if rotation:
        img = img.rotate(-rotation, expand=True)
    return img


def _image_to_pdf_page(path, rotation):
    img = load_image(path, rotation)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > IMG_MAX_SIDE:
        img.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=72.0, quality=JPEG_QUALITY)
    buf.seek(0)
    return PdfReader(buf).pages[0]


def _flattened_reader(path):
    """Burn annotations (filled form fields, stamps) into page content.

    merge_transformed_page only copies the content stream, so without this a
    filled-in PDF form would lose its typed values.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    pdf = pdfium.PdfDocument(path)
    try:
        pdf.init_forms()
        for i in range(len(pdf)):
            page = pdf[i]
            pdfium_c.FPDFPage_Flatten(page.raw, pdfium_c.FLAT_PRINT)
            page.close()
        buf = io.BytesIO()
        pdf.save(buf)
    finally:
        pdf.close()
    buf.seek(0)
    return PdfReader(buf)


def _pdf_pages(path, rotation):
    reader = open_reader(path)
    if any("/Annots" in p for p in reader.pages):
        try:
            reader = _flattened_reader(path)
        except Exception:
            pass  # fall back to content-only; better than failing the merge
    for page in reader.pages:
        if rotation:
            page.rotate(rotation)
        yield page


def _place_on_a4(writer, src, landscape_ok):
    src.transfer_rotation_to_content()
    box = src.cropbox
    w, h = float(box.width), float(box.height)
    pw, ph = (A4_H, A4_W) if (landscape_ok and w > h) else (A4_W, A4_H)
    s = min(pw / w, ph / h)
    tx = (pw - w * s) / 2 - float(box.left) * s
    ty = (ph - h * s) / 2 - float(box.bottom) * s
    page = writer.add_blank_page(pw, ph)
    page.merge_transformed_page(src, Transformation().scale(s).translate(tx, ty))


def merge_to_a4(items, out_path, landscape_ok=True, progress=None):
    """items: list of (path, rotation_degrees_clockwise). Returns pages written."""
    out_norm = os.path.normcase(os.path.abspath(out_path))
    for path, _ in items:
        if os.path.normcase(os.path.abspath(path)) == out_norm:
            raise ValueError("ไฟล์ปลายทางซ้ำกับไฟล์ต้นฉบับ: " + os.path.basename(path))

    writer = PdfWriter()
    for n, (path, rotation) in enumerate(items, 1):
        if progress:
            progress(n, len(items), path)
        try:
            if kind_of(path) == "pdf":
                for page in _pdf_pages(path, rotation):
                    _place_on_a4(writer, page, landscape_ok)
            else:
                _place_on_a4(writer, _image_to_pdf_page(path, rotation), landscape_ok)
        except Exception as e:
            raise RuntimeError(f"{os.path.basename(path)}: {e}") from e

    writer.compress_identical_objects()
    with open(out_path, "wb") as f:
        writer.write(f)
    return len(writer.pages)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: pdfmerge_core.py out.pdf input1 [input2 ...]")
    files, skipped = expand_paths(sys.argv[2:])
    for s in skipped:
        print("skipped:", s)
    print("pages:", merge_to_a4([(f, 0) for f in files], sys.argv[1]))
