#!/bin/bash
# A script to automate building, pushing, and deploying the Koala Cloud docker container.
# OPTIMIZED: Cleans up old local images before saving to speed up transfer.

# --- Configuration ---
REMOTE_USER="vonhoon"
REMOTE_HOST="homeserver"
IMAGE_NAME="server-control"
PROJECT_DIR_LOCAL="$HOME/projects/server_control"
PROJECT_DIR_REMOTE="server_control"

# --- Script Logic ---
set -e
set -o pipefail

echo "🚀 Starting Koala Cloud deployment script..."
cd "$PROJECT_DIR_LOCAL"

# --- NEW: Clean up old local images ---
echo "🧹 Cleaning up old local image tags to speed up save..."
# This command finds all local images for 'server-control' that are NOT tagged 'latest'
# and removes them. The '|| true' prevents the script from failing if no such images are found.
docker images --format "{{.Repository}}:{{.Tag}}" | grep "^${IMAGE_NAME}:" | grep -v ":latest$" | xargs --no-run-if-empty docker rmi || true

# --- Versioning ---
VERSION_FILE=".version"
if [ ! -f "$VERSION_FILE" ]; then echo "1" > "$VERSION_FILE"; fi
BUILD_NUMBER=$(cat "$VERSION_FILE")
BASE_TAG="1.0"
VERSIONED_TAG="${IMAGE_NAME}:${BASE_TAG}.${BUILD_NUMBER}"
STATIC_TAG="${IMAGE_NAME}" # This becomes 'latest'

IMAGE_FILE="${IMAGE_NAME}_${BASE_TAG}.${BUILD_NUMBER}.tar.gz"

echo "🔨 Building new image..."
docker build -t "${VERSIONED_TAG}" -t "${STATIC_TAG}" .

echo "📦 Saving and compressing image to ${IMAGE_FILE}..."
docker save "${STATIC_TAG}" | gzip > "${IMAGE_FILE}"

echo "📡 Copying image to ${REMOTE_HOST}..."
scp "${IMAGE_FILE}" "${REMOTE_USER}@${REMOTE_HOST}:~/"

echo "☁️  Deploying on remote server (${REMOTE_HOST})..."
ssh "${REMOTE_USER}@${REMOTE_HOST}" << EOF
    set -e
    echo "    - Navigating to remote project directory..."
    cd ~/${PROJECT_DIR_REMOTE}

    echo "    - Stopping current services..."
    docker compose down

    echo "    - Loading new image from tarball..."
    gunzip -c ~/${IMAGE_FILE} | docker load

    echo "    - Starting new services in detached mode..."
    docker compose up -d

    echo "    - Cleaning up remote image file..."
    rm ~/${IMAGE_FILE}
EOF

# --- Cleanup and Version Bump ---
echo "🧹 Cleaning up local image file..."
rm "${IMAGE_FILE}"

NEW_BUILD_NUMBER=$((BUILD_NUMBER + 1))
echo "${NEW_BUILD_NUMBER}" > "$VERSION_FILE"

echo "---"
echo "✅ Deployment of ${VERSIONED_TAG} successful!"
echo "---"