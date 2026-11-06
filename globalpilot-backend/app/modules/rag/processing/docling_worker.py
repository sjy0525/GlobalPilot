"""Isolated Docling CLI worker. Run with the parser virtualenv, never API Python.

The manifest contains only JSON; image bytes are private sibling PNG files and
are read by pdf_worker before its temporary directory is removed.
"""

from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

# Each bounded job has a fresh process. Compiling the VLM on every upload adds
# unpredictable latency and memory without reuse; eager inference is sufficient.
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _prepare_models(vision_repo: str, *, include_vision: bool = True) -> Path:
    """Download only inference files, not every ONNX quantization in a repo.

    Installed snapshots are linked under the directory layout Docling expects.
    Complete local models need no network request on subsequent uploads.
    """
    from concurrent.futures import ThreadPoolExecutor
    from huggingface_hub import snapshot_download

    root = Path(os.environ.get(
        "GLOBALPILOT_DOCLING_MODEL_CACHE", str(Path.home() / ".cache/globalpilot/docling")
    ))
    root.mkdir(parents=True, exist_ok=True)
    specs = [
        ("docling-project/docling-layout-heron", "main", ["*.json", "*.txt", "*.safetensors"], "model.safetensors"),
        ("docling-project/docling-models", "v2.3.0", ["config.json", "model_artifacts/tableformer/accurate/*"],
         "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors"),
    ]
    if include_vision:
        specs.append((vision_repo, "main", ["*.json", "*.txt", "model.safetensors"], "model.safetensors"))

    def prepare(spec):
        repo, revision, patterns, required = spec
        target = root / repo.replace("/", "--")
        if (target / required).is_file():
            return
        snapshot = Path(snapshot_download(repo_id=repo, revision=revision, allow_patterns=patterns))
        if not (snapshot / required).is_file():
            raise RuntimeError(f"Model weights missing for {repo}")
        # The HF cache provides cross-process file locks; linking is atomic enough
        # for simultaneous first uploads because both resolve the same snapshot.
        try:
            target.symlink_to(snapshot, target_is_directory=True)
        except FileExistsError:
            if not (target / required).is_file():
                raise RuntimeError(f"Incomplete model cache for {repo}")

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(prepare, specs))
    return root


def _image_ocr(path: Path, languages: str) -> str:
    result = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", languages, "--psm", "11"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError("Picture OCR failed")
    return result.stdout.strip()


def _description_annotations(item) -> list[str]:
    descriptions = []
    metadata = getattr(item, "meta", None)
    description = getattr(metadata, "description", None)
    if description is not None and getattr(description, "text", "").strip():
        descriptions.append(description.text.strip())
    if descriptions:
        return descriptions
    for annotation in getattr(item, "annotations", []):
        if getattr(annotation, "kind", None) == "description":
            text = getattr(annotation, "text", "").strip()
            if text and text not in descriptions:
                descriptions.append(text)
    return descriptions


def _top_left_rect(provenance: dict) -> tuple[float, float, float, float]:
    box = provenance["bbox"]
    left, right = sorted((float(box["l"]), float(box["r"])))
    if provenance["coord_origin"] == "BOTTOMLEFT":
        height = float(provenance["page_height"])
        top, bottom = sorted((height - float(box["t"]), height - float(box["b"])))
    else:
        top, bottom = sorted((float(box["t"]), float(box["b"])))
    return left, top, right, bottom


def _attach_table_context(contents: list[dict]) -> None:
    """Link nearby unit lines without altering the source table grid or order."""
    tables = [entry for entry in contents if entry["kind"] == "table" and entry["provenance"]]
    for entry in tables:
        entry["context_text"] = entry.get("caption", "")
        entry["related_content_ids"] = []
    for candidate in contents:
        compact = re.sub(r"\s+", "", candidate.get("text", ""))
        if candidate["kind"] not in {"text", "heading"} or len(compact) > 60:
            continue
        if not re.match(r"^(?:金额)?单位[:：]", compact) or not candidate["provenance"]:
            continue
        choices = []
        for table in tables:
            for unit_prov in candidate["provenance"]:
                for table_prov in table["provenance"]:
                    if unit_prov["page_no"] != table_prov["page_no"]:
                        continue
                    left, top, right, bottom = _top_left_rect(table_prov)
                    uleft, utop, uright, ubottom = _top_left_rect(unit_prov)
                    # Unit labels sit just above a table, often right-aligned.
                    # A label below the table may belong to the following table.
                    if uright < left - 12 or uleft > right + 12:
                        continue
                    if utop > top + 12 or ubottom < top - 48:
                        continue
                    choices.append((abs(ubottom - top), table["content_id"], table))
        if choices:
            # Assign each unit label to only one nearest table on its page.
            target = min(choices, key=lambda choice: (choice[0], choice[1]))[2]
            target["related_content_ids"].append(candidate["content_id"])
            target["context_text"] = "\n".join(part for part in (
                target["context_text"], candidate["text"]
            ) if part)


