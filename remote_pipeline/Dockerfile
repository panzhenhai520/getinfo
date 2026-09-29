FROM python:3.11-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# OCR via Tesseract + Chinese language pack.
#   - tesseract-ocr            : the Tesseract binary
#   - tesseract-ocr-chi-sim    : Simplified Chinese language data
#   - libtesseract / libtesseract-dev not needed; pytesseract shells the CLI.
# Keep libgl/libglib2.0-0 for crawl4ai/playwright headless rendering.
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && crawl4ai-setup

COPY . /app/remote_pipeline
RUN mkdir -p /data/artifacts

EXPOSE 11236
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "900", "--bind", "0.0.0.0:11236", "remote_pipeline.app:app"]
