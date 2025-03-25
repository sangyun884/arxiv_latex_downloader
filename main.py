import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import requests
import tarfile
import tempfile
import os
import re
import shutil
import threading
import queue
import typing
from contextlib import contextmanager
import logging

# --- Constants and Cache Setup ---

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".arxiv_downloader_cache")

def ensure_cache_dir() -> None:
    """Ensures that the main cache directory exists."""
    if not os.path.exists(CACHE_DIR):
        try:
            os.makedirs(CACHE_DIR)
        except OSError as e:
            messagebox.showerror("Cache Error", f"Could not create cache directory:\n{CACHE_DIR}\nError: {e}")
            # Decide if the app should exit or try to run without cache
            # For simplicity, we'll let it try, but caching will fail.

def get_cache_subdir(arxiv_id: str) -> str:
    """Returns the cache subdirectory path for a given arXiv ID."""
    # Replace characters unsafe for filenames/paths
    safe_arxiv_id = re.sub(r'[\\/*?:"<>|]', '_', arxiv_id)
    return os.path.join(CACHE_DIR, safe_arxiv_id)

def is_cached(arxiv_id: str) -> bool:
    """Checks if the tar.gz for the given arXiv ID is already cached."""
    cache_subdir = get_cache_subdir(arxiv_id)
    tar_path = os.path.join(cache_subdir, 'source.tar.gz')
    return os.path.exists(tar_path)

def cache_tar(arxiv_id: str, temp_tar_path: str) -> typing.Optional[str]:
    """Copies the downloaded tar.gz to the cache subdirectory. Returns cached path or None."""
    cache_subdir = get_cache_subdir(arxiv_id)
    if not os.path.exists(cache_subdir):
        try:
            os.makedirs(cache_subdir)
        except OSError as e:
            print(f"Warning: Could not create cache subdirectory {cache_subdir}: {e}")
            return None # Indicate caching failed
    cached_tar_path = os.path.join(cache_subdir, 'source.tar.gz')
    try:
        shutil.copy(temp_tar_path, cached_tar_path)
        return cached_tar_path
    except Exception as e:
        print(f"Warning: Could not copy tar to cache {cached_tar_path}: {e}")
        return None # Indicate caching failed

# --- Parsing and Downloading ---

