#!/usr/bin/env bash

"""
run_ui_test_docker.sh

Scaffolds a lightweight headless Linux environment (via Docker)
for UI testing. This avoids polluting the Mac environment and is
perfect for headless agents running Playwright.

Prerequisites:
- Docker installed and running
"""

set -euo pipefail

IMAGE_NAME="playwright-ui-test-env"
CONTAINER_NAME="fireworks-ui-tester"

echo "Checking for Docker..."
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install Docker to use the remote UI testing box."
    exit 1
fi

echo "Building headless testing container image..."
# We create a simple Dockerfile inline
mkdir -p .tmp/docker_build
cat << 'EOF' > .tmp/docker_build/Dockerfile
# Use the official Microsoft Playwright image which includes all browsers and dependencies
FROM mcr.microsoft.com/playwright:v1.43.0-jammy

# Set up working directory
WORKDIR /app

# Install standard dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Default command keeps the container alive for execution
CMD ["sleep", "infinity"]
EOF

docker build -t "$IMAGE_NAME" .tmp/docker_build/

echo "Starting container: $CONTAINER_NAME..."
# Stop and remove if it already exists
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true

# Run the container in the background, mounting the current repository
docker run -d \
    --name "$CONTAINER_NAME" \
    -v "$(pwd)":/app \
    "$IMAGE_NAME"

echo ""
echo "✅ Remote UI Box is running!"
echo "Agents can now execute tests inside this Linux container using:"
echo "  docker exec $CONTAINER_NAME npm test"
echo "  docker exec $CONTAINER_NAME npx playwright test"
echo ""
echo "To stop the box, run: docker stop $CONTAINER_NAME"
