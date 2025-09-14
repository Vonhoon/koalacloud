FROM python:3.11-slim

WORKDIR /app

# nsenter lives in util-linux; sqlite3 for local token DB
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
CMD ["python", "app.py"]
