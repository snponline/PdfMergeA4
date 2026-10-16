"""Create / remove the "รวม PDF (A4)" shortcuts on the Desktop and in Send to.

Uses PowerShell + WScript.Shell (present on every Windows), so the frozen exe
can do it on a customer's PC that has no Python.
"""
import base64
import ctypes
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes

LNK_NAME = "รวม PDF (A4).lnk"
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
FOLDERID_SENDTO = "{8983036C-27C0-404B-8F08-102D10DCFD74}"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _known_folder(folder_id):
    guid = _GUID()
    ctypes.windll.ole32.CLSIDFromString(folder_id, ctypes.byref(guid))
    ptr = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)):
        raise OSError("SHGetKnownFolderPath failed for " + folder_id)
    try:
        return ptr.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ptr)


def default_folders():
    return [_known_folder(FOLDERID_DESKTOP), _known_folder(FOLDERID_SENDTO)]


def launch_target():
    """(target, arguments) that start this program."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    here = os.path.dirname(os.path.abspath(__file__))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw, '"' + os.path.join(here, "pdf_merge.pyw") + '"'


def running_from_temp():
    """True when the exe was opened straight from inside a zip (Windows unpacks it to %TEMP%)."""
    if not getattr(sys, "frozen", False):
        return False
    exe = os.path.normcase(os.path.abspath(sys.executable))
    return exe.startswith(os.path.normcase(os.path.abspath(tempfile.gettempdir())) + os.sep)


def installed(folders=None):
    return all(os.path.exists(os.path.join(f, LNK_NAME)) for f in folders or default_folders())


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def install(folders=None):
    target, args = launch_target()
    icon = target if getattr(sys, "frozen", False) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pdfmerge.ico")
    lines = ["$ProgressPreference = 'SilentlyContinue'", "$ErrorActionPreference = 'Stop'",
             "$shell = New-Object -ComObject WScript.Shell"]
    for folder in folders or default_folders():
        # WScript.Shell saves shortcut paths as ANSI and mangles Thai, so save
        # under an ASCII name and rename afterwards.
        tmp = os.path.join(folder, "PdfMergeA4_tmp.lnk")
        lines += [
            f"$s = $shell.CreateShortcut({_ps_quote(tmp)})",
            f"$s.TargetPath = {_ps_quote(target)}",
            f"$s.Arguments = {_ps_quote(args)}",
            f"$s.WorkingDirectory = {_ps_quote(os.path.dirname(target))}",
            f"$s.IconLocation = {_ps_quote(icon + ',0')}",
            "$s.Description = 'Merge PDFs and images into one A4 PDF'",
            "$s.Save()",
            f"Move-Item -LiteralPath {_ps_quote(tmp)} "
            f"-Destination {_ps_quote(os.path.join(folder, LNK_NAME))} -Force",
        ]
    # -EncodedCommand is UTF-16, so Thai paths survive Windows PowerShell 5.1
    encoded = base64.b64encode("\n".join(lines).encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("mbcs", errors="replace").strip()[:500])


def remove(folders=None):
    for folder in folders or default_folders():
        path = os.path.join(folder, LNK_NAME)
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    # developer convenience: python shortcuts.py [--remove]
    remove() if "--remove" in sys.argv else install()
    print("installed" if installed() else "not installed", default_folders())
