"""Uploaded-file text extraction + message augmentation.

The `openclaw agent` CLI is text-only, so uploads are ingested HERE: extract text
(pdftotext / LibreOffice / OCR) and fold it into the message sent to Ava. Images
are stored + shown; with no vision model, only OCR'd text (if tesseract present)
reaches Ava.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import config, state

TEXT_EXTS = config.TEXT_EXTS
IMAGE_EXTS = config.IMAGE_EXTS
OFFICE_EXTS = config.OFFICE_EXTS


def _ext_run(cmd, timeout=120):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout)


def _pdf_text(path: str) -> str:
    try:
        cp = _ext_run(["pdftotext", "-q", "-layout", path, "-"])
        return cp.stdout.decode("utf-8", "ignore") if cp.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _office_text(path: str) -> str:
    """Convert an Office/ODF doc to plain text via headless LibreOffice."""
    d = tempfile.mkdtemp(prefix="ava_doc_")
    try:
        _ext_run(["soffice", "--headless", "--convert-to", "txt:Text",
                  "--outdir", d, path], timeout=150)
        for n in os.listdir(d):
            if n.lower().endswith(".txt"):
                with open(os.path.join(d, n), "rb") as f:
                    return f.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ""


def _ocr_text(path: str) -> str:
    if not shutil.which("tesseract"):
        return ""
    try:
        cp = _ext_run(["tesseract", path, "stdout"], timeout=90)
        return cp.stdout.decode("utf-8", "ignore") if cp.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def extract_text(path: str, ext: str) -> str:
    ext = ext.lower()
    if ext in TEXT_EXTS:
        try:
            with open(path, "rb") as f:
                return f.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    if ext == ".pdf":
        return _pdf_text(path)
    if ext in OFFICE_EXTS:
        return _office_text(path)
    if ext in IMAGE_EXTS:
        return _ocr_text(path)
    # Unknown type: treat as text only if it looks textual (no NUL bytes).
    try:
        with open(path, "rb") as f:
            data = f.read()
        if b"\x00" not in data[:4096]:
            return data.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:120] or "file"


def _parse_ids(raw: str) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _augment(text: str, ids: list[str]) -> str:
    """Fold uploaded-attachment text into the message sent to Ava."""
    if not ids:
        return text
    blocks = []
    for aid in ids:
        with state.attachments_lock:
            rec = state.attachments.get(aid)
        if not rec:
            continue
        if rec["text"]:
            blocks.append(f'--- Attached {rec["kind"]} "{rec["filename"]}" ---\n'
                          f'{rec["text"]}')
        elif rec["kind"] == "image":
            blocks.append(f'[The user attached an image "{rec["filename"]}". No text '
                          f'could be extracted from it.]')
        else:
            blocks.append(f'[The user attached "{rec["filename"]}" but no readable text '
                          f'could be extracted.]')
    if not blocks:
        return text
    base = text or "Please review the attached file(s)."
    return base + "\n\n" + "\n\n".join(blocks)
