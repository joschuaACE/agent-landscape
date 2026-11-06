"""Office / PDF documents → Markdown text agents can read (Microsoft markitdown).

Used by the briefing upload (POST /api/context/convert) and for binary FileParts arriving over A2A.
Conversion is local and offline; nothing leaves the machine.
"""

from __future__ import annotations

import io
import logging
from pathlib import PurePath

log = logging.getLogger(__name__)

# extension → label shown in the UI
CONVERTIBLE = {
    ".pdf": "PDF", ".docx": "Word", ".pptx": "PowerPoint", ".xlsx": "Excel", ".xls": "Excel",
    ".epub": "EPUB", ".msg": "Outlook mail",
}
MAX_BYTES = 25 * 1024 * 1024
MAX_CHARS = 400_000  # same cap as intel documents


class ConversionError(ValueError):
    pass


def kind_of(name: str) -> str | None:
    return CONVERTIBLE.get(PurePath(name or "").suffix.lower())


def convert(name: str, data: bytes) -> tuple[str, str]:
    """Return (markdown text, document kind). Raises ConversionError with a user-friendly message."""
    kind = kind_of(name)
    if kind is None:
        supported = ", ".join(sorted(CONVERTIBLE))
        raise ConversionError(f"{name}: unsupported binary format (convertible: {supported})")
    if len(data) > MAX_BYTES:
        raise ConversionError(f"{name} is too large ({len(data) / 1e6:.1f} MB, max {MAX_BYTES // 1_000_000} MB)")
    try:
        from markitdown import MarkItDown, StreamInfo
    except ImportError as exc:  # optional at import time – the app still runs without it
        raise ConversionError("Document conversion is not installed (pip install 'markitdown[pdf,docx,pptx,xlsx]')") from exc
    try:
        result = MarkItDown(enable_plugins=False).convert_stream(
            io.BytesIO(data), stream_info=StreamInfo(extension=PurePath(name).suffix.lower(), filename=name))
    except Exception as exc:  # noqa: BLE001 – corrupt / encrypted / unsupported content
        log.info("conversion of %s failed: %s", name, exc)
        raise ConversionError(f"{name}: could not read the {kind} file ({type(exc).__name__})") from exc
    text = (result.text_content or "").strip()
    if not text:
        raise ConversionError(f"{name}: no text found – scanned {kind}s need OCR, which is not supported")
    return text[:MAX_CHARS], kind
