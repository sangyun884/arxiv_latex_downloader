# main.py – arXiv LaTeX Combiner (GUI-only, improved heuristics)
# ============================================================================
# Paste an arXiv URL → the whole paper’s LaTeX appears in one box (and is
# auto-copied to your clipboard).  Works well on ML papers (NeurIPS/ICLR/ACL…).
#
# Requirements: requests, tkinter (present on the standard macOS/Windows build;
# on Linux install tk-dev & rebuild, or `sudo apt install python3-tk`).
# ============================================================================

from __future__ import annotations

import logging
import queue
import re
import shutil
import tarfile
import tempfile
import threading
import typing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Set

import requests
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

# --------------------------- Configuration -----------------------------------
LOG_LEVEL = logging.INFO
CHUNK_SIZE = 8192
MAX_INLINE_DEPTH = 25
CACHE_DIR = Path.home() / ".arxiv_downloader_cache"
CACHE_DIR = Path(
    typing.cast(str, (Path.home() / ".arxiv_downloader_cache"))
)
MAX_CACHE_MB = 2048  # 2 GiB

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# --------------------------- Cache helpers -----------------------------------


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_cache_subdir(arxiv_id: str) -> Path:
    safe = re.sub(r"[\\/*?:\"<>|]", "_", arxiv_id)
    return CACHE_DIR / safe


def enforce_cache_quota() -> None:
    total = sum(p.stat().st_size for p in CACHE_DIR.rglob("*")) / 1_048_576
    if total <= MAX_CACHE_MB:
        return
    logger.info("Cache %.0f MB > %.0f MB – pruning…", total, MAX_CACHE_MB)
    for d in sorted(CACHE_DIR.iterdir(), key=lambda x: x.stat().st_mtime):
        shutil.rmtree(d, ignore_errors=True)
        total = sum(p.stat().st_size for p in CACHE_DIR.rglob("*")) / 1_048_576
        if total <= MAX_CACHE_MB:
            break


def cache_tar(arxiv_id: str, tmp_tar: Path) -> Path | None:
    dest_dir = get_cache_subdir(arxiv_id)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cached = dest_dir / "source.tar.gz"
        shutil.copy(tmp_tar, cached)
        enforce_cache_quota()
        return cached
    except Exception as e:  # pragma: no cover
        logger.warning("Could not cache tar: %s", e)
        return None


# --------------------------- arXiv ID utils ----------------------------------


def parse_arxiv_id(s: str) -> str | None:
    if re.fullmatch(r"[\w.\-]+", s):
        return s
    m = re.search(r"arxiv\.org/(?:abs|pdf|e-print)/([\w.\-]+)", s)
    return m.group(1) if m else None


# --------------------------- Secure tar extract ------------------------------


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest.resolve())):
            raise RuntimeError(f"Blocked path-traversal: {m.name}")
    tar.extractall(dest)


# --------------------------- Download source ---------------------------------
ProgressCb = Callable[[str, Optional[int]], None]


def download_source(arxiv_id: str, cb: ProgressCb | None = None) -> Path | None:
    cached = get_cache_subdir(arxiv_id) / "source.tar.gz"
    if cached.exists():
        if cb:
            cb("Using cached archive", 100)
        return cached

    url = f"https://arxiv.org/e-print/{arxiv_id}"
    if cb:
        cb("Connecting…", 0)

    try:
        with tempfile.TemporaryDirectory() as tdir:
            tmp_tar = Path(tdir) / "src.tar.gz"
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                with open(tmp_tar, "wb") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if cb and total:
                                cb(
                                    f"Downloading {done//1024}K / {total//1024}K",
                                    int(done * 100 / total),
                                )
            if cb:
                cb("Caching…", 100)
            return cache_tar(arxiv_id, tmp_tar)
    except requests.RequestException as e:
        if cb:
            cb(f"Download error: {e}", 0)
    return None


# --------------------------- Extract helpers ---------------------------------


def extract_tar(tar_path: Path, dest: Path, cb: ProgressCb | None = None) -> bool:
    try:
        if cb:
            cb("Extracting…", None)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            safe_extract(tar, dest)
        if cb:
            cb("Extraction complete", 100)
        return True
    except (tarfile.TarError, RuntimeError) as e:
        if cb:
            cb(f"Extraction error: {e}", 0)
        return False


