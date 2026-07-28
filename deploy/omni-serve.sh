#!/usr/bin/env bash
# omni-serve.sh — compatibility shim. The real launcher is local-serve.sh.
#
# This script used to be Nemotron-Omni-specific: it hardcoded that model's vLLM
# --tool-call-parser / --reasoning-parser, so pointing AVA_OMNI_MODEL at anything
# else served the new model with the old model's parsers and silently broke tool
# calling. local-serve.sh resolves the parsers per model family instead.
#
# Kept because things reference it by path — `ava-omni.service` on the development
# box, and any docs/muscle memory. It forwards every argument and all the legacy
# OMNI_* env vars still work. Prefer `bash deploy/local-serve.sh` in new setups.
#
# To serve a different model:
#   AVA_MODEL=Qwen/Qwen3-32B-AWQ bash deploy/local-serve.sh
exec "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/local-serve.sh" "$@"
