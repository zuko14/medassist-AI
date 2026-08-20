# MediAssist AI - Hospital WhatsApp Assistant
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# The hospital and all its patients are in India — every naive datetime.now()
# call in this codebase (scheduler cron times, slot cutoffs, date validation)
# assumes local IST wall-clock time. Without this, the container defaults to
# UTC and every scheduled job fires 5.5 hours off from the intended time.
ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY admin/ ./admin/
COPY connectors/ ./connectors/
COPY tests/ ./tests/

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Run the application (binds to $PORT on Render/Railway, defaults to 8000 locally)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