def parse_arxiv_id(url: str) -> typing.Optional[str]:
    """
    Extracts the arXiv ID from a given arXiv URL.
    Handles different URL formats (abs, pdf, e-print).
    """
    # More robust regex covering various formats including version numbers
    patterns = [
        r'arxiv\.org/(?:abs|pdf|e-print)/([\w.-]+?v?\d*)(?:\.pdf)?$', # Matches ID at the end
        r'arxiv\.org/(?:abs|pdf|e-print)/(\d{4}\.\d{4,5}(?:v\d+)?)', # New format ID
        r'arxiv\.org/(?:abs|pdf|e-print)/([a-zA-Z-]+/\d{7}(?:v\d+)?)' # Old format ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# Type alias for the progress callback function
ProgressCallback = typing.Callable[[str, typing.Optional[int]], None]

def download_source(arxiv_id: str, progress_callback: typing.Optional[ProgressCallback] = None) -> typing.Optional[str]:
    """
    Downloads the LaTeX source as a tar.gz file from arXiv.
    Returns the path to the *cached* tar.gz file, or None on failure.
    Implements caching to avoid re-downloading.
    """
    cache_subdir = get_cache_subdir(arxiv_id)
    cached_tar_path = os.path.join(cache_subdir, 'source.tar.gz')

    if is_cached(arxiv_id):
        if progress_callback:
            progress_callback("Using cached source...", 100)
        return cached_tar_path

    url = f'https://arxiv.org/e-print/{arxiv_id}'
    try:
        # Use a temporary directory for downloading
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_tar_path = os.path.join(temp_dir, 'source.tar.gz')
            if progress_callback: progress_callback("Connecting...", 0)

            response = requests.get(url, stream=True, timeout=30) # Added timeout
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            total_size = response.headers.get('content-length')
            downloaded_size = 0

            if progress_callback: progress_callback("Downloading source...", 0)

            with open(temp_tar_path, 'wb') as f:
                if total_size is None: # No content length header
                    # Download without progress percentage
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if progress_callback:
                                progress_callback(f"Downloading... {downloaded_size // 1024}K", None) # Indeterminate
                else:
                    # Download with progress percentage
                    total_size = int(total_size)
                    chunk_size = 8192
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            percentage = int(100 * downloaded_size / total_size) if total_size > 0 else 0
                            if progress_callback:
                                progress_callback(f"Downloading... {downloaded_size // 1024}K / {total_size // 1024}K", percentage)

            if progress_callback: progress_callback("Download complete. Caching...", 100)

            # Cache the downloaded tar.gz
            final_cached_path = cache_tar(arxiv_id, temp_tar_path)
            if final_cached_path:
                 if progress_callback: progress_callback("Source cached.", 100)
                 return final_cached_path
            else:
                 if progress_callback: progress_callback("Caching failed.", 100)
                 # If caching fails, maybe return the temp path? Or signal failure.
                 # Returning None is safer if cache is expected.
                 return None

    except requests.exceptions.Timeout:
        if progress_callback: progress_callback("Download timed out.", 0)
        print("Error downloading source: Timeout")
        return None
    except requests.exceptions.RequestException as e:
        if progress_callback: progress_callback(f"Download error: {e}", 0)
        print(f"Error downloading source: {e}")
        return None
    except Exception as e: # Catch other potential errors like disk full during write
        if progress_callback: progress_callback(f"Error during download: {e}", 0)
        print(f"Error during download process: {e}")
        return None

# --- Extraction and Processing ---

def extract_tar(tar_path: str, extract_path: str, progress_callback: typing.Optional[ProgressCallback] = None) -> bool:
    """Extracts the tar.gz file to the specified directory."""
    try:
        if progress_callback:
            progress_callback("Extracting files...", None) # Use None for indeterminate progress
        with tarfile.open(tar_path, 'r:gz') as tar:
            # Make sure target directory exists before extraction
            os.makedirs(extract_path, exist_ok=True)
            tar.extractall(path=extract_path)
        if progress_callback:
            progress_callback("Extraction complete.", 100) # Signal completion
        return True
    except tarfile.TarError as e:
        print(f"Error extracting tar.gz: {e}")
        if progress_callback: progress_callback(f"Extraction Error: {e}", 0)
        return False
    except Exception as e:
        print(f"Unexpected error during extraction: {e}")
        if progress_callback: progress_callback(f"Extraction Error: {e}", 0)
        return False

def find_main_tex(extract_path: str) -> typing.Optional[str]:
    """
    Attempts to find the main .tex file in the extracted source using enhanced heuristics.
    Returns the full path to the main .tex file or None.
    """
    tex_files: typing.List[str] = []
    for root, _, files in os.walk(extract_path):
        for file in files:
            if file.lower().endswith('.tex'):
                tex_files.append(os.path.join(root, file))

    if not tex_files:
        return None

    # Heuristic 1: Check for common main file names (case-insensitive)
    common_names = ['main.tex', 'paper.tex', 'article.tex', 'report.tex', 'thesis.tex', 'ms.tex']
    for file_path in tex_files:
        if os.path.basename(file_path).lower() in common_names:
            # Quick check if it also contains \documentclass before returning immediately
            try:
                 content_check = ""
                 try:
                     with open(file_path, 'r', encoding='utf-8') as f: content_check = f.read(1000)
                 except UnicodeDecodeError:
                     with open(file_path, 'r', encoding='latin-1') as f: content_check = f.read(1000)
                 if re.search(r'\\documentclass', content_check, re.IGNORECASE):
                    return file_path
            except Exception: pass # Ignore errors in this quick check

    # Heuristic 2: Search for essential LaTeX commands, prioritizing \documentclass
    candidate_scores: typing.Dict[str, int] = {}
    essential_commands = [
        (r'\\documentclass', 5), # High weight
        (r'\\begin\{document\}', 3),
        (r'\\title', 1),
        (r'\\author', 1),
        (r'\\abstract', 1),
        (r'\\maketitle', 1),
        (r'\\bibliography', 1), # Indicates main structure
        (r'\\end\{document\}', 1),
    ]

    for file_path in tex_files:
        score = 0
        content = ""
        try:
            # Try reading with UTF-8, fallback to Latin-1
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read(5000) # Read only beginning for performance
            except UnicodeDecodeError:
                with open(file_path, 'r', encoding='latin-1') as f:
                    content = f.read(5000)

            # Simple comment removal (basic) - important before searching commands
            content_no_comments = ""
            for line in content.splitlines():
                stripped_line = line.strip()
                if not stripped_line.startswith('%'):
                    # More robust comment removal needed for % not at start
                    line_content = re.sub(r'(?<!\\)%.*', '', line) # Remove comments unless escaped \%
                    content_no_comments += line_content + "\n"

            for cmd_pattern, weight in essential_commands:
                if re.search(cmd_pattern, content_no_comments, re.IGNORECASE):
                    score += weight

            # Bonus for being in the root directory
            if os.path.dirname(file_path) == extract_path:
                 score += 2

            candidate_scores[file_path] = score

        except Exception as e:
            print(f"Warning: Error reading or scoring {file_path}: {e}")
            candidate_scores[file_path] = 0 # Penalize files that cause errors
            continue

    if candidate_scores:
        # Select the file with the highest score, break ties with shortest name (less likely to be include)
        sorted_candidates = sorted(candidate_scores.items(), key=lambda item: (-item[1], len(os.path.basename(item[0]))))
        if not sorted_candidates: # Should not happen if candidate_scores is populated
             return None

        best_candidate, best_score = sorted_candidates[0]
        if best_score > 0: # Require at least some evidence
             # Check if the top candidate actually contains \documentclass (redundant check removed, done during scoring)
             return best_candidate

    # Heuristic 3: If scoring failed or yielded no good candidates, fallback to largest .tex file in root?
    # Let's stick to the highest score found if > 0, otherwise maybe the largest overall.
    # Check again if best score was > 0
    if candidate_scores and max(candidate_scores.values()) > 0:
         best_candidate = max(candidate_scores, key=candidate_scores.get)
         return best_candidate


    # Final fallback: largest .tex file overall IF NO SCORED CANDIDATE WAS FOUND
    if tex_files:
        # Try largest file in root first if available
        root_tex_files = [f for f in tex_files if os.path.dirname(f) == extract_path]
        if root_tex_files:
            root_tex_files_sorted = sorted(root_tex_files, key=lambda x: os.path.getsize(x), reverse=True)
            # Check if the largest root file has documentclass before returning it
            try:
                largest_root_file = root_tex_files_sorted[0]
                content_check = ""
                try:
                    with open(largest_root_file, 'r', encoding='utf-8') as f: content_check = f.read(1000)
                except UnicodeDecodeError:
                    with open(largest_root_file, 'r', encoding='latin-1') as f: content_check = f.read(1000)
                if re.search(r'\\documentclass', content_check, re.IGNORECASE):
                    return largest_root_file
            except Exception: pass # Ignore errors in this final check


        # If no suitable root file or error, return largest overall
        tex_files_sorted = sorted(tex_files, key=lambda x: os.path.getsize(x), reverse=True)
        if tex_files_sorted:
             return tex_files_sorted[0]

    return None # Really shouldn't happen if tex_files list was populated initially

# Define max recursion depth to prevent infinite loops with circular includes
MAX_INLINE_DEPTH = 15

def inline_tex(file_path: str, base_path: str, included_files: typing.Optional[typing.Set[str]] = None, depth: int = 0) -> str:
    """
    Recursively inlines \\input and \\include commands.
    Detects and prevents circular inclusions and excessive depth.
    `base_path` is the directory where the main .tex file was found (or extraction root).
    """
    if depth > MAX_INLINE_DEPTH:
        print(f"Warning: Maximum recursion depth ({MAX_INLINE_DEPTH}) reached while inlining. Stopping inclusion for {file_path}")
        return f"% --- ERROR: Maximum recursion depth reached trying to include {os.path.relpath(file_path, base_path)} ---\n"

    # Use a set to track files included in the current inclusion chain for this call
    if included_files is None:
        included_files = set()

    # Normalize the current file path to avoid issues with relative paths and tracking
    try:
        normalized_file_path = os.path.normpath(os.path.realpath(file_path)) # Use realpath to resolve symlinks if any
    except OSError: # Handle potential errors if file path is invalid early
        print(f"Error: Invalid file path encountered: {file_path}")
        return f"% --- ERROR: Invalid file path: {file_path} ---\n"


    if normalized_file_path in included_files:
        print(f"Warning: Circular dependency detected. Skipping already included file: {normalized_file_path}")
        # Use relative path in the comment for readability
        try:
            rel_path_comment = os.path.relpath(normalized_file_path, base_path)
        except ValueError: # Handle case where paths are on different drives (Windows)
            rel_path_comment = normalized_file_path
        return f"% --- WARNING: Circular dependency detected. Skipping already included file: {rel_path_comment} ---\n"

    included_files.add(normalized_file_path) # Add current file to tracking set for this branch

    content: str
    try:
        # Determine relative path for comments *before* potential errors
        try:
            rel_path_comment = os.path.relpath(normalized_file_path, base_path)
        except ValueError:
            rel_path_comment = normalized_file_path

        # Try reading with UTF-8, fallback to Latin-1
        try:
            with open(normalized_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(normalized_file_path, 'r', encoding='latin-1') as f:
                content = f.read()
            content = f"% --- WARNING: File read with latin-1 encoding: {rel_path_comment} ---\n" + content

    except FileNotFoundError:
        print(f"Error: Included file not found: {normalized_file_path}")
        return f"% --- ERROR: File not found: {rel_path_comment} ---\n"
    except Exception as e:
        print(f"Error reading file {normalized_file_path}: {e}")
        return f"% --- ERROR: Could not read file {rel_path_comment}: {e} ---\n"

    # Pattern to find \input{...} or \include{...}
    # Needs to handle potential spaces and comments between command and brace
    # Looks for \input or \include, optional whitespace, then {filename}
    # Using more careful regex to avoid matching commented out includes
    # Matches lines NOT starting with % (ignoring leading whitespace) that contain \input or \include
    pattern = re.compile(r'^\s*\\(input|include)\s*\{(.*?)\}\s*(%.*)?$') # $ anchors to end, captures optional comment


    # Use a function for replacement to handle recursion
    def replace_match(match: re.Match) -> str:
        # This function processes the *entire line* if it matched the pattern
        original_line = match.group(0)
        command = match.group(1) # 'input' or 'include'
        relative_path = match.group(2).strip()
        trailing_comment = match.group(3) if match.group(3) else ""

        # --- Determine the path to the included file ---
        potential_path_no_ext = os.path.join(os.path.dirname(normalized_file_path), relative_path)
        potential_path_with_ext = potential_path_no_ext + '.tex'

        included_path = None
        # Check existence: prefer .tex if relative_path didn't have it, but check both
        if relative_path.lower().endswith('.tex'):
             # If user specified .tex, check that first
             if os.path.exists(potential_path_no_ext) and not os.path.isdir(potential_path_no_ext):
                 included_path = potential_path_no_ext
        else:
             # If user did not specify .tex, check with .tex first, then without
             if os.path.exists(potential_path_with_ext) and not os.path.isdir(potential_path_with_ext):
                 included_path = potential_path_with_ext
             elif os.path.exists(potential_path_no_ext) and not os.path.isdir(potential_path_no_ext):
                 included_path = potential_path_no_ext


        # If still not found, maybe it existed without .tex even if user specified it? (Less common)
        if included_path is None and relative_path.lower().endswith('.tex'):
             path_without_ext_anyway = os.path.join(os.path.dirname(normalized_file_path), relative_path[:-4])
             if os.path.exists(path_without_ext_anyway) and not os.path.isdir(path_without_ext_anyway):
                  included_path = path_without_ext_anyway

        # Final fallback if neither exists, use the .tex version for the error message
        if included_path is None:
             included_path_for_error = potential_path_with_ext if not relative_path.lower().endswith('.tex') else potential_path_no_ext
             # Normalize for potential error message consistency? Maybe not needed.
             print(f"Warning: Included file specified in {os.path.basename(normalized_file_path)} not found: {relative_path}")
             # Keep the original line but comment it out and add a warning
             return f"% {original_line.strip()}\n% --- WARNING: Above Input/Included file not found: {relative_path} ---"


        # --- Normalize and check for recursion/existence again ---
        try:
            normalized_included_path = os.path.normpath(os.path.realpath(included_path))
            # Get relative path for comments
            try:
                 rel_path_comment_include = os.path.relpath(normalized_included_path, base_path)
            except ValueError:
                 rel_path_comment_include = normalized_included_path
        except OSError:
             print(f"Warning: Invalid path generated for include: {included_path}")
             return f"% {original_line.strip()}\n% --- WARNING: Invalid path generated for include: {relative_path} ---"


        # Check actual existence of normalized path (redundant but safe)
        if not os.path.exists(normalized_included_path):
            print(f"Warning: Included file normalized path not found: {normalized_included_path}")
            return f"% {original_line.strip()}\n% --- WARNING: Input/Included file not found (after normalization): {rel_path_comment_include} ---"


        # --- Perform Recursive Inlining ---
        # Pass a copy of the included_files set
        try:
            include_comment_start = f"% --- Start included file: {rel_path_comment_include} ---"
            # Add newline before \include content as \include typically starts a new page/clears page
            # \input just continues inline. Add newline for clarity anyway.
            newline = "\n" #if command == 'include' else "" # Always add newline for clarity
            included_content = inline_tex(normalized_included_path, base_path, included_files.copy(), depth + 1)
            include_comment_end = f"% --- End included file: {rel_path_comment_include} ---"

            # Preserve the trailing comment from the original line, if any
            return include_comment_start + newline + included_content + newline + include_comment_end + " " + trailing_comment.strip()

        except RecursionError: # Should be caught by depth check, but as safeguard
             print(f"Error: Recursion error while processing {normalized_included_path}")
             return f"% {original_line.strip()}\n% --- ERROR: Recursion error while including {rel_path_comment_include} ---"


    # Process content line by line to handle comments correctly
    processed_lines = []
    for line in content.splitlines():
        match = pattern.match(line)
        if match:
            # Line contains an include/input command and is not commented out at the start
            processed_lines.append(replace_match(match))
        else:
            # Keep lines that don't match the pattern (regular content, comments, etc.)
            processed_lines.append(line)

    inlined_content = "\n".join(processed_lines)

    return inlined_content


def combine_tex_files(main_tex_path: str, progress_callback: typing.Optional[ProgressCallback] = None) -> typing.Optional[str]:
    """
    Combines multiple .tex files into a single LaTeX source string by inlining includes/inputs.
    Returns the combined string or None on failure.
    """
    if not main_tex_path or not os.path.exists(main_tex_path):
        if progress_callback: progress_callback("Main .tex file not found.", 0)
        return None

    if progress_callback:
        progress_callback("Combining .tex files...", None)

    base_path = os.path.dirname(main_tex_path)
    try:
        # Add a header comment
        header = f"% --- Combined LaTeX source generated from arXiv source --- \n"
        header += f"% --- Main file identified as: {os.path.relpath(main_tex_path, base_path)} --- \n\n"
        combined_content = header + inline_tex(main_tex_path, base_path)
        if progress_callback: progress_callback("Combining complete.", 100)
        return combined_content
    except Exception as e:
        # Catch potential unexpected errors during inlining
        print(f"Error during final combination step: {e}")
        if progress_callback: progress_callback(f"Combining Error: {e}", 0)
        import traceback
        traceback.print_exc()
        return None


# --- GUI and Threading ---

# Queue for communication between worker thread and GUI thread
results_queue: queue.Queue = queue.Queue()

def worker_thread_func(arxiv_url: str) -> None:
    """Function to run in the worker thread."""
    global results_queue
    try:
        def progress_update(message: str, percentage: typing.Optional[int] = None) -> None:
            """Helper to put status updates into the queue."""
            results_queue.put(("status", percentage, message))

        arxiv_id = parse_arxiv_id(arxiv_url)
        if not arxiv_id:
            results_queue.put(("error", "Invalid URL", "Could not parse arXiv ID from the URL.\nPlease use a valid abstract, pdf, or e-print URL."))
            results_queue.put(("status", 0, "Failed.")) # Reset status
            return

        progress_update(f"Processing arXiv ID: {arxiv_id}", 0)

        # Step 1: Download (or get from cache)
        cached_tar_path = download_source(arxiv_id, progress_callback=progress_update)

        if not cached_tar_path:
            # Error messages are handled within download_source via progress_callback
            # Ensure a final 'Failed' status if download failed.
            if results_queue.empty() or results_queue.queue[-1] != ("status", 0, "Failed."):
                 results_queue.put(("error", "Download Failed", "Could not download or cache the LaTeX source. Check ID and network connection."))
                 results_queue.put(("status", 0, "Download failed."))
            return

        # Step 2: Extract to a temporary directory
        # Use context manager for automatic cleanup
        with tempfile.TemporaryDirectory(prefix=f"arxiv_{arxiv_id}_") as temp_extract_path:
            progress_update("Extracting source files...", None)
            success = extract_tar(cached_tar_path, temp_extract_path, progress_callback=progress_update)

            if not success:
                results_queue.put(("error", "Extraction Failed", "Could not extract the downloaded archive."))
                results_queue.put(("status", 0, "Extraction failed."))
                return

            # Step 3: Find Main .tex File
            progress_update("Finding main .tex file...", None)
            main_tex_path = find_main_tex(temp_extract_path)

            if not main_tex_path:
                results_queue.put(("error", "Main File Not Found", "Could not automatically determine the main .tex file in the source."))
                results_queue.put(("status", 0, "Main .tex not found."))
                # Optionally list found tex files here for debugging?
                # tex_files_found = [os.path.relpath(p, temp_extract_path) for p in find_all_tex(temp_extract_path)]
                # results_queue.put(("info", f"Found .tex files: {tex_files_found}"))
                return

            progress_update(f"Main .tex file found: {os.path.relpath(main_tex_path, temp_extract_path)}", None)

            # Step 4: Combine .tex Files
            combined_tex = combine_tex_files(main_tex_path, progress_callback=progress_update)

            if combined_tex is None:
                # Error reported by combine_tex_files via callback
                results_queue.put(("error", "Combine Failed", "Failed to combine .tex files. Check console output for details."))
                results_queue.put(("status", 0, "Combining failed."))
                return

            # Step 5: Success - Put result in queue
            results_queue.put(("result", combined_tex))
            # Final status update will be handled in check_queue after copying

    except Exception as e:
        # Catch any unexpected errors during the whole process
        print(f"FATAL WORKER ERROR: {e}")
        import traceback
        traceback.print_exc() # Log detailed error for debugging
        results_queue.put(("error", "Processing Error", f"An unexpected error occurred: {e}\nCheck console for details."))
        results_queue.put(("status", 0, "Error occurred."))

def check_queue(
    root: tk.Tk,
    text_widget: scrolledtext.ScrolledText,
    progress_bar: ttk.Progressbar,
    status_label: tk.Label,
    download_button: tk.Button,
    url_entry: tk.Entry
) -> None:
    """Checks the queue for messages from the worker thread and updates GUI."""
    global results_queue
    is_done = False # Flag to check if processing finished in this batch
    try:
        # Process all messages currently in the queue
        while True:
            message = results_queue.get_nowait()
            msg_type = message[0]

            if msg_type == "status":
                _, value, status_text = message
                update_progress(progress_bar, status_label, value, status_text)
            elif msg_type == "result":
                is_done = True
                _, combined_tex = message
                text_widget.delete(1.0, tk.END)
                text_widget.insert(tk.END, combined_tex)
                # Automatically copy to clipboard
                copy_to_clipboard(root, text_widget, silent=True) # Keep silent on automatic copy
                update_progress(progress_bar, status_label, 100, "Completed and copied to clipboard!")
                progress_bar.stop()  # Explicitly stop any animation
                # Re-enable UI on success
                download_button.config(state=tk.NORMAL)
                url_entry.config(state=tk.NORMAL)
            elif msg_type == "error":
                is_done = True
                _, title, detail = message
                messagebox.showerror(title, detail)
                # Re-enable UI on error
                download_button.config(state=tk.NORMAL)
                url_entry.config(state=tk.NORMAL)
                # Optionally reset progress bar on error
                update_progress(progress_bar, status_label, 0, "Failed.")
            elif msg_type == "info": # For potential debug messages
                 _, info_text = message
                 print(f"INFO: {info_text}")


    except queue.Empty:
        # If queue is empty, schedule the next check only if the process is not done yet
        # We determine this by checking if the UI is still disabled
        if not is_done and download_button['state'] == tk.DISABLED:
            root.after(100, check_queue, root, text_widget, progress_bar, status_label, download_button, url_entry)
        # If is_done is True, the process finished (success or error) and UI was re-enabled, so stop polling.

def process_arxiv_link_threaded(
    root: tk.Tk,
    url_entry: tk.Entry,
    text_widget: scrolledtext.ScrolledText,
    progress_bar: ttk.Progressbar,
    status_label: tk.Label,
    download_button: tk.Button
) -> None:
    """Starts the worker thread and disables UI elements."""
    arxiv_url = url_entry.get().strip()
    # Check if the content is the placeholder text
    if not arxiv_url or url_entry.cget('fg') == 'grey':
         messagebox.showwarning("Input Missing", "Please enter an arXiv URL.")
         return

    # Quick check for valid ID before starting thread and disabling UI
    arxiv_id = parse_arxiv_id(arxiv_url)
    if not arxiv_id:
        messagebox.showerror("Invalid URL", "Could not parse arXiv ID from the URL.\nPlease use a valid abstract, pdf, or e-print URL.")
        return

    # Disable UI elements
    download_button.config(state=tk.DISABLED)
    url_entry.config(state=tk.DISABLED)
    text_widget.delete(1.0, tk.END)
    update_progress(progress_bar, status_label, 0, "Starting...")

    # Clear the queue before starting a new job? Or assume previous job finished.
    # Let's clear it to be safe.
    global results_queue
    while not results_queue.empty():
        try: results_queue.get_nowait()
        except queue.Empty: break

    # Start the worker thread
    thread = threading.Thread(target=worker_thread_func, args=(arxiv_url,), daemon=True)
    thread.start()

    # Schedule the first check of the queue
    root.after(100, check_queue, root, text_widget, progress_bar, status_label, download_button, url_entry)

# --- GUI Utilities ---

def update_progress(progress_bar: ttk.Progressbar, status_label: tk.Label, value: typing.Optional[int], status: str) -> None:
    """Updates the progress bar and status label. Ensures stop on determinate."""
    logging.debug(f"update_progress called with value={value}, status='{status}'")
    current_mode = progress_bar['mode']
    logging.debug(f"  Progress bar mode BEFORE update: {current_mode}")

    if value is not None:
        # --- Determinate Update ---
        # Ensure value is within bounds [0, 100]
        value = max(0, min(100, value))

        # 1. Stop animation if it was running
        if current_mode == 'indeterminate':
            logging.debug("  Stopping indeterminate progress bar before setting determinate value.")
            progress_bar.stop()

        # 2. Set mode to determinate
        progress_bar['mode'] = 'determinate'

        # 3. Set the value
        progress_bar['value'] = value
        logging.debug(f"  Set mode=determinate, value={value}")

    else:
        # --- Indeterminate Update ---
        # Only start if it's not already indeterminate
        if current_mode != 'indeterminate':
            logging.debug("  Setting mode=indeterminate and starting animation.")
            progress_bar['mode'] = 'indeterminate'
            # Reset value? Optional, but good practice for indeterminate state.
            progress_bar['value'] = 0
            progress_bar.start(10) # Start pulsing
        else:
            # Already indeterminate, just update text. No change to bar animation/mode.
            logging.debug("  Mode is already indeterminate. No change to bar animation/mode.")

    # --- Update status text ---
    status_label.config(text=status)
    logging.debug(f"  Progress bar mode AFTER update: {progress_bar['mode']}")

    # --- Force GUI update ---
    # Use update_idletasks to process pending GUI events without blocking
    logging.debug("  Updating idletasks.")
    progress_bar.update_idletasks()
    status_label.update_idletasks()
    logging.debug("  update_progress finished.")


def copy_to_clipboard(root: tk.Tk, text_widget: scrolledtext.ScrolledText, silent: bool = False) -> None:
    """Copies the content of the text widget to the clipboard."""
    try:
        text_content = text_widget.get(1.0, tk.END)
        # Check if content (excluding potential whitespace/newlines) exists
        if not text_content.strip():
            if not silent:
                messagebox.showwarning("Clipboard", "Nothing to copy.")
            return

        root.clipboard_clear()
        root.clipboard_append(text_content)
        if not silent:
            messagebox.showinfo("Copied", "LaTeX source copied to clipboard.")
    except tk.TclError as e:
         # Handle potential clipboard errors (e.g., on systems without clipboard access)
         error_msg = f"Failed to copy to clipboard: {e}"
         print(error_msg)
         if not silent:
             messagebox.showerror("Clipboard Error", error_msg)
    except Exception as e: # Catch any other unexpected errors
        error_msg = f"An unexpected error occurred during copy: {e}"
        print(error_msg)
        if not silent:
            messagebox.showerror("Error", error_msg)


def clear_cache() -> None:
    """Removes the cache directory and its contents."""
    if os.path.exists(CACHE_DIR):
        try:
            # Ask for confirmation
            if messagebox.askyesno("Confirm Clear Cache",
                                   f"This will permanently delete all cached arXiv sources in:\n{CACHE_DIR}\n\nAre you sure?",
                                   icon='warning'): # Use warning icon
                shutil.rmtree(CACHE_DIR)
                ensure_cache_dir() # Recreate the base directory silently
                messagebox.showinfo("Cache Cleared", "The arXiv cache has been cleared.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear cache: {e}")
    else:
        messagebox.showinfo("Cache", "Cache directory does not exist (nothing to clear).")

def setup_placeholder(entry: tk.Entry, placeholder_text: str) -> None:
    """Adds placeholder text functionality to an Entry widget."""
    entry.insert(0, placeholder_text)
    entry.config(fg='grey')

    def on_focusin(event: tk.Event) -> None:
        if entry.cget('fg') == 'grey':
            entry.delete(0, tk.END)
            entry.config(fg='black')

    def on_focusout(event: tk.Event) -> None:
        # Use strip() to avoid placeholder disappearing if only spaces are entered
        if not entry.get().strip():
            # Check if it already contains placeholder to avoid double insertion
            if entry.get() != placeholder_text:
                entry.delete(0, tk.END)
                entry.insert(0, placeholder_text)
            entry.config(fg='grey')

    entry.bind('<FocusIn>', on_focusin, add='+')
    entry.bind('<FocusOut>', on_focusout, add='+')


# --- GUI Creation ---
def create_gui() -> tk.Tk:
    """Creates the main GUI for the application."""
    root = tk.Tk()
    root.title("arXiv LaTeX Source Combiner")
    root.geometry("900x700")
    # Set minimum size to prevent elements overlapping badly
    root.minsize(600, 400)
    root.resizable(True, True)

    # --- Top Frame for Input ---
    # FIX: Remove padding from constructor
    input_frame = tk.Frame(root)
    # FIX: Apply padding in pack()
    input_frame.pack(fill=tk.X, side=tk.TOP, padx=10, pady=10)

    url_label = tk.Label(input_frame, text="arXiv URL:")
    url_label.pack(side=tk.LEFT, padx=(0, 5))

    url_entry = tk.Entry(input_frame) # Width is handled by expansion
    url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
    placeholder_text = "Enter arXiv abstract, pdf, or e-print URL (e.g., https://arxiv.org/abs/2303.12345)"
    setup_placeholder(url_entry, placeholder_text)

    download_button = tk.Button(input_frame, text="Get Combined LaTeX", width=20)
    download_button.pack(side=tk.LEFT, padx=(5, 0))

    # --- Bottom Frame for Actions (Pack this *before* the text area) ---
    # FIX: Remove padding from constructor
    action_frame = tk.Frame(root)
    # FIX: Apply padding in pack() - This was the line causing the latest crash
    action_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=(5, 10))

    copy_button = tk.Button(action_frame, text="Copy to Clipboard", width=20,
                            command=lambda: copy_to_clipboard(root, text_area)) # text_area defined later, lambda captures it
    copy_button.pack(side=tk.LEFT, padx=(0, 10))

    clear_cache_button = tk.Button(action_frame, text="Clear Cache", width=15, command=clear_cache)
    clear_cache_button.pack(side=tk.RIGHT)

    # --- Middle Frame for Progress (Pack this above the action frame) ---
    # FIX: Remove padding from constructor
    progress_frame = tk.Frame(root)
    # FIX: Apply padding in pack()
    progress_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=5)

    progress_bar = ttk.Progressbar(progress_frame, orient='horizontal', mode='determinate', length=300)
    progress_bar.pack(side=tk.LEFT, padx=(0, 5))

    status_label = tk.Label(progress_frame, text="Ready", anchor=tk.W, justify=tk.LEFT)
    status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # --- Main Area for Text Output (Pack this last to fill remaining space) ---
    # FIX: Remove padding from constructor (already done in previous fix)
    text_frame = tk.Frame(root)
    # FIX: Apply padding in pack() (already done in previous fix)
    text_frame.pack(expand=True, fill=tk.BOTH, side=tk.TOP, padx=10, pady=(0, 5))

    text_area = scrolledtext.ScrolledText(text_frame, wrap=tk.NONE, font=("Courier New", 10), undo=True, relief=tk.SUNKEN, borderwidth=1)
    text_area.pack(expand=True, fill=tk.BOTH)


    # --- Set Button Command ---
    download_button.config(command=lambda: process_arxiv_link_threaded(
        root, url_entry, text_area, progress_bar, status_label, download_button
    ))

    # --- Bindings ---
    def select_all(event: tk.Event = None) -> str:
        """Selects all text in the text_area widget."""
        text_area.tag_add(tk.SEL, '1.0', tk.END)
        text_area.mark_set(tk.INSERT, '1.0')
        text_area.see(tk.INSERT)
        return 'break'

    if root.tk.call('tk', 'windowingsystem') == 'aqua': # macOS
        text_area.bind('<Command-a>', select_all)
        text_area.bind('<Command-A>', select_all)
    else: # Windows/Linux
        text_area.bind('<Control-a>', select_all)
        text_area.bind('<Control-A>', select_all)

    def trigger_download(event: tk.Event = None) -> None:
        if download_button['state'] == tk.NORMAL:
             download_button.invoke()

    url_entry.bind('<Return>', trigger_download)

    return root

# --- Main Execution ---

if __name__ == "__main__":
    # 1. Check Dependencies (Requests)
    try:
        import requests
    except ImportError:
        # Need a minimal Tk setup just for the error message
        root_err = tk.Tk()
        root_err.withdraw()
        messagebox.showerror("Missing Library",
                             "Required library 'requests' not found.\nPlease install it using: pip install requests")
        root_err.destroy()
        exit(1) # Exit if dependency is missing

    # 2. Ensure Cache Directory Exists
    ensure_cache_dir()

    # 3. Create and Run the GUI
    app_root = create_gui()
    app_root.mainloop()