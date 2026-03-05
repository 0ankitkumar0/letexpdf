"""Smoke tests for the /compile + SSE progress + /download pipeline."""
import json
import os
import sys
import tempfile
import time
import zipfile
import requests
import threading

BASE = "http://localhost:5000"


def make_zip(name, tex_source):
    path = os.path.join(tempfile.gettempdir(), name)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("main.tex", tex_source)
    return path


# ── Test 1: Successful compile ──────────────────────────
def test_success():
    print("\n=== Test 1: Successful compile ===")
    zip_path = make_zip("test_ok.zip", r"""\documentclass{article}
\begin{document}
\title{Test} \author{A} \maketitle
\section{Hello}
This is \LaTeX.
\end{document}
""")

    with open(zip_path, "rb") as f:
        r = requests.post(f"{BASE}/compile", files={"file": ("t.zip", f)}, timeout=30)
    print(f"  Upload  : {r.status_code}")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    print(f"  Job ID  : {job_id}")

    # Listen to SSE
    events = []
    r2 = requests.get(f"{BASE}/progress/{job_id}", stream=True, timeout=120)
    for line in r2.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            evt_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1].strip())
            events.append((evt_type, data))
            print(f"  SSE     : {evt_type} → {data.get('step') or data.get('pdf_name') or ''}")
            if evt_type in ("done", "error"):
                break

    assert events[-1][0] == "done", f"Expected 'done', got '{events[-1][0]}'"
    pdf_name = events[-1][1]["pdf_name"]

    # Download PDF
    r3 = requests.get(f"{BASE}/download/{job_id}", timeout=30)
    print(f"  Download: {r3.status_code} ({len(r3.content)} bytes)")
    assert r3.status_code == 200
    assert len(r3.content) > 1000
    print("  ✓ PASS")


# ── Test 2: Compile with error ──────────────────────────
def test_error():
    print("\n=== Test 2: Compile with LaTeX error ===")
    zip_path = make_zip("test_err.zip", r"""\documentclass{article}
\begin{document}
\section{Hello}
Some text \undefinedmacro here.
\end{document}
""")

    with open(zip_path, "rb") as f:
        r = requests.post(f"{BASE}/compile", files={"file": ("t.zip", f)}, timeout=30)
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    events = []
    r2 = requests.get(f"{BASE}/progress/{job_id}", stream=True, timeout=120)
    for line in r2.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            evt_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1].strip())
            events.append((evt_type, data))
            if evt_type in ("done", "error"):
                break

    assert events[-1][0] == "error", f"Expected 'error', got '{events[-1][0]}'"
    err_data = events[-1][1]
    print(f"  Message : {err_data['message']}")
    print(f"  Errors  : {len(err_data.get('errors', []))} parsed")
    for e in err_data.get("errors", []):
        tag = f"line {e['line']}" if e.get("line") else "?"
        print(f"    [{e['type']}] {tag}: {e['message']}")
    print("  ✓ PASS")


if __name__ == "__main__":
    test_success()
    test_error()
    print("\nAll tests passed.")
