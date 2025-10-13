#!/bin/bash
# A script to automate building, pushing, and deploying the Koala Cloud docker container.

# --- Configuration ---
# << IMPORTANT >>: Fill these variables out before running!
REMOTE_USER="vonhoon"
REMOTE_HOST="homeserver"
IMAGE_NAME="server-control"         # Must match the 'image' name in your docker-compose.yaml
PROJECT_DIR_LOCAL="$HOME/projects/server_control" # Your local project path
PROJECT_DIR_REMOTE="server_control" # The project directory on the remote server

# --- Script Logic ---
# set -e: Exit immediately if a command fails.
# set -o pipefail: Ensures that a pipeline command returns a failure status if any command fails.
set -e
set -o pipefail

echo "🚀 Starting Koala Cloud deployment script..."

echo "➡️  Navigating to project directory: ${PROJECT_DIR_LOCAL}"
cd "$PROJECT_DIR_LOCAL"

# --- Versioning ---
# We'll use a local .version file to automatically increment the build number.
VERSION_FILE=".version"
if [ ! -f "$VERSION_FILE" ]; then
    echo "Initializing version file."
    echo "1" > "$VERSION_FILE"
fi

BUILD_NUMBER=$(cat "$VERSION_FILE")
BASE_TAG="1.0"
VERSIONED_TAG="${IMAGE_NAME}:${BASE_TAG}.${BUILD_NUMBER}"
# This static tag matches your docker-compose.yaml and will be overwritten with each deploy.
STATIC_TAG="${IMAGE_NAME}"

IMAGE_FILE="${IMAGE_NAME}_${BASE_TAG}.${BUILD_NUMBER}.tar.gz"

echo "🔨 Building new image..."
echo "   - Versioned Tag: ${VERSIONED_TAG}"
echo "   - Static Tag:    ${STATIC_TAG} (for docker-compose)"
docker build -t "${VERSIONED_TAG}" -t "${STATIC_TAG}" .

echo "📦 Saving and compressing image to ${IMAGE_FILE}..."
docker save "${STATIC_TAG}" | gzip > "${IMAGE_FILE}"

echo "📡 Copying image to ${REMOTE_HOST}..."
scp "${IMAGE_FILE}" "${REMOTE_USER}@${REMOTE_HOST}:~/"

echo "☁️  Deploying on remote server (${REMOTE_HOST})..."
# This block runs all the necessary commands on your remote server in one go.
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
echo "🔢 Bumping version to ${NEW_BUILD_NUMBER} for next build."
echo "${NEW_BUILD_NUMBER}" > "$VERSION_FILE"

echo "---"
echo "✅ Deployment of ${VERSIONED_TAG} successful!"
echo "---"