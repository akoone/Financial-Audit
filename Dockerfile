FROM python:3.12-slim
WORKDIR /app
# OMP_THREAD_LIMIT=1: on a small shared CPU, Tesseract's extra threads only fight each other (2x slower)
ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/mpl OMP_THREAD_LIMIT=1
# Tesseract OCR: free receipt/screenshot reading when no AI key is set
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-uzb tesseract-ocr-uzb-cyrl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import matplotlib.pyplot"   # build the font cache now, not at every cold start
COPY app ./app
EXPOSE 8080
CMD ["python", "-m", "app.main"]
