"""Real HTTP/Docling/pgvector/provider verification against an isolated test DB.

The app must be running on --base-url with the same disposable database. Private
login details stay in ignored 0600 files; public reports never contain tokens.
"""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "test-results/structured-pdf"


def compact(text):
    return re.sub(r"[\s,，]", "", text)


def contains_number(text, expected):
    return any(Decimal(number) == Decimal(expected)
               for number in re.findall(r"\d+(?:\.\d+)?", compact(text)))


def verify_case(client, credentials, filename, question, expected, expected_page):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.core.config import settings
    import app.db.model_registry  # Register all ORM FK targets in this standalone script.
    from app.modules.brands.models import Brand

    engine = create_engine(settings.database_url)
    name = "PDF OCR 验收" if "scan-derived" in filename else "PDF 表格验收"
    with Session(engine) as db, db.begin():
        brand = db.scalar(select(Brand).where(Brand.team_id == credentials["team_id"], Brand.name == name))
        if brand is None:
            brand = Brand(team_id=credentials["team_id"], name=name)
            db.add(brand)
            db.flush()
        brand_id = str(brand.id)
    engine.dispose()
    params = {"team_id": credentials["team_id"], "brand_id": brand_id}
    source = RESULTS / "fixtures" / filename
    started = time.monotonic()
    existing_response = client.get("/api/documents", params=params)
    existing_response.raise_for_status()
    existing = next((record for record in existing_response.json() if record["source"] == filename), None)
    files = {"file": (filename, source.read_bytes(), "application/pdf")}
    if existing:
        response = client.put(f"/api/documents/{existing['id']}",
                              params={**params, "expected_version": existing["version"]}, files=files)
    else:
        response = client.post("/api/documents", params=params, files=files)
    response.raise_for_status()
    document = response.json()
    identifier = document["id"]
    structure_response = client.get(f"/api/documents/{identifier}/structure", params=params)
    structure_response.raise_for_status()
    structure = structure_response.json()["structured"]
    original = client.get(f"/api/documents/{identifier}/file", params=params)
    original.raise_for_status()
    assert hashlib.sha256(original.content).digest() == hashlib.sha256(source.read_bytes()).digest()
    for page in structure["pages"]:
        asset = client.get(f"/api/documents/{identifier}/assets/{page['asset_id']}", params={
            **params, "expected_version": document["version"],
        })
        asset.raise_for_status()
        assert asset.content.startswith(b"\x89PNG"), "Page asset is not PNG"
    query = client.post("/api/query", params={"team_id": credentials["team_id"]},
                        json={"brand_id": brand_id, "question": question})
    query.raise_for_status()
    result = query.json()
    assert result["sources"], "No original evidence was retrieved"
    assert contains_number(result["answer"], expected), "Expected fact is missing from generated answer"
    assert re.search(r"\[\d+\]", result["answer"]), "Answer has no source reference"
    assert re.search(rf"第\s*{expected_page}\s*页|页码[：:\s]*{expected_page}(?!\d)", result["answer"]), "Answer has no expected PDF page number"
    matching = [hit for hit in result["sources"]
                if hit["document_id"] == identifier and expected_page in hit["page_numbers"]
                and contains_number(hit["content"], expected)]
    assert matching, "Expected fact was not linked to the correct original PDF page"
    for hit in result["sources"]:
        assert hit["document_version"] == document["version"]
        for item in hit["evidence"]:
            evidence_response = client.get(
                f"/api/documents/{identifier}/contents/{item['content_id']}",
                params={**params, "expected_version": document["version"]},
            )
            evidence_response.raise_for_status()
            assert evidence_response.json() == item, "Query evidence differs from original evidence API"
    report = {"file": filename, "passed": True, "document_id": identifier, "brand_id": brand_id,
              "team_id": credentials["team_id"], "question": question, "answer": result["answer"],
              "sources": result["sources"], "page_count": structure["page_count"],
              "content_count": len(structure["contents"]), "chunk_count": document["chunk_count"],
              "expected_page": expected_page, "expected_fact": expected,
              "original_pdf_sha256": hashlib.sha256(original.content).hexdigest(),
              "elapsed_seconds": round(time.monotonic() - started, 2)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--only", choices=["table", "ocr"])
    args = parser.parse_args()
    os.environ["DATABASE_URL"] = (RESULTS / "database-url.txt").read_text().strip()
    from sqlalchemy.engine import make_url
    assert make_url(os.environ["DATABASE_URL"]).database.startswith("test_structured_pdf_"), "Isolated test DB required"
    credentials = json.loads((RESULTS / "browser-credentials.json").read_text())
    report = {"real_components": ["HTTP", "Docling", "Tesseract OCR", "layout/table models",
                                  "picture descriptions", "PostgreSQL", "pgvector", "DashScope embedding",
                                  "DashScope rerank", "DeepSeek generation"], "cases": []}
    with httpx.Client(base_url=args.base_url, timeout=900) as client:
        login = client.post("/api/auth/login", json={key: credentials[key] for key in ("email", "password")})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        cases = [
            ("table", "P01_国务院办公厅2024年部门预算-sample.pdf",
             "请根据部门收支总表回答：收入总计和支出总计各是多少万元？注意与本年收入合计区分。注明PDF物理页码并引用来源编号。", "68196.20", 2),
            ("ocr", "P01_国务院办公厅2024年部门预算-scan-derived.pdf",
             "图片中一般公共预算拨款收入是多少万元，占本年收入的比例是多少？注明PDF物理页码并引用来源编号。", "52336.91", 1),
        ]
        for name, filename, question, expected, page in cases:
            if args.only and name != args.only:
                continue
            try:
                result = verify_case(client, credentials, filename, question, expected, page)
            except Exception as exc:
                with (RESULTS / "online-diagnostics.log").open("a") as diagnostics:
                    traceback.print_exc(file=diagnostics)
                # Do not serialize HTTP request headers, response dumps or secrets.
                result = {"file": filename, "passed": False, "error_type": type(exc).__name__,
                          "error": str(exc) if isinstance(exc, AssertionError) else "Check private server log for diagnostics"}
            report["cases"].append(result)
            print(json.dumps({key: result[key] for key in ("file", "passed", "error_type", "error", "elapsed_seconds", "document_id") if key in result}, ensure_ascii=False), flush=True)
            (RESULTS / (f"online-{args.only}.json" if args.only else "online.json")).write_text(
                json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["cases"] and all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
