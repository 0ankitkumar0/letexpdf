# ---------- Stage: runtime ----------
FROM debian:bookworm-slim

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install TeX Live (latex-extra covers most common packages) + latexmk + cleanup
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-extra \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        texlive-fonts-extra \
        texlive-bibtex-extra \
        texlive-science \
        texlive-pictures \
        latexmk \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Application directory
WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY templates/ templates/

# Use the venv Python
ENV PATH="/app/venv/bin:$PATH"

EXPOSE 5000

# Run with a production-ready server (Werkzeug's built-in server is fine for
# moderate loads behind a reverse proxy; swap for gunicorn if needed).
CMD ["python3", "app.py"]
