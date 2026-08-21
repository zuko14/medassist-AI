#!/usr/bin/env bash
# Render native-runtime build script.
# Set as the Build Command in Render dashboard:
#   chmod +x build.sh && ./build.sh

set -o errexit  # exit on error

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

echo "==> Installing Playwright Chromium browser..."
playwright install --with-deps chromium

echo "==> Build complete."