# --------------------------- Main-file heuristic -----------------------------
FILENAMES = {
    # Common
    "main.tex",
    "paper.tex",
    "article.tex",
    "report.tex",
    "thesis.tex",
    "ms.tex",
    # ML-/CV-conf templates
    *(f"neurips{y}.tex" for y in range(2022, 2026)),
    *(f"iclr{y}_conference.tex" for y in range(2022, 2026)),
    *(f"uai{y}.tex" for y in range(2022, 2026)),
    *(f"icml{y}.tex" for y in range(2022, 2026)),
    *(f"cvpr{y}.tex" for y in range(2022, 2026)),
    *(f"eccv{y}.tex" for y in range(2022, 2026)),
    *(f"acl{y}.tex" for y in range(2022, 2026)),
    *(f"emnlp{y}.tex" for y in range(2022, 2026)),
    *(f"aaai{y}.tex" for y in range(2022, 2026)),
}

SECTION_RE = re.compile(r"\\section\{[A-Za-z]{1,30}")


def list_tex_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.tex") if p.is_file()]


def score_tex(path: Path, root: Path) -> int:
    try:
        txt = path.read_text("utf-8", errors="ignore")[:5000]
    except Exception:
        return 0
    txt = "\n".join(
        re.sub(r"(?<!\\)%.*", "", l)
        for l in txt.splitlines()
        if not l.strip().startswith("%")
    )
    score = 0
    if "\\documentclass" in txt:
        score += 6
    if "\\begin{document}" in txt:
        score += 4
    if "\\begin{abstract}" in txt:
        score += 3
    if SECTION_RE.search(txt):
        score += 2
    if path.name.lower() in FILENAMES:
        score += 4
    if any(seg in {"supplementary", "supp", "appendix"} for seg in path.parts):
        score -= 3
    score -= len(path.relative_to(root).parents)
    return score


def find_main_tex(root: Path) -> Path | None:
    tex_files = list_tex_files(root)
    if not tex_files:
        return None
    ranked = sorted(
        tex_files,
        key=lambda p: (-score_tex(p, root), len(p.name), p.stat().st_size),
    )
    best = ranked[0]
    return best if score_tex(best, root) > 0 else None


# --------------------------- Inlining include/input --------------------------
INCLUDE_RE = re.compile(r"^\s*\\(include|input)\s*\{([^}]+)\}")


def inline_tex(
    path: Path, base: Path, seen: Set[Path] | None = None, depth: int = 0
) -> str:
    if depth > MAX_INLINE_DEPTH:
        return f"% Max depth reached at {path}\n"
    if seen is None:
        seen = set()
    if path in seen:
        return f"% Circular include {path}\n"
    seen.add(path)

    try:
        raw = path.read_text("utf-8", errors="ignore")
    except Exception as e:
        return f"% Error reading {path}: {e}\n"

    out_lines: list[str] = []
    for line in raw.splitlines():
        m = INCLUDE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue

        rel = m.group(2).strip()
        cand = (path.parent / rel).expanduser()

        # add .tex if no suffix
        if cand.suffix == "":
            cand_with_ext = cand.with_suffix(".tex")
            if cand_with_ext.exists():
                cand = cand_with_ext
        if not cand.exists():
            out_lines.append(f"% WARNING: include not found: {rel}")
            continue

        sub_content = inline_tex(cand.resolve(), base, seen.copy(), depth + 1)
        relname = cand.relative_to(base)
        out_lines.append(f"% --- Begin include {relname} ---")
        out_lines.append(sub_content)
        out_lines.append(f"% --- End include {relname} ---")

    return "\n".join(out_lines)


def combine_tex_files(main_path: Path, cb: ProgressCb | None = None) -> str | None:
    if cb:
        cb("Combining files…", None)
    combined = inline_tex(main_path, main_path.parent)
    header = (
        f"% Combined LaTeX generated by arXiv Combiner – "
        f"{datetime.utcnow():%Y-%m-%d %H:%M UTC}\n"
        f"% Source main file: {main_path.name}\n\n"
    )
    if cb:
        cb("Combine done", 100)
    return header + combined


