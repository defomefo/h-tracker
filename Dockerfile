# Lean Python image — slim is ~50MB; full Debian would be 5x bigger.
# 3.12 is the safest stable choice for the google-genai SDK.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Where SQLite lives. fly.toml mounts a persistent volume here so the DB
    # survives deploys and machine restarts.
    HFARM_DB_PATH=/data/h-tracker.db

WORKDIR /app

# Native deps WeasyPrint needs for HTML→PDF rendering:
#   libcairo2          — vector graphics rendering backend
#   libpango-1.0-0     — text shaping (kerning, ligatures, RTL)
#   libpangoft2-1.0-0  — Pango + FreeType bridge
#   libgdk-pixbuf-2.0-0 — image decoding (PNGs, JPEGs in the brief)
#   libffi-dev         — required by some pip wheels at build time
#   shared-mime-info   — mime-type sniffing for embedded assets
#   fonts-dejavu-core  — fallback font so PDFs render even when the
#                        requested family is missing
# All from Debian stable repos. Adds ~25MB to the image, one-time cost.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        shared-mime-info \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so the layer is cached across code-only changes
COPY requirements.txt .
RUN pip install -r requirements.txt

# App files
COPY . .

# Ensure the volume mount point exists even on first boot before Fly attaches
# the disk (gunicorn opens DB_PATH on the first request).
RUN mkdir -p /data

# Fly's internal proxy talks to the container on this port
EXPOSE 8000

# gunicorn with 2 sync workers is plenty for an internal tool. SQLite WAL
# mode handles concurrent reads + serialised writes across workers fine.
# Tweak --workers via fly secret if you need more throughput.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--access-logfile", "-", "app:app"]
