# Stage 1: Build the frontend assets using a Node.js image
FROM node:20-slim AS builder

WORKDIR /build

# Copy the package.json file
COPY package.json .

# Run npm install based on the package.json. This is the standard way.
RUN npm install

# Copy the file for building the CSS
COPY input.css .

# Run the executable from its precise local path
RUN ./node_modules/.bin/tailwindcss -i ./input.css -o ./output.css --minify


# Stage 2: Build the final Python application image
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux sqlite3 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your application code
COPY . .

# Copy the compiled 'output.css' from the 'builder' stage
COPY --from=builder /build/output.css ./static/output.css

EXPOSE 5000
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:5000", "--worker-class", "eventlet", "app:app"]