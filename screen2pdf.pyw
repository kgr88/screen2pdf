"""Screen2PDF - capture screenshots and combine them into a single PDF.

Hotkeys (work from anywhere; the app window is hidden during capture so
it never appears in the shot):
  Ctrl+Alt+P  capture the selected screen
  Ctrl+Alt+R  drag to select a portion of the screen
"""

import ctypes
import ctypes.wintypes
import io
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import img2pdf
import mss
from PIL import Image, ImageEnhance, ImageTk
from pypdf import PdfReader, PdfWriter

APP_NAME = "Screen2PDF"
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312
# hotkey id -> (event name, virtual key, label)
HOTKEYS = {
    1: ("capture", 0x50, "Ctrl+Alt+P"),
    2: ("region", 0x52, "Ctrl+Alt+R"),
}

# Capture at real pixel resolution on high-DPI displays and keep the UI sharp.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


OCR_UPSCALE = 2  # screen text is small; Tesseract reads it far better enlarged
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
# In the built .exe, PyInstaller unpacks the bundled Tesseract + tessdata here.
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)

TESSERACT_CANDIDATES = [
    os.path.join(BUNDLE_DIR, "tesseract", "tesseract.exe"),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]
# A tessdata folder next to the app/exe wins (so languages can be added without
# rebuilding); otherwise the one bundled into the exe is used.
TESSDATA_DIRS = [os.path.join(APP_DIR, "tessdata"), os.path.join(BUNDLE_DIR, "tessdata")]


def find_tesseract() -> str | None:
    bundled = TESSERACT_CANDIDATES[0]
    if os.path.isfile(bundled):
        return bundled
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in TESSERACT_CANDIDATES if os.path.isfile(p)), None)


