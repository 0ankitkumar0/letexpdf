"""
LaTeX Compilation Web Service
Accepts a .zip of LaTeX sources, compiles to PDF, and returns the result.
Streams real-time progress via Server-Sent Events (SSE).
"""

import io
import json
import os
import glob
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    request,
    send_file,
    render_template,
)

app = Flask(__name__, template_folder="templates")

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {".zip"}
COMPILE_TIMEOUT = 120  # seconds
JOB_TTL = 1800  # seconds (30 min) — completed jobs are purged after this

# In-memory job store  {job_id: {status, progress, events[], result}}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _allowed_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def _find_main_tex(directory: str) -> str | None:
    """Return the path to the main .tex file inside *directory*.

    Strategy:
      1. If main.tex exists (case-insensitive), use it.
      2. Otherwise pick the first .tex file found at the top level.
      3. If nothing at top level, recurse into subdirectories.
    """
    for entry in os.listdir(directory):
        if entry.lower() == "main.tex":
            return os.path.join(directory, entry)

    for entry in sorted(os.listdir(directory)):
        if entry.lower().endswith(".tex"):
            return os.path.join(directory, entry)

    for tex in sorted(glob.glob(os.path.join(directory, "**", "*.tex"), recursive=True)):
        return tex

    return None


def _latexmk_available() -> bool:
    """Return True only if latexmk can actually run (not just a broken shim)."""
    try:
        result = subprocess.run(
            ["latexmk", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _parse_latex_errors(log: str) -> list[dict]:
    """Extract structured errors from a LaTeX log.

    Returns a list of dicts:
      { "type": "error"|"warning", "message": str, "line": int|None, "context": str }
    """
    errors: list[dict] = []

    # --- Errors: lines starting with "!" ---
    # Pattern: ! <message> followed optionally by l.<number> <context>
    error_blocks = re.split(r"(?=^! )", log, flags=re.MULTILINE)
    for block in error_blocks:
        if not block.startswith("! "):
            continue
        lines = block.strip().splitlines()
        message = lines[0][2:].strip()  # remove "! " prefix

        line_no = None
        context = ""
        for l in lines[1:]:
            m = re.match(r"^l\.(\d+)\s*(.*)", l)
            if m:
                line_no = int(m.group(1))
                context = m.group(2).strip()
                break

        errors.append({
            "type": "error",
            "message": message,
            "line": line_no,
            "context": context,
        })

    # --- Warnings: "LaTeX Warning:" lines ---
    for m in re.finditer(
        r"^(LaTeX|Package \w+) Warning:\s*(.+?)(?:\s+on input line (\d+))?\.?\s*$",
        log,
        re.MULTILINE,
    ):
        errors.append({
            "type": "warning",
            "message": m.group(2).strip(),
            "line": int(m.group(3)) if m.group(3) else None,
            "context": "",
        })

    return errors


def _job_event(job_id: str, event_type: str, data: dict):
    """Push an SSE event into the job's event queue."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        evt = {"event": event_type, "data": data}
        job["events"].append(evt)


def _compile_worker(job_id: str, tmp_dir: str, tex_path: str):
    """Run the full compilation pipeline in a background thread."""
    try:
        use_latexmk = _latexmk_available()

        if use_latexmk:
            steps = [("latexmk", [
                "latexmk", "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-no-shell-escape",
                "-outdir=" + tmp_dir,
                tex_path,
            ])]
        else:
            base_cmd = [
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-no-shell-escape",
                "-output-directory=" + tmp_dir,
                tex_path,
            ]
            bib_files = glob.glob(os.path.join(tmp_dir, "**", "*.bib"), recursive=True)
            aux_name = os.path.splitext(os.path.basename(tex_path))[0] + ".aux"
            aux_path = os.path.join(tmp_dir, aux_name)

            if bib_files:
                steps = [
                    ("pdflatex (pass 1/3)", base_cmd),
                    ("bibtex", ["bibtex", aux_path]),
                    ("pdflatex (pass 2/3)", base_cmd),
                    ("pdflatex (pass 3/3)", base_cmd),
                ]
            else:
                steps = [
                    ("pdflatex (pass 1/3)", base_cmd),
                    ("pdflatex (pass 2/3)", base_cmd),
                    ("pdflatex (pass 3/3)", base_cmd),
                ]

        total = len(steps)
        full_log = ""

        for i, (label, cmd) in enumerate(steps):
            pct = int((i / total) * 100)
            _job_event(job_id, "progress", {
                "step": label,
                "current": i + 1,
                "total": total,
                "percent": pct,
            })

            try:
                result = subprocess.run(
                    cmd,
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    timeout=COMPILE_TIMEOUT,
                )
                full_log += result.stdout + "\n" + result.stderr + "\n"

                is_bibtex = os.path.basename(cmd[0]).startswith("bibtex")
                if result.returncode != 0 and not (is_bibtex and result.returncode <= 1):
                    parsed = _parse_latex_errors(full_log)
                    _job_event(job_id, "error", {
                        "message": "Compilation failed at: " + label,
                        "errors": parsed,
                        "log": full_log,
                    })
                    with _jobs_lock:
                        _jobs[job_id]["status"] = "failed"
                    return

            except subprocess.TimeoutExpired:
                _job_event(job_id, "error", {
                    "message": f"Compilation timed out after {COMPILE_TIMEOUT}s at: {label}",
                    "errors": [],
                    "log": full_log,
                })
                with _jobs_lock:
                    _jobs[job_id]["status"] = "failed"
                return
            except FileNotFoundError as exc:
                _job_event(job_id, "error", {
                    "message": f"{exc.filename} not found. Is a TeX distribution installed?",
                    "errors": [],
                    "log": "",
                })
                with _jobs_lock:
                    _jobs[job_id]["status"] = "failed"
                return

        # --- success: locate PDF ---
        pdf_name = os.path.splitext(os.path.basename(tex_path))[0] + ".pdf"
        pdf_path = os.path.join(tmp_dir, pdf_name)

        if not os.path.isfile(pdf_path):
            parsed = _parse_latex_errors(full_log)
            _job_event(job_id, "error", {
                "message": "PDF was not generated despite compilation completing.",
                "errors": parsed,
                "log": full_log,
            })
            with _jobs_lock:
                _jobs[job_id]["status"] = "failed"
            return

        # Read PDF into memory
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # Parse any warnings even on success
        warnings = [e for e in _parse_latex_errors(full_log) if e["type"] == "warning"]

        _job_event(job_id, "done", {
            "pdf_name": pdf_name,
            "pdf_size": len(pdf_bytes),
            "warnings": warnings,
        })

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["pdf_bytes"] = pdf_bytes
            _jobs[job_id]["pdf_name"] = pdf_name

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _purge_expired_jobs():
    """Remove finished jobs older than JOB_TTL to prevent memory leaks."""
    now = time.time()
    with _jobs_lock:
        expired = [
            jid for jid, job in _jobs.items()
            if job["status"] in ("done", "failed")
            and now - job.get("created_at", now) > JOB_TTL
        ]
        for jid in expired:
            del _jobs[jid]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/compile", methods=["POST"])
def compile_upload():
    """Accept the ZIP, start compilation in a thread, return a job_id."""
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request."}), 400

    file = request.files["file"]
    if file.filename == "" or file.filename is None:
        return jsonify({"error": "No file selected."}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "Only .zip files are accepted."}), 400

    # --- create isolated temp dir ---
    job_id = uuid.uuid4().hex
    tmp_dir = os.path.join(tempfile.gettempdir(), "latex_" + job_id)
    os.makedirs(tmp_dir, exist_ok=True)

    zip_path = os.path.join(tmp_dir, "upload.zip")
    file.save(zip_path)

    if os.path.getsize(zip_path) > MAX_UPLOAD_SIZE:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "File too large (max 50 MB)."}), 413

    # --- extract ---
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                member_path = os.path.realpath(os.path.join(tmp_dir, member))
                if not member_path.startswith(os.path.realpath(tmp_dir)):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return jsonify({"error": "Malicious zip entry detected."}), 400
            zf.extractall(tmp_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "Uploaded file is not a valid ZIP archive."}), 400

    # --- find main .tex ---
    tex_path = _find_main_tex(tmp_dir)
    if tex_path is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "No .tex file found in the archive."}), 400

    # --- register job & start worker ---
    _purge_expired_jobs()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "events": [],
            "pdf_bytes": None,
            "pdf_name": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(
        target=_compile_worker,
        args=(job_id, tmp_dir, tex_path),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/progress/<job_id>")
def progress_stream(job_id: str):
    """SSE stream for a running job."""
    def generate():
        cursor = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is None:
                    yield _sse("error", {"message": "Job not found."})
                    return

                new_events = job["events"][cursor:]
                cursor += len(new_events)
                status = job["status"]

            for evt in new_events:
                yield _sse(evt["event"], evt["data"])

            if status in ("done", "failed"):
                return

            import time
            time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/download/<job_id>")
def download_pdf(job_id: str):
    """Download the compiled PDF for a finished job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        if job["status"] != "done" or job["pdf_bytes"] is None:
            return jsonify({"error": "PDF not available."}), 404
        pdf_bytes = job["pdf_bytes"]
        pdf_name = job["pdf_name"]

    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{pdf_name}"'
    return response


def _sse(event: str, data: dict) -> str:
    """Format a single SSE message."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
