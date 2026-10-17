"""Merge PDFs and images into A4 pages, saved as one PDF, one JPG per page, or
one long JPG.

Each source page is scaled to fit an A4 page (aspect ratio kept, centred,
nothing cropped). Images are EXIF-rotated first, so phone photos come out
upright. Used by pdf_merge.pyw; also runnable from the command line:

    python pdfmerge_core.py out.pdf in1.pdf photo.jpg in2.pdf
    python pdfmerge_core.py out.jpg [--long] in1.pdf photo.jpg
"""
import io
import os
import re
import sys

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation

__version__ = "1.1.0"

A4_W, A4_H = 595.276, 841.890  # points (210 x 297 mm)
IMG_MAX_SIDE = 3000             # px; ~250 dpi on A4, keeps phone photos small
JPEG_QUALITY = 90
JPG_DPI = 200                   # "JPG per page" output: A4 = 1654 x 2339 px
LONG_WIDTH = 1240               # "long image" width (150 dpi A4); plenty for phone screens
LONG_GAP = 24                   # band between pages in the long image
LONG_GAP_COLOR = "#cfcfcf"
JPEG_MAX_SIDE = 65000           # the JPEG format tops out at 65535 px

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


def _build_a4(items, landscape_ok, progress):
    writer = PdfWriter()
    for n, (path, rotation) in enumerate(items, 1):
        if progress:
            progress(f"กำลังรวม {n}/{len(items)}: {os.path.basename(path)}")
        try:
            if kind_of(path) == "pdf":
                for page in _pdf_pages(path, rotation):
                    _place_on_a4(writer, page, landscape_ok)
            else:
                _place_on_a4(writer, _image_to_pdf_page(path, rotation), landscape_ok)
        except Exception as e:
            raise RuntimeError(f"{os.path.basename(path)}: {e}") from e
    writer.compress_identical_objects()
    return writer


def jpg_page_paths(out_path, pages):
    """name.jpg for a single page; name_1.jpg … name_N.jpg (zero-padded) for more."""
    if pages == 1:
        return [out_path]
    stem, ext = os.path.splitext(out_path)
    width = len(str(pages))
    return [f"{stem}_{i:0{width}d}{ext}" for i in range(1, pages + 1)]


def output_paths(fmt, out_path, pages):
    return jpg_page_paths(out_path, pages) if fmt == "jpg" else [out_path]


def _render_pages(pdf_bytes, scale_of, progress):
    """Yield each page of an in-memory PDF as an RGB image; scale_of(w_pt, h_pt) -> scale."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for i in range(len(pdf)):
            if progress:
                progress(f"กำลังสร้างรูป หน้า {i + 1}/{len(pdf)}")
            page = pdf[i]
            scale = scale_of(page.get_width(), page.get_height())
            img = page.render(scale=scale).to_pil().convert("RGB")
            page.close()
            yield img
    finally:
        pdf.close()


def merge(items, out_path, fmt="pdf", landscape_ok=True, progress=None):
    """Merge items onto A4 pages and save them as `fmt`:

    "pdf"  one PDF file
    "jpg"  one A4 JPG per page (JPG_DPI), named by jpg_page_paths()
    "long" every page stacked top-to-bottom into one tall JPG, LONG_WIDTH px wide

    items: list of (path, rotation_degrees_clockwise). Returns (pages, files written).
    """
    writer = _build_a4(items, landscape_ok, progress)
    pages = len(writer.pages)
    outputs = output_paths(fmt, out_path, pages)
    inputs = {os.path.normcase(os.path.abspath(p)) for p, _ in items}
    for o in outputs:
        if os.path.normcase(os.path.abspath(o)) in inputs:
            raise ValueError("ไฟล์ปลายทางซ้ำกับไฟล์ต้นฉบับ: " + os.path.basename(o))

    if fmt == "pdf":
        with open(out_path, "wb") as f:
            writer.write(f)
        return pages, outputs

    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    if fmt == "jpg":
        scale = JPG_DPI / 72
        for img, path in zip(_render_pages(data, lambda w, h: scale, progress), outputs):
            img.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True, dpi=(JPG_DPI, JPG_DPI))
        return pages, outputs

    if fmt != "long":
        raise ValueError("unknown format: " + fmt)
    # every page drawn LONG_WIDTH wide; shrink all of them if the stack would pass
    # the JPEG size limit
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in writer.pages]
    width = LONG_WIDTH
    total = sum(width * h / w for w, h in sizes) + LONG_GAP * (pages - 1)
    if total > JPEG_MAX_SIDE:
        width = int(width * (JPEG_MAX_SIDE - LONG_GAP * (pages - 1)) / (total - LONG_GAP * (pages - 1)))
    imgs = list(_render_pages(data, lambda w, h: width / w, progress))
    sheet = Image.new("RGB", (width, sum(i.height for i in imgs) + LONG_GAP * (pages - 1)),
                      LONG_GAP_COLOR)
    y = 0
    for img in imgs:
        if img.width != width:  # rounding in the renderer
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        sheet.paste(img, (0, y))
        y += img.height + LONG_GAP
    dpi = round(width / (A4_W / 72))
    sheet.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True, dpi=(dpi, dpi))
    return pages, outputs


if __name__ == "__main__":
    # python pdfmerge_core.py out.pdf|out.jpg [--long] input1 [input2 ...]
    args = sys.argv[1:]
    long_image = "--long" in args
    args = [a for a in args if a != "--long"]
    if len(args) < 2:
        sys.exit("usage: pdfmerge_core.py out.pdf|out.jpg [--long] input1 [input2 ...]")
    out = args[0]
    fmt = "long" if long_image else ("jpg" if out.lower().endswith((".jpg", ".jpeg")) else "pdf")
    files, skipped = expand_paths(args[1:])
    for s in skipped:
        print("skipped:", s)
    pages, written = merge([(f, 0) for f in files], out, fmt)
    print(f"{pages} pages ->", *written, sep="\n  ")
