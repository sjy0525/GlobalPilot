"""Run the real Docling worker against retained public PDF extracts.

No embedding, rerank or language model calls are made here. Original PDF bytes,
normalized structure, retrieval metadata and PNG assets are retained for visual
review. Use --file to run one fixture while debugging the local parser setup.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pypdf import PdfReader

from app.modules.rag.processing.pdf_worker import parse_pdf
from app.modules.rag.processing.structured import structured_documents, validate_structure


ROOT = Path(__file__).resolve().parents[1] / "test-results/structured-pdf"


def normalized(value):
    return "".join(value.split()).replace(",", "")


def check_file(path: Path, output: Path) -> dict:
    started = time.monotonic()
    original = path.read_bytes()
    parsed = parse_pdf(original)
    if not parsed.get("ok"):
        raise RuntimeError(f"{path.name}: {parsed.get('code')}: {parsed.get('detail')}")
    structure, assets = parsed["structured"], parsed["assets"]
    validate_structure(structure, assets)
    assert structure["parser"] == "docling"
    assert structure["page_count"] == len(PdfReader(path).pages)
    assert structure["contents"], "Parser returned no evidence items"
    for item in structure["contents"]:
        assert item["provenance"], f"Missing original location: {item['content_id']}"
        for location in item["provenance"]:
            assert location["coord_origin"] in {"TOPLEFT", "BOTTOMLEFT"}
            assert location["page_width"] > 0 and location["page_height"] > 0
    metadata = {"team_id": "corpus-team", "brand_id": "corpus-brand", "document_id": "corpus-document",
                "document_version": 1, "source": path.name}
    canonical, chunks = structured_documents(structure, metadata)
    assert chunks
    assert all(chunk.metadata["content_id"] and chunk.metadata["page_numbers"] for chunk in chunks)
    folder = output / path.stem
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "original.pdf").write_bytes(original)
    (folder / "structure.json").write_text(json.dumps(canonical, ensure_ascii=False, indent=2))
    (folder / "reading-order.txt").write_text("\n\n".join(
        f"[{item['kind']}] page={item['page_no']} content={item['content_id']}\n"
        f"{item.get('markdown') or item.get('text') or item.get('description') or ''}"
        for item in structure["contents"]
    ))
    asset_report = []
    for asset in assets:
        target = folder / f"{asset['id']}.png"
        target.write_bytes(asset["data"])
        asset_report.append({key: value for key, value in asset.items() if key != "data"})
        asset_report[-1].update(bytes=len(asset["data"]), sha256=hashlib.sha256(asset["data"]).hexdigest())
    tables = [item for item in structure["contents"] if item["kind"] == "table"]
    pictures = [item for item in structure["contents"] if item["kind"] == "picture"]
    all_text = normalized("\n".join(
        item.get("text", "") + item.get("markdown", "") + item.get("description", "")
        for item in structure["contents"]
    ))
    probes = []
    if "P01_" in path.name:
        probes = ["52,336.91", "76.74"] if "scan-derived" in path.name else ["52,336.91", "68,196.20"]
    elif "P02_" in path.name:
        probes = ["54,369.94", "65,429.45"]
    elif "P03_" in path.name:
        probes = ["176,380.42", "203,758.59"]
    probe_results = {probe: normalized(probe) in all_text for probe in probes}
    picture_probe_results = {}
    if "scan-derived" in path.name:
        assert not any((page.extract_text() or "").strip() for page in PdfReader(path).pages)
        assert all(probe_results.values()), f"OCR numeric probes failed: {probe_results}"
    else:
        assert tables, "Public budget extract lost its table structure"
        assert all(probe_results.values()), f"Table numeric probes failed: {probe_results}"
        if "P01_" in path.name or "P02_" in path.name:
            picture_probes = ["52,336.91", "76.74"] if "P01_" in path.name else ["54,369.94", "83.10"]
            # Search only picture-specific Tesseract OCR, not body text or VLM
            # descriptions: these same figures also occur in native paragraphs.
            picture_ocr = normalized("\n".join(item.get("ocr_text", "") for item in pictures))
            picture_probe_results = {probe: normalized(probe) in picture_ocr for probe in picture_probes}
            assert pictures, "Embedded figures were not retained as picture assets"
            assert all(picture_probe_results.values()), f"Embedded-image OCR probes failed: {picture_probe_results}"
        tables_page_two = normalized("\n".join(item.get("markdown", "") for item in tables if item["page_no"] == 2))
        assert all(normalized(probe) in tables_page_two for probe in probes), "Table totals must resolve to physical sample page 2"
    result = {"file": path.name, "passed": True, "sha256": hashlib.sha256(original).hexdigest(),
              "elapsed_seconds": round(time.monotonic() - started, 3), "parser": structure["parser"],
              "parser_version": structure.get("parser_version"), "page_count": structure["page_count"],
              "content_count": len(structure["contents"]), "table_count": len(tables),
              "picture_count": len(pictures), "retrieval_chunk_count": len(chunks),
              "numeric_probes": probe_results, "picture_ocr_probes": picture_probe_results, "assets": asset_report,
              "scope": "real Docling OCR/layout/table parser; no embedding/rerank/LLM calls"}
    (folder / "checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--output", type=Path, default=ROOT / "parser-results")
    args = parser.parse_args()
    fixtures = ROOT / "fixtures"
    files = [Path(value) for value in args.file] if args.file else sorted(fixtures.glob("*-sample.pdf")) + sorted(fixtures.glob("*-scan-derived.pdf"))
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"real_components": ["Docling", "Tesseract OCR", "layout detection", "table structure", "PNG asset export", "section-aware chunks"],
              "excluded": ["embedding provider", "rerank provider", "generation provider", "database", "browser"], "cases": []}
    for path in files:
        try:
            case = check_file(path, args.output)
        except Exception as exc:
            case = {"file": path.name, "passed": False, "error": str(exc)}
        report["cases"].append(case)
        print(json.dumps({key: case[key] for key in ("file", "passed", "error", "elapsed_seconds", "content_count", "table_count", "picture_count") if key in case}, ensure_ascii=False), flush=True)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if files and all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
