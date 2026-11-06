#!/usr/bin/env bash
set -euo pipefail

backend_dir="$(cd "$(dirname "$0")/.." && pwd)"
parser_venv="$backend_dir/.venv-docling"
parser_python="${GLOBALPILOT_DOCLING_SETUP_PYTHON:-}"
if [[ -z "$parser_python" ]]; then
  for candidate in "$parser_venv/bin/python" python3.12 python3.13; do
    if command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -c 'import sys; assert sys.version_info[:2] in {(3, 12), (3, 13)}' >/dev/null 2>&1; then
      parser_python="$candidate"
      break
    fi
  done
fi
if [[ -z "$parser_python" ]]; then
  echo 'Python 3.12 or 3.13 is required. Install it (for example: uv python install 3.12),' >&2
  echo 'then set GLOBALPILOT_DOCLING_SETUP_PYTHON to that interpreter and rerun.' >&2
  exit 1
fi
"$parser_python" -c 'import sys; assert sys.version_info[:2] in {(3, 12), (3, 13)}, "Docling runtime requires Python 3.12/3.13"'

if ! command -v tesseract >/dev/null 2>&1; then
  echo 'Install Tesseract and eng/chi_sim language data before running the parser.' >&2
  exit 1
fi
languages="$(tesseract --list-langs 2>/dev/null)"
for language in eng chi_sim; do
  if ! echo "$languages" | grep -qx "$language"; then
    echo "Missing Tesseract language: $language" >&2
    exit 1
  fi
done

if [[ "${1:-}" != "--check" ]]; then
  if [[ ! -x "$parser_venv/bin/python" ]]; then
    "$parser_python" -m venv "$parser_venv"
  fi
  "$parser_venv/bin/python" -m ensurepip --upgrade
  "$parser_venv/bin/python" -m pip install -r "$backend_dir/requirements-docling.txt"
fi
"$parser_venv/bin/python" -c 'import torch; from docling.document_converter import DocumentConverter; from docling.datamodel.pipeline_options import TesseractCliOcrOptions; print("Docling/OCR runtime imports passed")'
if [[ "${1:-}" != "--check" ]]; then
  "$parser_venv/bin/python" "$backend_dir/app/modules/rag/processing/docling_worker.py" --prepare-models
fi
echo 'Docling runtime imports ready (eng/chi_sim OCR, layout, accurate tables).'
echo 'Pictures use grounded OCR/captions; optional GLOBALPILOT_PDF_PICTURE_DESCRIPTION_MODE=vlm adds inspection-only VLM descriptions.'
echo 'Set GLOBALPILOT_DOCLING_PYTHON to use a different parser interpreter.'
