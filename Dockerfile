# MediAssist AI - Hospital WhatsApp Assistant
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    tesseract-ocr \
    poppler-utils \
    tini \
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

# Playwright's default cache dir is under $HOME, which differs between the
# root user (build-time RUN) and appuser (runtime USER below). Pinning an
# absolute path here makes both resolve to the same directory, so the
# browser appuser installed at build time is the one it finds at runtime —
# no more per-boot Chromium re-download.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN playwright install --with-deps chromium

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY admin/ ./admin/
COPY connectors/ ./connectors/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Use tini as PID 1 to reap zombie processes from Playwright headless browser workers
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the application (binds to $PORT on Render/Railway, defaults to 8000 locally with 2 worker processes)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='${FORWARDED_ALLOW_IPS:-127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}'"]
