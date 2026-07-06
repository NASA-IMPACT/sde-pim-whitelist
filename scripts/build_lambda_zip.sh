#!/usr/bin/env bash
#
# Build the Lambda deployment artifact: Linux-targeted API dependencies + the
# application code + the bundled classified sidecars, zipped deterministically.
#
# The dependency install pins the wheel platform to Amazon Linux (manylinux2014)
# so pydantic-core's compiled wheel matches the Lambda runtime regardless of the
# machine running this script (building on macOS otherwise yields an "invalid ELF
# header" at cold start). Keep LAMBDA_ARCH in lockstep with the CDK function's
# architecture (infra/stacks/platform_stack.py).
#
# Output: ./app.zip at the repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BUILD_DIR="build/lambda"
ARCH="${LAMBDA_ARCH:-x86_64}"          # x86_64 | aarch64
PLATFORM="${ARCH}-manylinux2014"

echo "==> Cleaning previous build"
rm -rf "$BUILD_DIR" app.zip
mkdir -p "$BUILD_DIR"

echo "==> Installing API runtime deps as ${PLATFORM} wheels"
# Only the API extra's top-level packages; uv resolves their transitive deps.
# Deliberately excludes the sync-only deps (click/openai/requests) to keep the
# zip lean — the request path never imports them (openai is imported lazily).
uv pip install \
  --python-platform "$PLATFORM" \
  --python-version 3.12 \
  --only-binary :all: \
  --target "$BUILD_DIR" \
  fastapi mangum pydantic pydantic-settings

echo "==> Copying application code"
cp -r src/pim_whitelist "$BUILD_DIR/pim_whitelist"

echo "==> Bundling classified sidecars at the zip root"
mkdir -p "$BUILD_DIR/whitelist/classified"
cp whitelist/classified/*.json "$BUILD_DIR/whitelist/classified/"

echo "==> Zipping (deterministic: sorted entries, fixed mtime)"
find "$BUILD_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUILD_DIR" -exec touch -t 200001010000 {} +
( cd "$BUILD_DIR" && find . -type f | LC_ALL=C sort | zip -X -q "$ROOT/app.zip" -@ )

echo "==> Built app.zip ($(du -h app.zip | cut -f1))"