def convert(source: Path, output_folder: Path) -> dict:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMode, PdfPipelineOptions, PictureDescriptionVlmOptions,
        TableFormerMode, TableStructureOptions, TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.exceptions import DocumentLoadError
    from docling_core.types.doc import PictureItem, TableItem

    languages = os.environ.get("GLOBALPILOT_PDF_OCR_LANGUAGES", "chi_sim+eng")
    vision_repo = os.environ.get("GLOBALPILOT_PDF_VISION_MODEL", "HuggingFaceTB/SmolVLM-256M-Instruct")
    description_mode = os.environ.get("GLOBALPILOT_PDF_PICTURE_DESCRIPTION_MODE", "ocr_caption")
    if description_mode not in {"vlm", "ocr_caption"}:
        raise RuntimeError("Unsupported picture description mode")
    describe_visually = description_mode == "vlm"
    pipeline = PdfPipelineOptions()
    pipeline.artifacts_path = _prepare_models(vision_repo, include_vision=describe_visually)
    pipeline.do_ocr = True
    # Retain reliable PDF text. OCR image/layout regions and image-only scanned
    # pages; full-page replacement is reserved for broken PDF character maps.
    pipeline.ocr_options = TesseractCliOcrOptions(
        lang=languages.split("+"), mode=OcrMode.PDF_AWARE_LAYOUT_REGIONS,
        replace_broken_text_layer=True,
    )
    pipeline.do_table_structure = True
    pipeline.table_structure_options = TableStructureOptions(
        do_cell_matching=True, mode=TableFormerMode.ACCURATE,
    )
    pipeline.images_scale = 2.0
    pipeline.generate_page_images = True
    pipeline.generate_picture_images = True
    pipeline.do_picture_description = describe_visually
    pipeline.picture_description_options = PictureDescriptionVlmOptions(
        repo_id=vision_repo,
        prompt=("Describe the visible diagram, chart or picture briefly. "
                "Do not infer facts or invent numbers that are not visible."),
        generation_config={"max_new_tokens": 128, "do_sample": False},
        batch_size=1, picture_area_threshold=0.0,
    )
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
    })
    try:
        conversion = converter.convert(source, raises_on_error=True)
    except DocumentLoadError as exc:
        raise ValueError("无法解析该 PDF 文件") from exc
    status = str(getattr(conversion.status, "value", conversion.status)).lower()
    if status not in {"success", "partial_success"}:
        raise ValueError("Document conversion did not succeed")
    # Do not silently publish partial parses; missing pages can invalidate citations.
    if status == "partial_success":
        raise RuntimeError("Docling returned a partial parse")
    document = conversion.document
    assets, pages, contents = [], [], []
    for page_no, page in sorted(document.pages.items()):
        asset_id = f"page-{page_no}"
        filename = f"{asset_id}.png"
        page.image.pil_image.save(output_folder / filename, format="PNG")
        width, height = float(page.size.width), float(page.size.height)
        pages.append({"page_no": int(page_no), "width": width, "height": height,
                      "asset_id": asset_id})
        assets.append({"id": asset_id, "media_type": "image/png", "file": filename,
                       "kind": "page", "page_no": int(page_no)})
    page_dimensions = {page["page_no"]: page for page in pages}
    headings: list[tuple[int, str]] = []
    for item, _depth in document.iterate_items():
        label = str(getattr(getattr(item, "label", ""), "value", getattr(item, "label", "")))
        is_table, is_picture = isinstance(item, TableItem), isinstance(item, PictureItem)
        text = str(getattr(item, "text", "") or "").strip()
        if not is_table and not is_picture and not text:
            continue
        reference = str(item.self_ref)
        content_id = "content-" + sha256(reference.encode()).hexdigest()[:20]
        provenance = []
        for prov in getattr(item, "prov", []):
            dimensions = page_dimensions.get(int(prov.page_no), {})
            box = prov.bbox
            origin = str(getattr(box.coord_origin, "value", box.coord_origin)).upper()
            provenance.append({
                "page_no": int(prov.page_no),
                "bbox": {key: float(getattr(box, key)) for key in ("l", "t", "r", "b")},
                "coord_origin": origin,
                "page_width": dimensions.get("width"),
                "page_height": dimensions.get("height"),
            })
        page_no = provenance[0]["page_no"] if provenance else None
        kind = "table" if is_table else "picture" if is_picture else (
            "heading" if label in {"title", "section_header"} else "text"
        )
        if kind == "heading":
            level = 1 if label == "title" else int(getattr(item, "level", 1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
        entry = {
            "content_id": content_id, "kind": kind, "label": label,
            "text": text, "markdown": text,
            "section_path": [heading for _, heading in headings],
            "page_no": page_no, "provenance": provenance,
            "parser_ref": reference,
        }
        if is_table:
            entry["markdown"] = item.export_to_markdown(doc=document)
            entry["text"] = entry["markdown"]
            entry["table"] = item.data.model_dump(mode="json")
            entry["caption"] = item.caption_text(document)
        elif is_picture:
            image = item.get_image(document)
            if image is None:
                raise RuntimeError("Docling did not export a picture asset")
            asset_id = "picture-" + sha256(reference.encode()).hexdigest()[:20]
            filename = f"{asset_id}.png"
            image.save(output_folder / filename, format="PNG")
            ocr_text = _image_ocr(output_folder / filename, languages)
            caption = item.caption_text(document).strip()
            visual_descriptions = _description_annotations(item) if describe_visually else []
            generated_description = "\n".join(visual_descriptions).strip()
            section = " / ".join(entry["section_path"])
            description = "\n".join(part for part in (
                f"第 {page_no} 页图片。",
                f"所在章节：{section}" if section else "",
                f"图注：{caption}" if caption else "",
                f"图片内可识别文字：{ocr_text}" if ocr_text else "",
            ) if part)
            retrieval_ready = bool(caption or ocr_text)
            entry.update({
                "asset_id": asset_id, "caption": caption, "ocr_text": ocr_text,
                "description": description,
                "description_source": "caption_ocr", "description_is_generated": False,
                "description_status": "grounded" if retrieval_ready else "missing",
                "retrieval_ready": retrieval_ready,
                "generated_description": generated_description,
                "generated_description_is_generated": bool(generated_description),
                "generated_description_model": vision_repo if generated_description else None,
            })
            # VLM descriptions can hallucinate. They are inspection-only fields,
            # never part of retrieval text, even when the option is enabled.
            entry["text"] = entry["markdown"] = description if retrieval_ready else ""
            assets.append({"id": asset_id, "media_type": "image/png", "file": filename,
                           "kind": "picture", "page_no": page_no})
        contents.append(entry)

    _attach_table_context(contents)
    # Raw Docling JSON preserves its original content IDs and table structures;
    # replace all embedded image bytes with separately stored asset references.
    raw = document.export_to_dict()
    for page_no, page in raw.get("pages", {}).items():
        if page.get("image"):
            page["image"]["uri"] = f"asset:page-{page_no}"
    for picture in raw.get("pictures", []):
        if picture.get("image"):
            asset_id = "picture-" + sha256(str(picture["self_ref"]).encode()).hexdigest()[:20]
            picture["image"]["uri"] = f"asset:{asset_id}"
    body = "\n\n".join(item["markdown"] for item in contents if item["markdown"].strip())
    if not body.strip():
        raise ValueError("PDF 中没有识别到可检索内容")
    return {
        "ok": True, "text": body, "assets": assets,
        "structured": {
            "schema_version": 1, "parser": "docling", "parser_version": version("docling"),
            "page_count": len(pages), "pages": pages, "contents": contents,
            "ocr": {"engine": "tesseract", "languages": languages.split("+"),
                    "mode": "pdf_aware_layout_regions", "full_page": False,
                    "replace_broken_text_layer": True},
            "picture_description_mode": description_mode,
            "picture_description_model": pipeline.picture_description_options.repo_id if describe_visually else None,
            "raw_document": raw,
        },
    }


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--prepare-models":
        print(_prepare_models(os.environ.get(
            "GLOBALPILOT_PDF_VISION_MODEL", "HuggingFaceTB/SmolVLM-256M-Instruct"
        ), include_vision=os.environ.get("GLOBALPILOT_PDF_PICTURE_DESCRIPTION_MODE", "ocr_caption") == "vlm"))
        return 0
    source, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        result = convert(source, output_path.parent)
    except (ImportError, ModuleNotFoundError):
        logging.exception("Docling dependencies are unavailable")
        result = {"ok": False, "status_code": 503, "code": "PDF_PARSER_UNAVAILABLE",
                  "detail": "PDF 解析器依赖不完整，请运行 scripts/setup_docling.sh"}
    except ValueError as exc:
        logging.exception("Invalid PDF conversion")
        result = {"ok": False, "status_code": 400, "code": "INVALID_PDF",
                  "detail": "PDF 中没有识别到可检索内容" if "没有识别到" in str(exc) else "无法解析该 PDF 文件"}
    except Exception:
        logging.exception("Docling OCR/structure conversion failed")
        result = {"ok": False, "status_code": 503, "code": "PDF_PARSER_UNAVAILABLE",
                  "detail": "PDF OCR 或结构化解析失败，请检查解析器模型与 OCR 服务"}
    output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
