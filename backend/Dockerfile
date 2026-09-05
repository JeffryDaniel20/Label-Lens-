# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential libpq5 curl libgomp1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY . .
# `.[ocr]` (paddleocr/paddlepaddle): this image is python:3.12-slim, the one
# interpreter this project actually runs PaddleOCR under - the "no wheel"
# gap (see app/vision/ocr/paddle.py, P3-T2) is specific to the *local*
# Python 3.14 dev venv, not this deployment target. `libgomp1` is
# paddlepaddle's OpenMP runtime dependency. `libgl1`/`libglib2.0-0`: this
# project's own dependency is `opencv-python-headless` (no GL needed), but
# `paddleocr` pulls in full `opencv-contrib-python` transitively regardless
# (confirmed by a real build against this image, 2026-09-04 - the "headless
# needs nothing extra" assumption in an earlier version of this comment was
# wrong for paddleocr's own dependency, not just this project's).
RUN pip install --upgrade pip && pip install ".[dev,ocr]"

# Never run as root: uploaded content is untrusted from the first byte.
RUN useradd --create-home --uid 10001 labellens && chown -R labellens:labellens /srv
USER labellens

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
