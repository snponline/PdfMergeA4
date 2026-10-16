"""รวม PDF / รูป เป็น A4 — a small window for ordering files before merging.

Opened from Explorer's "Send to" menu (selected files arrive as argv) or by
double-clicking this file. Files and folders can also be dragged onto the list.
Rows can be dragged to reorder; the merge itself lives in pdfmerge_core.py.
"""
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

import pdfmerge_core as core
import shortcuts

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

FONT = ("Leelawadee UI", 10)
PREVIEW_W, PREVIEW_H = 240, 320
RENDER_LONG_SIDE = 700  # px rendered before shrinking into the preview


def app_dir():
    """Folder of the .exe when frozen by PyInstaller, else of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource(name):
    """Bundled file: unpacked to _MEIPASS inside the onefile exe."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def short_path(path):
    """'folder\\file.pdf' — enough to tell same-named files apart in the status bar."""
    folder = os.path.basename(os.path.dirname(path))
    return os.path.join(folder, os.path.basename(path)) if folder else path


def render_source(path, rotation):
    """First page of a PDF or an image, upright, rotated clockwise, as RGB."""
    if core.kind_of(path) == "pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        try:
            pdf.init_forms()  # so filled-in form fields show in the preview
            page = pdf[0]
            scale = RENDER_LONG_SIDE / max(page.get_width(), page.get_height())
            img = page.render(scale=scale).to_pil().convert("RGB")
            page.close()
        finally:
            pdf.close()
    else:
        img = Image.open(path)
        if img.format == "JPEG":
            img.draft("RGB", (RENDER_LONG_SIDE * 2, RENDER_LONG_SIDE * 2))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            img = Image.new("RGB", rgba.size, "white")
            img.paste(rgba, mask=rgba.getchannel("A"))
        else:
            img = img.convert("RGB")
    if rotation:
        img = img.rotate(-rotation, expand=True)
    return img


