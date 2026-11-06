"""Validate PDFs and run Docling in its isolated OCR/ML environment.

This module stays importable in spawned API workers without importing Torch.
There is no text-only fallback when the structured parser is unavailable.
"""

from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from pypdf import PdfReader
from pypdf.errors import ParseError, PdfReadError

BACKEND_ROOT = Path(__file__).resolve().parents[4]


def _failure(detail: str, *, status_code: int = 400, code: str = "INVALID_PDF") -> dict:
    return {"ok": False, "detail": detail, "pid": os.getpid(),
            "status_code": status_code, "code": code}


def _invalid_pdf(detail: str) -> dict:
    return _failure(detail)


def parse_pdf(content: bytes) -> dict:
    """Return picklable normalized structure and binary image assets."""
    if not content:
        return _invalid_pdf("PDF 文件不能为空")
    # Format/encryption preflight only. All extraction is performed by Docling.
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            return _invalid_pdf("不支持加密的 PDF 文件")
        if not reader.pages:
            return _invalid_pdf("PDF 文件没有页面")
    except (PdfReadError, ParseError, OSError, ValueError, NotImplementedError):
        return _invalid_pdf("无法解析该 PDF 文件")

    interpreter = Path(os.environ.get(
        "GLOBALPILOT_DOCLING_PYTHON", str(BACKEND_ROOT / ".venv-docling/bin/python")
    ))
    if not interpreter.is_file():
        return _failure("PDF 结构化解析服务尚未安装，请运行 scripts/setup_docling.sh",
                        status_code=503, code="PDF_PARSER_UNAVAILABLE")
    try:
        timeout = max(30, int(os.environ.get("GLOBALPILOT_DOCLING_TIMEOUT", "600")))
    except ValueError:
        timeout = 600
    with TemporaryDirectory(prefix="globalpilot-docling-") as directory:
        folder = Path(directory)
        input_path, output_path = folder / "source.pdf", folder / "result.json"
        input_path.write_bytes(content)
        try:
            completed = subprocess.run(
                [str(interpreter), str(Path(__file__).with_name("docling_worker.py")),
                 str(input_path), str(output_path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return _failure("PDF 结构化解析超时，请缩小文件后重试", status_code=503,
                            code="PDF_PARSER_TIMEOUT")
        except OSError:
            return _failure("PDF 结构化解析服务无法启动", status_code=503,
                            code="PDF_PARSER_UNAVAILABLE")
        if not output_path.is_file():
            return _failure("PDF 结构化解析服务运行失败，请检查解析器和模型安装",
                            status_code=503, code="PDF_PARSER_UNAVAILABLE")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
            if not result.get("ok"):
                result["pid"] = os.getpid()
                return result
            if completed.returncode:
                return _failure("PDF 结构化解析服务运行失败", status_code=503,
                                code="PDF_PARSER_UNAVAILABLE")
            assets = []
            for asset in result.get("assets", []):
                asset_path = (folder / asset.pop("file")).resolve()
                if not asset_path.is_relative_to(folder.resolve()):
                    raise ValueError("Asset outside parser temporary directory")
                assets.append({**asset, "data": asset_path.read_bytes()})
            result["assets"], result["pid"] = assets, os.getpid()
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return _failure("PDF 结构化解析结果无效", status_code=503,
                            code="PDF_PARSER_UNAVAILABLE")
