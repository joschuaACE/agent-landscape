"""PDF / Office attachments are converted to markdown (briefing upload + A2A FileParts)."""

from __future__ import annotations

import base64
import io

import pytest

from agent_landscape.a2a.types import FilePart, Message, TextPart
from agent_landscape.context import documents
from agent_landscape.context.intel import items_from_message


def _pptx(text: str) -> bytes:
    pptx = pytest.importorskip("pptx")  # installed with markitdown[pptx]
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Angebots Guideline"
    slide.placeholders[1].text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_convert_office_and_errors():
    text, kind = documents.convert("guideline.pptx", _pptx("Management-Zusammenfassung max. halbe Seite"))
    assert kind == "PowerPoint" and "Angebots Guideline" in text and "Management-Zusammenfassung" in text
    with pytest.raises(documents.ConversionError, match="unsupported"):
        documents.convert("photo.png", b"\x89PNG\r\n\x1a\n\0\0")
    with pytest.raises(documents.ConversionError, match="could not read"):
        documents.convert("broken.pdf", b"%PDF-1.4 not really a pdf \0\0\0")


def test_a2a_filepart_with_office_document_is_converted():
    raw = _pptx("Deliverables clearly defined")
    msg = Message(role="user", parts=[TextPart(text="Review the offer"), FilePart(file={
        "name": "workshop.pptx", "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "bytes": base64.b64encode(raw).decode()})])
    items = items_from_message(msg)
    assert len(items) == 1 and items[0].kind == "file" and items[0].mime == "text/markdown"
    assert "Deliverables clearly defined" in items[0].content
