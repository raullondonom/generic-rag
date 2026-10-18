FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OCR (Tesseract English/Spanish) for scanned PDFs and images, and antiword for .doc (Word 97-2003),
# which is optional if the distribution does not ship it
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
    && (apt-get install -y --no-install-recommends antiword || echo "antiword not available: .doc unsupported") \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY rag ./rag
COPY static ./static
COPY tests ./tests
COPY bench ./bench
COPY pytest.ini .

RUN useradd --uid 1000 --create-home rag \
    && mkdir -p /app/bench/results && chown rag /app/bench/results
USER rag

EXPOSE 8000
CMD ["uvicorn", "rag.main:app", "--host", "0.0.0.0", "--port", "8000"]
