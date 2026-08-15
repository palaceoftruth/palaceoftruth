#!/usr/bin/env bash
set -euo pipefail

context_dir="${1:?build context is required}"
dockerfile_dir="${2:?Dockerfile directory is required}"
target="${3:-}"
: "${BUILDKIT_HOST:?BUILDKIT_HOST is required}"
: "${EXPECTED_REF:?Tilt must provide EXPECTED_REF}"

args=(
  --addr "$BUILDKIT_HOST"
  build
  --frontend dockerfile.v0
  --local "context=$context_dir"
  --local "dockerfile=$dockerfile_dir"
  --opt platform=linux/amd64
  --output "type=image,name=$EXPECTED_REF,push=true"
)
[[ -z "$target" ]] || args+=(--opt "target=$target")
buildctl "${args[@]}"