def run_tesseract(exe: str, *args: str) -> subprocess.CompletedProcess:
    tessdata = next((d for d in TESSDATA_DIRS if os.path.isdir(d)), None)
    if tessdata:
        args = ("--tessdata-dir", tessdata, *args)
    return subprocess.run(
        [exe, *args], capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def tesseract_languages(exe: str) -> list[str]:
    out = run_tesseract(exe, "--list-langs").stdout.splitlines()[1:]
    return [lang.strip() for lang in out if lang.strip() and lang.strip() != "osd"]


def build_pdf(shots: list[str], out: str, ocr_exe: str | None, lang: str, progress) -> None:
    """Write shots to one PDF. With OCR, an invisible text layer is laid over each page."""
    base = PdfReader(io.BytesIO(img2pdf.convert(shots)))
    writer = PdfWriter()
    for i, (page, shot) in enumerate(zip(base.pages, shots), 1):
        if ocr_exe:
            progress(f"Recognizing text… page {i}/{len(shots)}")
            page.merge_page(ocr_text_layer(ocr_exe, shot, lang))
        writer.add_page(page)
    progress("Writing PDF…")
    with open(out, "wb") as f:
        writer.write(f)


def ocr_text_layer(exe: str, shot: str, lang: str):
    """OCR an enlarged copy of the shot and return a text-only PDF page of the same size."""
    with tempfile.TemporaryDirectory() as tmp:
        big = os.path.join(tmp, "page.png")
        with Image.open(shot) as img:
            w, h = img.size
            img.convert("L").resize((w * OCR_UPSCALE, h * OCR_UPSCALE), Image.LANCZOS).save(
                big, dpi=(96 * OCR_UPSCALE, 96 * OCR_UPSCALE)  # keeps the page size identical
            )
        stem = os.path.join(tmp, "text")
        result = run_tesseract(exe, big, stem, "-l", lang, "-c", "textonly_pdf=1", "pdf")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Tesseract failed")
        with open(stem + ".pdf", "rb") as f:
            return PdfReader(io.BytesIO(f.read())).pages[0]


def hotkey_listener(events: queue.Queue) -> None:
    """Register global hotkeys and forward presses to the Tk thread via a queue."""
    user32 = ctypes.windll.user32
    for hk_id, (_, vk, label) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hk_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
            events.put(f"hotkey-failed:{label}")
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
            events.put(HOTKEYS[msg.wParam][0])


class RegionSelector:
    """Full-screen overlay over a frozen screenshot; drag a rectangle to select it.

    Returns the selection as (left, top, right, bottom) in image coordinates,
    or None if cancelled with Esc / right-click.
    """

    def __init__(self, root: tk.Tk, image: Image.Image, origin: tuple[int, int]) -> None:
        self.image = image
        self.result = None
        self.start = None
        self.bright = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        w, h = image.size
        self.win.geometry(f"{w}x{h}+{origin[0]}+{origin[1]}")

        self.canvas = tk.Canvas(self.win, width=w, height=h, highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.dim = ImageTk.PhotoImage(ImageEnhance.Brightness(image).enhance(0.45))
        self.canvas.create_image(0, 0, image=self.dim, anchor="nw")
        self.crop_item = self.canvas.create_image(0, 0, anchor="nw")
        self.rect = self.canvas.create_rectangle(0, 0, 0, 0, outline="#3a8dff", width=2, state="hidden")
        self.label = self.canvas.create_text(
            0, 0, anchor="sw", fill="white", font=("Segoe UI", 11, "bold"), state="hidden"
        )
        self.canvas.create_text(
            w // 2, 30, fill="white", font=("Segoe UI", 13),
            text="Drag to select an area  •  Esc or right-click to cancel",
        )

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self.win.destroy())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

        self.win.focus_force()
        self.win.grab_set()

    def box(self, event) -> tuple[int, int, int, int]:
        w, h = self.image.size
        x0, y0 = self.start
        x1, y1 = min(max(event.x, 0), w), min(max(event.y, 0), h)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def on_press(self, event) -> None:
        self.start = (event.x, event.y)

    def on_drag(self, event) -> None:
        if not self.start:
            return
        l, t, r, b = self.box(event)
        self.canvas.coords(self.rect, l, t, r, b)
        self.canvas.itemconfigure(self.rect, state="normal")
        self.canvas.coords(self.label, l + 2, t - 4 if t > 24 else b + 22)
        self.canvas.itemconfigure(self.label, text=f"{r - l} × {b - t}", state="normal")
        if r > l and b > t:
            self.bright = ImageTk.PhotoImage(self.image.crop((l, t, r, b)))
            self.canvas.itemconfigure(self.crop_item, image=self.bright)
            self.canvas.coords(self.crop_item, l, t)
        self.canvas.tag_raise(self.rect)
        self.canvas.tag_raise(self.label)

    def on_release(self, event) -> None:
        if not self.start:
            return
        l, t, r, b = self.box(event)
        if r - l >= 5 and b - t >= 5:
            self.result = (l, t, r, b)
            self.win.destroy()
        else:  # a click without a real drag: start over
            self.start = None
            self.canvas.itemconfigure(self.rect, state="hidden")
            self.canvas.itemconfigure(self.label, state="hidden")
            self.canvas.itemconfigure(self.crop_item, image="")

    def run(self):
        self.win.wait_window()
        return self.result


class Screen2PDF:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.workdir = tempfile.mkdtemp(prefix="screen2pdf_")
        self.shots: list[str] = []  # paths to PNG files, in page order
        self.events: queue.Queue = queue.Queue()
        self.capturing = False
        self.preview_img = None
        self.last_region = None  # mss-style dict in virtual-desktop coordinates

        root.title(APP_NAME)
        root.geometry("720x480")
        root.minsize(560, 360)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.refresh_monitors()

        threading.Thread(target=hotkey_listener, args=(self.events,), daemon=True).start()
        self.root.after(100, self.poll_events)

    # ---------- UI ----------

    def build_ui(self) -> None:
        actions = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        actions.pack(fill="x")
        ttk.Button(
            actions, text=f"📷 Capture screen  ({HOTKEYS[1][2]})", command=self.capture
        ).pack(side="left")
        ttk.Button(
            actions, text=f"✂ Capture area  ({HOTKEYS[2][2]})", command=self.capture_region
        ).pack(side="left", padx=(8, 0))
        self.lock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            actions, text="Reuse last area", variable=self.lock_var
        ).pack(side="left", padx=(12, 0))

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Screen:").pack(side="left", padx=(0, 4))
        self.monitor_var = tk.StringVar()
        self.monitor_box = ttk.Combobox(top, textvariable=self.monitor_var, state="readonly", width=26)
        self.monitor_box.pack(side="left")

        ttk.Label(top, text="Delay (s):").pack(side="left", padx=(12, 4))
        self.delay_var = tk.IntVar(value=0)
        ttk.Spinbox(top, from_=0, to=30, textvariable=self.delay_var, width=4).pack(side="left")

        self.hide_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Hide window", variable=self.hide_var).pack(side="left", padx=(12, 0))

        body = ttk.Frame(self.root, padding=(8, 0))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=28, activestyle="none", exportselection=False)
        self.listbox.pack(side="top", fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.show_preview())
        self.listbox.bind("<Delete>", lambda e: self.delete_selected())

        btns = ttk.Frame(left)
        btns.pack(side="top", fill="x", pady=4)
        ttk.Button(btns, text="▲", width=3, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(btns, text="▼", width=3, command=lambda: self.move(1)).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear all", command=self.clear_all).pack(side="left")

        self.preview = ttk.Label(body, anchor="center", relief="sunken")
        self.preview.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.preview.bind("<Configure>", lambda e: self.show_preview())

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        self.status_var = tk.StringVar(value="No screenshots yet.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        self.save_btn = ttk.Button(bottom, text="💾 Save as PDF…", command=self.save_pdf)
        self.save_btn.pack(side="right")

        self.tesseract = find_tesseract()
        langs = tesseract_languages(self.tesseract) if self.tesseract else []
        if "pol" in langs and "eng" in langs:
            langs = ["pol+eng", *langs]  # mixed Polish/English text reads best with both
        self.lang_var = tk.StringVar(value=langs[0] if langs else "")
        lang_box = ttk.Combobox(bottom, textvariable=self.lang_var, values=langs, width=10)
        lang_box.pack(side="right", padx=(4, 12))
        self.ocr_var = tk.BooleanVar(value=bool(self.tesseract))
        ocr_check = ttk.Checkbutton(bottom, text="Selectable text (OCR)", variable=self.ocr_var)
        ocr_check.pack(side="right")
        if not self.tesseract:
            ocr_check.configure(state="disabled", text="Selectable text (install Tesseract OCR)")
            lang_box.configure(state="disabled")

    def refresh_monitors(self) -> None:
        with mss.MSS() as sct:
            mons = sct.monitors
        options = [f"All screens ({mons[0]['width']}x{mons[0]['height']})"]
        options += [f"Screen {i} ({m['width']}x{m['height']})" for i, m in enumerate(mons[1:], 1)]
        self.monitor_box["values"] = options
        self.monitor_box.current(1 if len(mons) > 1 else 0)

    def update_status(self, extra: str = "") -> None:
        n = len(self.shots)
        text = f"{n} screenshot{'s' if n != 1 else ''} → {n} PDF page{'s' if n != 1 else ''}"
        self.status_var.set(f"{text}   {extra}".rstrip())

    def refresh_list(self, select: int | None = None) -> None:
        self.listbox.delete(0, "end")
        for i, path in enumerate(self.shots, 1):
            self.listbox.insert("end", f"{i:>3}.  {os.path.basename(path)[:-4]}")
        if select is not None and self.shots:
            select = max(0, min(select, len(self.shots) - 1))
            self.listbox.selection_set(select)
            self.listbox.see(select)
        self.show_preview()
        self.update_status()

    def selected_index(self) -> int | None:
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def show_preview(self) -> None:
        idx = self.selected_index()
        if idx is None:
            self.preview.configure(image="", text="Select a screenshot to preview")
            return
        w, h = max(self.preview.winfo_width() - 8, 50), max(self.preview.winfo_height() - 8, 50)
        with Image.open(self.shots[idx]) as img:
            img.thumbnail((w, h))
            self.preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self.preview_img, text="")

    # ---------- Actions ----------

    def poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "capture":
                    self.capture()
                elif ev == "region":
                    self.capture_region()
                elif ev.startswith("hotkey-failed:"):
                    label = ev.split(":", 1)[1]
                    self.update_status(f"(hotkey {label} unavailable - in use by another app)")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def capture(self) -> None:
        self.start_capture(self.grab_screen)

    def capture_region(self) -> None:
        if self.lock_var.get() and self.last_region:
            self.start_capture(self.grab_last_region)
        else:
            self.start_capture(self.grab_selected_region)

    def start_capture(self, grabber) -> None:
        if self.capturing:
            return
        self.capturing = True
        delay_ms = max(0, int(self.delay_var.get() or 0)) * 1000
        if self.hide_var.get():
            self.root.withdraw()
            delay_ms += 300  # let the window finish disappearing
        self.root.after(delay_ms, lambda: self.do_capture(grabber))

    @staticmethod
    def grab(area: dict) -> Image.Image:
        with mss.MSS() as sct:
            shot = sct.grab(area)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def grab_screen(self) -> Image.Image:
        with mss.MSS() as sct:
            area = sct.monitors[self.monitor_box.current()]
        return self.grab(area)

    def grab_last_region(self) -> Image.Image:
        return self.grab(self.last_region)

    def grab_selected_region(self) -> Image.Image | None:
        """Freeze the whole desktop, let the user drag a rectangle, crop to it."""
        with mss.MSS() as sct:
            desktop = sct.monitors[0]
        full = self.grab(desktop)
        box = RegionSelector(self.root, full, (desktop["left"], desktop["top"])).run()
        if not box:
            return None
        l, t, r, b = box
        self.last_region = {
            "left": desktop["left"] + l, "top": desktop["top"] + t,
            "width": r - l, "height": b - t,
        }
        return full.crop(box)

    def do_capture(self, grabber) -> None:
        added = False
        try:
            img = grabber()
            if img is not None:
                name = datetime.now().strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
                path = os.path.join(self.workdir, f"{name}.png")
                img.save(path, "PNG", dpi=(96, 96))
                self.shots.append(path)
                added = True
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Capture failed:\n{exc}")
        finally:
            self.capturing = False
            if self.hide_var.get():
                self.root.deiconify()
            if added:
                self.refresh_list(select=len(self.shots) - 1)
                self.root.bell()

    def move(self, step: int) -> None:
        idx = self.selected_index()
        if idx is None:
            return
        new = idx + step
        if 0 <= new < len(self.shots):
            self.shots[idx], self.shots[new] = self.shots[new], self.shots[idx]
            self.refresh_list(select=new)

    def delete_selected(self) -> None:
        idx = self.selected_index()
        if idx is None:
            return
        path = self.shots.pop(idx)
        try:
            os.remove(path)
        except OSError:
            pass
        self.refresh_list(select=idx)

    def clear_all(self, confirm: bool = True) -> None:
        if not self.shots:
            return
        if confirm and not messagebox.askyesno(APP_NAME, f"Remove all {len(self.shots)} screenshots?"):
            return
        for path in self.shots:
            try:
                os.remove(path)
            except OSError:
                pass
        self.shots.clear()
        self.refresh_list()

    def save_pdf(self) -> None:
        if not self.shots:
            messagebox.showinfo(APP_NAME, "Take at least one screenshot first.")
            return
        default = datetime.now().strftime("Screenshots %Y-%m-%d %H-%M.pdf")
        out = filedialog.asksaveasfilename(
            title="Save PDF",
            defaultextension=".pdf",
            initialfile=default,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not out:
            return
        ocr_exe = self.tesseract if self.ocr_var.get() else None
        lang = self.lang_var.get().strip() or "eng"
        shots = list(self.shots)
        self.save_btn.configure(state="disabled")

        def progress(text: str) -> None:
            self.root.after(0, self.status_var.set, text)

        def work() -> None:
            # OCR takes a second or two per page, so keep it off the UI thread.
            try:
                build_pdf(shots, out, ocr_exe, lang, progress)
                self.root.after(0, self.after_save, out, None)
            except Exception as exc:
                self.root.after(0, self.after_save, out, exc)

        threading.Thread(target=work, daemon=True).start()

    def after_save(self, out: str, error: Exception | None) -> None:
        self.save_btn.configure(state="normal")
        if error:
            self.update_status()
            messagebox.showerror(APP_NAME, f"Could not save PDF:\n{error}")
            return
        if messagebox.askyesno(APP_NAME, f"Saved {len(self.shots)} pages to:\n{out}\n\nOpen it now?"):
            os.startfile(out)
        if messagebox.askyesno(APP_NAME, "Clear the screenshot list to start a new PDF?"):
            self.clear_all(confirm=False)
        else:
            self.update_status("✔ saved")

    def on_close(self) -> None:
        if self.shots and not messagebox.askyesno(
            APP_NAME, f"You have {len(self.shots)} unsaved screenshots. Quit anyway?"
        ):
            return
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    Screen2PDF(root)
    root.mainloop()


if __name__ == "__main__":
    main()