# --------------------------- Threaded worker ---------------------------------
results_queue: queue.Queue = queue.Queue()


def worker_thread_func(arxiv_url: str) -> None:
    def cb(msg: str, pct: Optional[int]) -> None:
        results_queue.put(("status", pct, msg))

    arxiv_id = parse_arxiv_id(arxiv_url)
    if not arxiv_id:
        results_queue.put(
            ("error", "Invalid URL", "Could not parse arXiv ID from the URL.")
        )
        return

    cb(f"Processing {arxiv_id}", 0)
    tarball = download_source(arxiv_id, cb)
    if not tarball:
        results_queue.put(("error", "Download failed", "Could not fetch source."))
        return

    with tempfile.TemporaryDirectory(prefix=f"arxiv_{arxiv_id}_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        if not extract_tar(tarball, tmpdir, cb):
            results_queue.put(
                ("error", "Extraction failed", "Could not unpack source archive.")
            )
            return

        cb("Locating main .tex…", None)
        main_tex = find_main_tex(tmpdir)
        if not main_tex:
            results_queue.put(
                ("error", "Main .tex not found", "Heuristic could not pick entry."))
            return

        cb(f"Main file: {main_tex.relative_to(tmpdir)}", None)
        combined = combine_tex_files(main_tex, cb)
        if not combined:
            results_queue.put(("error", "Combine failed", "Unexpected error."))
            return

        results_queue.put(("result", combined))


# --------------------------- GUI utilities -----------------------------------


def update_progress(
    pbar: ttk.Progressbar, label: tk.Label, value: Optional[int], text: str
) -> None:
    if value is None:
        if pbar["mode"] != "indeterminate":
            pbar["mode"] = "indeterminate"
            pbar.start(10)
    else:
        if pbar["mode"] == "indeterminate":
            pbar.stop()
            pbar["mode"] = "determinate"
        pbar["value"] = max(0, min(100, value))
    label.config(text=text)
    pbar.update_idletasks()
    label.update_idletasks()


def copy_to_clipboard(root: tk.Tk, textw: scrolledtext.ScrolledText) -> None:
    txt = textw.get("1.0", tk.END)
    if not txt.strip():
        messagebox.showwarning("Clipboard", "Nothing to copy.")
        return
    root.clipboard_clear()
    root.clipboard_append(txt)
    messagebox.showinfo("Clipboard", "LaTeX copied to clipboard.")


def clear_cache() -> None:
    if not CACHE_DIR.exists():
        messagebox.showinfo("Cache", "Cache is already empty.")
        return
    if messagebox.askyesno(
        "Confirm clear cache",
        f"Delete all cached archives under {CACHE_DIR}?",
        icon="warning",
    ):
        shutil.rmtree(CACHE_DIR)
        ensure_cache_dir()
        messagebox.showinfo("Cache", "Cache cleared.")


def setup_placeholder(entry: tk.Entry, placeholder: str) -> None:
    entry.insert(0, placeholder)
    entry.config(fg="grey")

    def on_focusin(_):
        if entry.cget("fg") == "grey":
            entry.delete(0, tk.END)
            entry.config(fg="black")

    def on_focusout(_):
        if not entry.get().strip():
            entry.config(fg="grey")
            entry.delete(0, tk.END)
            entry.insert(0, placeholder)

    entry.bind("<FocusIn>", on_focusin, add="+")
    entry.bind("<FocusOut>", on_focusout, add="+")


# --------------------------- GUI creation ------------------------------------


def create_gui() -> tk.Tk:
    root = tk.Tk()
    root.title("arXiv LaTeX Combiner")
    root.geometry("900x700")
    root.minsize(600, 400)

    # Top frame
    inp_frame = tk.Frame(root)
    inp_frame.pack(fill=tk.X, side=tk.TOP, padx=10, pady=10)

    tk.Label(inp_frame, text="arXiv URL:").pack(side=tk.LEFT)
    url_entry = tk.Entry(inp_frame)
    url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
    setup_placeholder(
        url_entry,
        "https://arxiv.org/abs/2303.12345   (or just 2303.12345)",
    )
    download_btn = tk.Button(inp_frame, text="Get LaTeX", width=15)
    download_btn.pack(side=tk.LEFT, padx=5)

    # Middle – progress
    prog_frame = tk.Frame(root)
    prog_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=5)
    pbar = ttk.Progressbar(
        prog_frame, orient="horizontal", mode="determinate", length=300
    )
    pbar.pack(side=tk.LEFT, padx=(0, 5))
    status_lbl = tk.Label(prog_frame, text="Ready", anchor=tk.W)
    status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # Bottom actions
    act_frame = tk.Frame(root)
    act_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=(5, 10))
    copy_btn = tk.Button(
        act_frame,
        text="Copy to Clipboard",
        width=18,
    )
    copy_btn.pack(side=tk.LEFT, padx=(0, 10))

    clear_cache_btn = tk.Button(
        act_frame, text="Clear Cache", width=12, command=clear_cache
    )
    clear_cache_btn.pack(side=tk.RIGHT)

    # Text area
    text_frame = tk.Frame(root)
    text_frame.pack(expand=True, fill=tk.BOTH, side=tk.TOP, padx=10, pady=(0, 5))
    text_area = scrolledtext.ScrolledText(
        text_frame,
        wrap=tk.NONE,
        font=("Courier New", 10),
        undo=True,
        relief=tk.SUNKEN,
        borderwidth=1,
    )
    text_area.pack(expand=True, fill=tk.BOTH)

    # Button commands ---------------------------------------------------------

    def process_threaded() -> None:
        url = url_entry.get().strip()
        if not url or url_entry.cget("fg") == "grey":
            messagebox.showwarning("Input", "Please enter an arXiv URL or ID.")
            return
        download_btn.config(state=tk.DISABLED)
        url_entry.config(state=tk.DISABLED)
        text_area.delete("1.0", tk.END)
        update_progress(pbar, status_lbl, 0, "Starting…")

        # clear queue
        while not results_queue.empty():
            try:
                results_queue.get_nowait()
            except queue.Empty:
                break

        t = threading.Thread(target=worker_thread_func, args=(url,), daemon=True)
        t.start()
        root.after(
            100,
            check_queue,
            root,
            text_area,
            pbar,
            status_lbl,
            download_btn,
            url_entry,
            copy_btn,
        )

    download_btn.config(command=process_threaded)
    copy_btn.config(command=lambda: copy_to_clipboard(root, text_area))

    root.bind("<Return>", lambda e: process_threaded())

    return root