def make_preview(path, rotation, landscape_ok):
    """The source drawn on an A4 sheet, as it will come out of the merge."""
    img = render_source(path, rotation)
    if landscape_ok and img.width > img.height:
        pw, ph = PREVIEW_W, round(PREVIEW_W * core.A4_W / core.A4_H)
    else:
        pw, ph = round(PREVIEW_H * core.A4_W / core.A4_H), PREVIEW_H
    sheet = Image.new("RGB", (pw, ph), "white")
    img = ImageOps.contain(img, (pw, ph), Image.LANCZOS)
    sheet.paste(img, ((pw - img.width) // 2, (ph - img.height) // 2))
    return ImageOps.expand(sheet, border=1, fill="#9a9a9a")


class App:
    def __init__(self, root, initial):
        self.root = root
        self.items = {}  # tree iid -> {"path", "rot", "pages"}
        self.q = queue.Queue()
        self.busy = False
        self._drag = None
        self._photo = None

        root.title(f"รวม PDF เป็น A4 · v{core.__version__}")
        if os.path.exists(resource("pdfmerge.ico")):
            root.iconbitmap(default=resource("pdfmerge.ico"))
        root.geometry("920x580")
        root.minsize(760, 460)
        self._build()
        self.add_files(sorted(initial, key=core.natural_key))

    # ---------- layout ----------
    def _build(self):
        root = self.root
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=FONT[0], size=FONT[1])
        style = ttk.Style()
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=line + 10)
        style.configure("Big.TButton", font=(FONT[0], 11, "bold"), padding=(14, 8))

        hint = ttk.Label(root, foreground="#555",
                         text="ลากไฟล์ PDF / รูป มาวางที่รายการได้ · ลากแถวเพื่อสลับลำดับ · "
                              "ทุกหน้าจะถูกปรับเป็น A4 โดยไม่ตัดขอบ")
        hint.pack(fill="x", padx=12, pady=(10, 4))

        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, padx=12)

        # file list
        listf = ttk.Frame(mid)
        cols = ("no", "name", "pages", "rot")
        self.tree = ttk.Treeview(listf, columns=cols, show="headings", selectmode="extended")
        for c, text, w, anchor, stretch in (
                ("no", "#", 40, "center", False),
                ("name", "ไฟล์", 240, "w", True),
                ("pages", "หน้า", 56, "center", False),
                ("rot", "หมุน", 60, "center", False)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(listf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        # shown over the empty list; clicking it opens the file picker
        self.empty_hint = tk.Label(self.tree, bg="white", fg="#8a8a8a", cursor="hand2",
                                   font=(FONT[0], 13), justify="center",
                                   text="ลากไฟล์ PDF / รูป มาวางที่นี่\n\nหรือคลิกตรงนี้เพื่อเลือกไฟล์")
        self.empty_hint.bind("<Button-1>", lambda e: self.browse())

        # buttons
        btns = ttk.Frame(mid)
        for text, cmd in (("▲  ขึ้น", lambda: self.move(-1)),
                          ("▼  ลง", lambda: self.move(1)),
                          ("↻  หมุน 90°", self.rotate),
                          (None, None),
                          ("+  เพิ่มไฟล์…", self.browse),
                          ("×  เอาออก", self.remove),
                          (None, None),
                          ("เปิดดูไฟล์", self.open_selected)):
            if text is None:
                ttk.Separator(btns).pack(fill="x", pady=6)
                continue
            b = ttk.Button(btns, text=text, command=cmd, width=14)
            b.pack(fill="x", pady=2)
        ttk.Separator(btns).pack(fill="x", pady=6)
        self.shortcut_btn = ttk.Button(btns, command=self.toggle_shortcuts, width=14)
        self.shortcut_btn.pack(fill="x", pady=2)
        self.refresh_shortcut_btn()

        # preview: fixed box so portrait/landscape previews don't shift the layout
        pv = ttk.Frame(mid)
        box =ttk.Frame(pv, width=PREVIEW_W + 4, height=PREVIEW_H + 4)
        box.pack_propagate(False)
        box.pack(pady=(0, 4))
        self.preview = ttk.Label(box, anchor="n")
        self.preview.pack(fill="both", expand=True)
        self.preview_cap = ttk.Label(pv, foreground="#555", anchor="center")
        self.preview_cap.pack()

        # pack right-hand panels first so a narrow window squeezes the list, not the preview
        pv.pack(side="right", fill="y")
        btns.pack(side="right", fill="y", padx=10)
        listf.pack(side="left", fill="both", expand=True)

        # bottom bar
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=12, pady=10)
        self.landscape = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="หน้าแนวนอนใช้ A4 แนวนอน",
                        variable=self.landscape, command=self.update_preview).pack(side="left")
        self.merge_btn = ttk.Button(bottom, text="รวมเป็น PDF (A4)…", style="Big.TButton",
                                    command=self.merge)
        self.merge_btn.pack(side="right")
        self.status = ttk.Label(bottom, foreground="#333")
        self.status.pack(side="left", padx=16, fill="x", expand=True)

        # events
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<ButtonPress-1>", self.drag_start, add="+")
        self.tree.bind("<B1-Motion>", self.drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None), add="+")
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<Control-Up>", lambda e: (self.move(-1), "break")[1])
        self.tree.bind("<Control-Down>", lambda e: (self.move(1), "break")[1])
        self.tree.bind("<Control-r>", lambda e: self.rotate())

        if hasattr(root, "drop_target_register"):
            for w in (root, self.tree, self.preview, self.empty_hint):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

    # ---------- list operations ----------
    def order(self):
        return list(self.tree.get_children())

    def refresh_rows(self):
        total = 0
        for n, iid in enumerate(self.order(), 1):
            it = self.items[iid]
            total += it["pages"]
            self.tree.item(iid, values=(n, os.path.basename(it["path"]), it["pages"],
                                        f"{it['rot']}°" if it["rot"] else ""))
        if self.items:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        if not self.busy:
            count = len(self.items)
            self.status.configure(text=f"{count} ไฟล์ · {total} หน้า" if count
                                  else "ยังไม่มีไฟล์ — กด เพิ่มไฟล์ หรือ ลากไฟล์มาวาง")

    def add_files(self, paths):
        files, skipped = core.expand_paths(paths)
        problems = [f"{os.path.basename(p)}: ไม่รองรับไฟล์ชนิดนี้" for p in skipped]
        first_new = None
        for f in files:
            try:
                pages = core.page_count(f)
            except Exception as e:
                problems.append(f"{os.path.basename(f)}: เปิดไม่ได้ ({e})")
                continue
            iid = self.tree.insert("", "end", values=())
            self.items[iid] = {"path": f, "rot": 0, "pages": pages}
            first_new = first_new or iid
        self.refresh_rows()
        if first_new and not self.tree.selection():
            self.tree.selection_set(first_new)
            self.tree.focus(first_new)
        if problems:
            messagebox.showwarning("บางไฟล์ไม่ได้เพิ่ม", "\n".join(problems), parent=self.root)

    def browse(self):
        exts = " ".join("*" + e for e in sorted(core.SUPPORTED_EXTS))
        sel = self.selected()
        start = os.path.dirname(self.items[sel[0]]["path"]) if sel else None
        paths = filedialog.askopenfilenames(
            parent=self.root, title="เลือกไฟล์ PDF หรือรูป", initialdir=start,
            filetypes=[("PDF และรูป", exts), ("ทุกไฟล์", "*.*")])
        if paths:
            self.add_files(list(paths))

    def on_drop(self, event):
        self.add_files(list(self.root.tk.splitlist(event.data)))
        return event.action

    def selected(self):
        order = self.order()
        return sorted(self.tree.selection(), key=order.index)

    def move(self, delta):
        sel = self.selected()
        if not sel:
            return
        order = self.order()
        if (delta < 0 and order.index(sel[0]) == 0) or \
           (delta > 0 and order.index(sel[-1]) == len(order) - 1):
            return
        for iid in (sel if delta < 0 else reversed(sel)):
            self.tree.move(iid, "", self.tree.index(iid) + delta)
        self.tree.see(sel[0] if delta < 0 else sel[-1])
        self.refresh_rows()

    def rotate(self):
        for iid in self.selected():
            self.items[iid]["rot"] = (self.items[iid]["rot"] + 90) % 360
        self.refresh_rows()
        self.update_preview()

    def remove(self):
        sel = self.selected()
        if not sel:
            return
        nxt = self.tree.next(sel[-1]) or self.tree.prev(sel[0])
        for iid in sel:
            self.tree.delete(iid)
            del self.items[iid]
        if nxt and nxt in self.items:
            self.tree.selection_set(nxt)
            self.tree.focus(nxt)
        self.refresh_rows()
        self.update_preview()

    def open_selected(self):
        sel = self.selected()
        if sel:
            os.startfile(self.items[sel[0]]["path"])

    def drag_start(self, event):
        row = self.tree.identify_row(event.y)
        self._drag = row if row and self.tree.identify_region(event.x, event.y) == "cell" else None

    def drag_motion(self, event):
        if not self._drag:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag:
            self.tree.move(self._drag, "", self.tree.index(target))
            self.tree.selection_set(self._drag)
            self.refresh_rows()

    # ---------- Desktop / Send to shortcuts ----------
    def refresh_shortcut_btn(self):
        try:
            on = shortcuts.installed()
        except Exception:
            on = False
        self.shortcut_btn.configure(text="ลบทางลัด…" if on else "สร้างทางลัด…")

    def toggle_shortcuts(self):
        if shortcuts.running_from_temp():
            messagebox.showwarning(
                "กรุณาแตกไฟล์ zip ก่อน",
                "ตอนนี้โปรแกรมถูกเปิดจากในไฟล์ zip โดยตรง ทางลัดที่สร้างจะใช้ไม่ได้\n\n"
                "คลิกขวาที่ไฟล์ zip → Extract All (แตกไฟล์ทั้งหมด)\n"
                "แล้วเปิด PdfMergeA4.exe จากโฟลเดอร์ที่แตกออกมา", parent=self.root)
            return
        try:
            if shortcuts.installed():
                if messagebox.askyesno("ลบทางลัด", 'ลบไอคอน "รวม PDF (A4)" บน Desktop\n'
                                                   "และในเมนูคลิกขวา → Send to ออกไหม?",
                                       parent=self.root):
                    shortcuts.remove()
                    messagebox.showinfo("ลบทางลัด", "ลบทางลัดแล้ว", parent=self.root)
            elif messagebox.askyesno(
                    "สร้างทางลัด",
                    'สร้างไอคอน "รวม PDF (A4)"\n\n'
                    "•  บน Desktop — ดับเบิลคลิกเพื่อเปิด หรือลากไฟล์มาวางบนไอคอน\n"
                    "•  ในเมนูคลิกขวาที่ไฟล์ → Send to\n\n"
                    f"ทางลัดจะเปิดโปรแกรมจากโฟลเดอร์นี้:\n{app_dir()}\n\n"
                    "ถ้าย้ายโปรแกรมไปที่อื่นภายหลัง ให้ลบแล้วสร้างทางลัดใหม่", parent=self.root):
                self.root.configure(cursor="watch")
                self.root.update()
                try:
                    shortcuts.install()
                finally:
                    self.root.configure(cursor="")
                messagebox.showinfo(
                    "สร้างทางลัด",
                    "สร้างทางลัดแล้ว\n\n"
                    "คลิกขวาที่ไฟล์ PDF หรือรูป → Send to → รวม PDF (A4)\n"
                    "(Windows 11: คลิกขวา → Show more options ก่อน แล้วจึงเลือก Send to)",
                    parent=self.root)
        except Exception as e:
            messagebox.showerror("ทางลัด", f"ทำไม่สำเร็จ:\n{e}", parent=self.root)
        self.refresh_shortcut_btn()

    # ---------- preview ----------
    def on_select(self):
        sel = self.selected()
        if sel and not self.busy:
            self.status.configure(text=short_path(self.items[sel[0]]["path"]))
        self.update_preview()

    def update_preview(self):
        sel = self.selected()
        if not sel:
            self.preview.configure(image="")
            self.preview_cap.configure(text="")
            return
        it = self.items[sel[0]]
        try:
            img = make_preview(it["path"], it["rot"], self.landscape.get())
            self._photo = ImageTk.PhotoImage(img)
            self.preview.configure(image=self._photo)
            cap = "ตัวอย่างหน้าแรก" if it["pages"] == 1 else f"ตัวอย่างหน้า 1 จาก {it['pages']}"
        except Exception as e:
            self.preview.configure(image="")
            cap = f"แสดงตัวอย่างไม่ได้: {e}"
        self.preview_cap.configure(text=cap)

    # ---------- merge ----------
    def merge(self):
        order = self.order()
        if not order:
            messagebox.showinfo("รวม PDF", "ยังไม่มีไฟล์ในรายการ", parent=self.root)
            return
        first = self.items[order[0]]["path"]
        out = filedialog.asksaveasfilename(
            parent=self.root, title="บันทึกไฟล์ที่รวมแล้ว",
            initialdir=os.path.dirname(first),
            initialfile="รวม_" + os.path.splitext(os.path.basename(first))[0] + ".pdf",
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not out:
            return
        items = [(self.items[i]["path"], self.items[i]["rot"]) for i in order]
        self.busy = True
        self.merge_btn.configure(state="disabled")
        self.root.configure(cursor="watch")
        threading.Thread(target=self._worker, args=(items, out, self.landscape.get()),
                         daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, items, out, landscape):
        def progress(n, total, path):
            self.q.put(("progress", f"กำลังรวม {n}/{total}: {os.path.basename(path)}"))
        try:
            pages = core.merge_to_a4(items, out, landscape, progress)
            self.q.put(("done", pages, out))
        except PermissionError:
            self.q.put(("error", "บันทึกไม่ได้ — ไฟล์ปลายทางอาจเปิดค้างอยู่ในโปรแกรมอื่น\n"
                                 "ปิดไฟล์นั้นแล้วลองใหม่ หรือบันทึกเป็นชื่ออื่น"))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    self.status.configure(text=msg[1])
                    continue
                self.busy = False
                self.merge_btn.configure(state="normal")
                self.root.configure(cursor="")
                self.refresh_rows()
                if msg[0] == "done":
                    _, pages, out = msg
                    self.status.configure(text="บันทึกแล้ว: " + short_path(out))
                    if messagebox.askyesno("รวมเสร็จแล้ว",
                                           f"รวมเสร็จแล้ว {pages} หน้า (A4)\n\n{out}\n\nเปิดไฟล์เลยไหม?",
                                           parent=self.root):
                        os.startfile(out)
                else:
                    messagebox.showerror("รวมไม่สำเร็จ", msg[1], parent=self.root)
                return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def make_root():
    if TkinterDnD:
        try:
            return TkinterDnD.Tk()
        except Exception:
            pass  # tkdnd failed to load; run without drag-and-drop from Explorer
    return tk.Tk()


def main():
    # headless: PdfMergeA4.exe --out result.pdf a.pdf b.jpg ...  (exit code 1 on error)
    if len(sys.argv) > 2 and sys.argv[1] == "--out":
        try:
            files, _ = core.expand_paths(sys.argv[3:])
            core.merge_to_a4([(f, 0) for f in files], sys.argv[2])
        except Exception:
            log_crash(show=False)
            sys.exit(1)
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = make_root()
    # no console window, so surface errors from button handlers too
    root.report_callback_exception = lambda *exc: log_crash()
    App(root, sys.argv[1:])
    root.mainloop()


def log_crash(show=True):
    path = os.path.join(app_dir(), "error.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(traceback.format_exc() + "\n")
    if show:
        try:
            messagebox.showerror("รวม PDF", "เกิดข้อผิดพลาด ดูรายละเอียดใน\n" + path)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_crash()
