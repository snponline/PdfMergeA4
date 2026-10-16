"""Build PdfMergeA4.exe and the release zip.

    python build_exe.py [out_folder]        (default: dist)

out_folder gets PdfMergeA4.exe and PdfMergeA4.zip (exe + README.txt +
THIRD-PARTY-NOTICES.txt). PyInstaller's work files go to %TEMP%, so nothing
build-related lands in a synced folder.
"""
import glob
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

from pdfmerge_core import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "PdfMergeA4"

ZIP_README = f"""รวม PDF เป็น A4 (PdfMergeA4) v{__version__}
====================================

รวมไฟล์ PDF และรูป (JPG, PNG) เป็น PDF ไฟล์เดียว ทุกหน้าขนาด A4

วิธีใช้
1. แตกไฟล์ zip ก่อน (คลิกขวา > Extract All) แล้ววางโฟลเดอร์ไว้ที่ที่ต้องการ
2. ดับเบิลคลิก PdfMergeA4.exe
3. ลากไฟล์ PDF / รูป มาวางในหน้าต่าง เรียงลำดับ แล้วกด "รวมเป็น PDF (A4)"

กดปุ่ม "สร้างทางลัด" ในโปรแกรม เพื่อเพิ่มไอคอนบน Desktop
และเมนูคลิกขวา > Send to > รวม PDF (A4)

ถ้า Windows ขึ้น "Windows protected your PC" ให้กด More info > Run anyway
(โปรแกรมไม่มีลายเซ็นดิจิทัล)

ซอร์สโค้ดและเวอร์ชันล่าสุด: https://github.com/snponline/PdfMergeA4
"""

VERSION_FILE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={v4}, prodvers={v4}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'snponline'),
      StringStruct('FileDescription', 'PdfMergeA4 - merge PDFs and images into one A4 PDF'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def third_party_notices():
    """License texts of everything bundled into the exe."""
    parts = [f"{NAME} bundles the following third-party software.\n"]

    def add(title, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            parts.append(f"\n{'=' * 78}\n{title}\n{'=' * 78}\n\n{f.read().strip()}\n")

    add(f"Python {sys.version.split()[0]} (PSF License)", os.path.join(sys.base_prefix, "LICENSE.txt"))
    for terms in sorted(glob.glob(os.path.join(sys.base_prefix, "tcl", "*", "license.terms"))):
        add(f"Tcl/Tk ({os.path.basename(os.path.dirname(terms))})", terms)
    for dist in ("pypdf", "pillow", "pypdfium2", "tkinterdnd2", "pyinstaller"):
        d = md.distribution(dist)
        for f in d.files or []:
            if any(k in str(f).upper() for k in ("LICENSE", "COPYING", "NOTICE")):
                add(f"{dist} {d.version} - {f.name}", str(f.locate()))
    return "".join(parts)


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dist"))
    os.makedirs(out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="pdfmerge_build_")
    try:
        v4 = tuple(int(x) for x in __version__.split(".")) + (0,)
        version_file = os.path.join(work, "version.txt")
        with open(version_file, "w", encoding="utf-8") as f:
            f.write(VERSION_FILE.format(v4=v4, v=__version__, name=NAME))

        icon = os.path.join(HERE, "pdfmerge.ico")
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
            "--name", NAME, "--icon", icon, "--add-data", f"{icon};.",
            "--version-file", version_file,
            "--collect-all", "tkinterdnd2", "--collect-all", "pypdfium2",
            *[a for m in ("numpy", "pandas", "matplotlib", "scipy", "IPython")
              for a in ("--exclude-module", m)],
            "--distpath", out, "--workpath", os.path.join(work, "build"), "--specpath", work,
            os.path.join(HERE, "pdf_merge.pyw"),
        ], check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    exe = os.path.join(out, NAME + ".exe")
    zip_path = os.path.join(out, NAME + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, NAME + ".exe")
        # BOM so Notepad on older Windows shows the Thai text correctly
        z.writestr("README.txt", "﻿" + ZIP_README.replace("\n", "\r\n"))
        z.writestr("THIRD-PARTY-NOTICES.txt", third_party_notices().replace("\n", "\r\n"))
    print(f"\nbuilt v{__version__}:\n  {exe}\n  {zip_path}")


if __name__ == "__main__":
    main()
