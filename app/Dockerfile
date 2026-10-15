FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OCR (Tesseract español/inglés) para PDFs escaneados e imágenes
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# antiword para .doc (Word 97-2003); opcional si la distribución no lo trae
RUN apt-get update \
    && (apt-get install -y --no-install-recommends antiword || echo "antiword no disponible: .doc no soportado") \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY rag ./rag
COPY static ./static
COPY tests ./tests
COPY pytest.ini .

EXPOSE 8000
CMD ["uvicorn", "rag.main:app", "--host", "0.0.0.0", "--port", "8000"]
