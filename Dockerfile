# A simplified Dockerfile without a Node.js build stage
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux sqlite3 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY empty_templates /app/empty_templates

# Copy your application code
COPY . .

EXPOSE 5000
CMD ["python", "app.py"]