#!/usr/bin/env bash
# Render native-runtime build script.
# Set as the Build Command in Render dashboard:
#   chmod +x build.sh && ./build.sh

set -o errexit  # exit on error

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

echo "==> Installing Playwright Chromium browser..."
# Note: --with-deps is NOT used here because Render's native buildpack
# does not provide root/sudo access during builds. The required system
# libraries (libnss3, libatk, etc.) are already present on Render's
# Ubuntu runtime image.
playwright install chromium

echo "==> Build complete."
