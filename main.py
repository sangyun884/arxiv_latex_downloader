import tkinter as tk
from tkinter import messagebox, scrolledtext
from tkinter import ttk
import requests
import tarfile
import tempfile
import os
import re
import shutil

# Define cache directory
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".arxiv_cache")

def ensure_cache_dir():
    """
    Ensures that the main cache directory exists.
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def get_cache_subdir(arxiv_id):
    """
    Returns the cache subdirectory path for a given arXiv ID.
    """
    # Replace '/' in older arXiv IDs to prevent directory issues
    safe_arxiv_id = arxiv_id.replace('/', '_')
    return os.path.join(CACHE_DIR, safe_arxiv_id)

def is_cached(arxiv_id):
    """
    Checks if the tar.gz for the given arXiv ID is already cached.
    """
    cache_subdir = get_cache_subdir(arxiv_id)
    tar_path = os.path.join(cache_subdir, 'source.tar.gz')
    return os.path.exists(tar_path)

def cache_tar(arxiv_id, tar_path):
    """
    Copies the downloaded tar.gz to the cache subdirectory.
    """
    cache_subdir = get_cache_subdir(arxiv_id)
    if not os.path.exists(cache_subdir):
        os.makedirs(cache_subdir)
    cached_tar_path = os.path.join(cache_subdir, 'source.tar.gz')
    shutil.copy(tar_path, cached_tar_path)

def parse_arxiv_id(url):
    """
    Extracts the arXiv ID from a given arXiv URL.
    """
    match = re.search(r'arxiv\.org/(?:abs|pdf)/([a-z\-]+/\d{7}|(\d+\.\d+))', url)
    if match:
        return match.group(1)
    return None

def download_source(arxiv_id, progress_callback=None):
    """
    Downloads the LaTeX source as a tar.gz file from arXiv.
    Implements caching to avoid re-downloading existing sources.
    """
    cache_subdir = get_cache_subdir(arxiv_id)
    tar_path = os.path.join(cache_subdir, 'source.tar.gz')
    
    if is_cached(arxiv_id):
        if progress_callback:
            progress_callback("Using cached source...")
        return tar_path, cache_subdir
    
    url = f'https://arxiv.org/e-print/{arxiv_id}'
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            if progress_callback:
                progress_callback("Downloading source...")
            temp_dir = tempfile.mkdtemp()
            temp_tar_path = os.path.join(temp_dir, 'source.tar.gz')
            with open(temp_tar_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
            # Cache the downloaded tar.gz
            cache_tar(arxiv_id, temp_tar_path)
            shutil.rmtree(temp_dir)  # Clean up temporary directory
            return tar_path, cache_subdir
        else:
            return None, None
    except Exception as e:
        print(f"Error downloading source: {e}")
        return None, None

def extract_tar(tar_path, extract_path, progress_callback=None):
    """
    Extracts the tar.gz file to the specified directory.
    """
    try:
        if progress_callback:
            progress_callback("Extracting files...")
        with tarfile.open(tar_path, 'r:gz') as tar:
            tar.extractall(path=extract_path)
        return True
    except Exception as e:
        print(f"Error extracting tar.gz: {e}")
        return False

def find_main_tex(extract_path):
    """
    Attempts to find the main .tex file in the extracted source using enhanced heuristics.
    """
    tex_files = []
    for root, dirs, files in os.walk(extract_path):
        for file in files:
            if file.endswith('.tex'):
                tex_files.append(os.path.join(root, file))
    if not tex_files:
        return None

    # Heuristic 1: Check for common main file names
    common_names = ['main.tex', 'paper.tex', 'content.tex', 'article.tex', 'thesis.tex']
    for file in tex_files:
        if os.path.basename(file).lower() in common_names:
            return file

    # Heuristic 2: Search for essential LaTeX commands
    candidate_scores = {}
    essential_commands = [
        r'\\documentclass',
        r'\\begin\{document\}',
        r'\\title\{',
        r'\\author\{',
        r'\\maketitle',
        r'\\usepackage'
    ]

    for file in tex_files:
        try:
            with open(file, 'r', encoding='utf-8') as f:
                content = f.read()
            score = 0
            for cmd in essential_commands:
                if re.search(cmd, content):
                    score += 1
            candidate_scores[file] = score
        except Exception as e:
            print(f"Error reading {file}: {e}")
            continue

    if candidate_scores:
        # Select the file with the highest score
        main_tex = max(candidate_scores, key=candidate_scores.get)
        if candidate_scores[main_tex] > 0:
            return main_tex

    # Heuristic 3: Largest .tex file
    tex_files_sorted = sorted(tex_files, key=lambda x: os.path.getsize(x), reverse=True)
    return tex_files_sorted[0]

def inline_tex(file_path, extract_path, included_files=None, depth=0, max_depth=10):
    """
    Recursively inlines \input and \include commands.
    """
    if included_files is None:
        included_files = set()
    if depth > max_depth:
        raise RecursionError("Maximum recursion depth reached while inlining files.")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return f"% Error reading file: {os.path.basename(file_path)}\n"

    # Regex patterns to find \input{...} and \include{...}
    pattern = re.compile(r'\\(?:input|include)\{([^}]+)\}')

    def replace_match(match):
        relative_path = match.group(1)
        # Append .tex if not present
        if not relative_path.endswith('.tex'):
            relative_path += '.tex'
        included_path = os.path.join(os.path.dirname(file_path), relative_path)
        # Normalize the path
        included_path = os.path.normpath(included_path)
        if included_path in included_files:
            return f"% Skipping already included file: {relative_path}\n"
        if not os.path.exists(included_path):
            return f"% File not found: {relative_path}\n"
        included_files.add(included_path)
        try:
            return inline_tex(included_path, extract_path, included_files, depth + 1, max_depth)
        except RecursionError:
            return f"% Recursion limit reached while including: {relative_path}\n"

    # Replace all \input and \include with the actual content
    inlined_content = pattern.sub(replace_match, content)
    return inlined_content

def combine_tex_files(main_tex, extract_path, progress_callback=None):
    """
    Combines multiple .tex files into a single LaTeX source.
    """
    try:
        if progress_callback:
            progress_callback("Combining .tex files...")
        combined = inline_tex(main_tex, extract_path)
        return combined
    except RecursionError as e:
        messagebox.showerror("Error", str(e))
        return None

def process_arxiv_link(arxiv_url, text_widget, progress_bar, status_label):
    """
    Main processing function to handle the arXiv link and display the LaTeX source.
    """
    arxiv_id = parse_arxiv_id(arxiv_url)
    if not arxiv_id:
        messagebox.showerror("Invalid URL", "Please enter a valid arXiv URL.")
        return

    text_widget.delete(1.0, tk.END)
    update_progress(progress_bar, status_label, 0, "Starting process...")

    # Step 1: Download Source
    def step1(status):
        update_status(status_label, status)

    tar_path, extract_path = download_source(arxiv_id, progress_callback=step1)
    if not tar_path:
        messagebox.showerror("Download Failed", "Could not download the LaTeX source. Please check the arXiv ID.")
        update_progress(progress_bar, status_label, 0, "Download failed.")
        return

    # Step 2: Extract Files
    success = extract_tar(tar_path, extract_path, progress_callback=lambda s: update_status(status_label, s))
    if not success:
        messagebox.showerror("Extraction Failed", "Could not extract the LaTeX source.")
        update_progress(progress_bar, status_label, 0, "Extraction failed.")
        return

    # Step 3: Find Main .tex File
    main_tex = find_main_tex(extract_path)
    if not main_tex:
        messagebox.showerror("No .tex File Found", "Could not find any .tex files in the source.")
        update_progress(progress_bar, status_label, 0, "No .tex file found.")
        return

    update_status(status_label, f"Main .tex file found: {os.path.basename(main_tex)}")

    # Step 4: Combine .tex Files
    combined_tex = combine_tex_files(main_tex, extract_path, progress_callback=lambda s: update_status(status_label, s))
    if combined_tex is None:
        # Optionally, cleanup extracted files if needed
        # shutil.rmtree(extract_path, ignore_errors=True)
        update_progress(progress_bar, status_label, 0, "Combining failed.")
        return

    # Step 5: Display Combined LaTeX Source
    text_widget.delete(1.0, tk.END)
    text_widget.insert(tk.END, combined_tex)
    update_progress(progress_bar, status_label, 100, "Completed.")

    # Optional: Remove image files to save space (uncomment if desired)
    # remove_image_files(extract_path)

def remove_image_files(extract_path):
    """
    Removes image files from the extracted source to save space.
    """
    image_extensions = ['.jpg', '.jpeg', '.png', '.pdf', '.gif', '.bmp', '.svg']
    for root, dirs, files in os.walk(extract_path):
        for file in files:
            if any(file.lower().endswith(ext) for ext in image_extensions):
                try:
                    os.remove(os.path.join(root, file))
                except Exception as e:
                    print(f"Error removing file {file}: {e}")

def update_progress(progress_bar, status_label, value, status):
    """
    Updates the progress bar and status label.
    """
    progress_bar['value'] = value
    status_label.config(text=status)
    progress_bar.update()
    status_label.update()

def update_status(status_label, status):
    """
    Updates the status label.
    """
    status_label.config(text=status)
    status_label.update()

def copy_to_clipboard(root, text_widget):
    """
    Copies the content of the text widget to the clipboard.
    """
    try:
        root.clipboard_clear()
        root.clipboard_append(text_widget.get(1.0, tk.END))
        messagebox.showinfo("Copied", "LaTeX source copied to clipboard.")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to copy to clipboard: {e}")

def create_gui():
    """
    Creates the GUI for the application.
    """
    root = tk.Tk()
    root.title("arXiv LaTeX Downloader")

    # Set window size
    root.geometry("900x700")
    root.resizable(True, True)

    # URL input frame using grid layout
    input_frame = tk.Frame(root)
    input_frame.pack(pady=10, padx=10, fill=tk.X)

    url_label = tk.Label(input_frame, text="arXiv URL:")
    url_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

    url_entry = tk.Entry(input_frame, width=80)
    url_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

    download_button = tk.Button(input_frame, text="Download", width=15, 
                                command=lambda: process_arxiv_link(url_entry.get().strip(), text_area, progress_bar, status_label))
    download_button.grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)

    # Configure grid weights to make the entry expand
    input_frame.grid_columnconfigure(1, weight=1)

    # Progress bar and status label
    progress_frame = tk.Frame(root)
    progress_frame.pack(pady=5, padx=10, fill=tk.X)

    progress_bar = ttk.Progressbar(progress_frame, orient='horizontal', mode='determinate', length=400)
    progress_bar.pack(side=tk.LEFT, padx=5, pady=5)

    status_label = tk.Label(progress_frame, text="Ready")
    status_label.pack(side=tk.LEFT, padx=5, pady=5)

    # Text area for LaTeX source
    text_area = scrolledtext.ScrolledText(root, wrap=tk.NONE, font=("Courier", 10))
    text_area.pack(pady=10, padx=10, expand=True, fill=tk.BOTH)

    # Copy button
    copy_button = tk.Button(root, text="Copy to Clipboard", width=20, 
                            command=lambda: copy_to_clipboard(root, text_area))
    copy_button.pack(pady=5)

    return root

def check_requests():
    """
    Checks if the 'requests' library is installed.
    """
    try:
        import requests
    except ImportError:
        messagebox.showerror("Missing Library", 
                             "The 'requests' library is required but not installed.\nPlease install it using 'pip install requests'.")
        return False
    return True

if __name__ == "__main__":
    # Initialize Tkinter root for messagebox
    temp_root = tk.Tk()
    temp_root.withdraw()  # Hide the root window

    if check_requests():
        ensure_cache_dir()
        temp_root.destroy()
        root = create_gui()
        root.mainloop()
    else:
        temp_root.destroy()
