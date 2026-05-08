FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by pandas/numpy/openpyxl
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Railway injects $PORT at runtime
CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 app:app