# --------------------------- Queue polling -----------------------------------


def check_queue(
    root: tk.Tk,
    textw: scrolledtext.ScrolledText,
    pbar: ttk.Progressbar,
    label: tk.Label,
    dl_btn: tk.Button,
    url_entry: tk.Entry,
    copy_btn: tk.Button,
) -> None:
    try:
        while True:
            msg = results_queue.get_nowait()
            kind = msg[0]
            if kind == "status":
                _, pct, text = msg
                update_progress(pbar, label, pct, text)
            elif kind == "result":
                _, content = msg
                textw.insert(tk.END, content)
                copy_to_clipboard(root, textw)
                update_progress(pbar, label, 100, "Done (copied to clipboard)")
                dl_btn.config(state=tk.NORMAL)
                url_entry.config(state=tk.NORMAL)
                copy_btn.config(state=tk.NORMAL)
            elif kind == "error":
                _, title, detail = msg
                messagebox.showerror(title, detail)
                update_progress(pbar, label, 0, "Failed.")
                dl_btn.config(state=tk.NORMAL)
                url_entry.config(state=tk.NORMAL)
    except queue.Empty:
        if dl_btn["state"] == tk.DISABLED:
            root.after(
                100,
                check_queue,
                root,
                textw,
                pbar,
                label,
                dl_btn,
                url_entry,
                copy_btn,
            )


# --------------------------- Main -------------------------------------------

if __name__ == "__main__":
    ensure_cache_dir()
    gui_root = create_gui()
    gui_root.mainloop()
