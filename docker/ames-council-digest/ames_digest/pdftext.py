"""PDF text extraction.

Most City of Ames packet PDFs are born-digital and carry a real text layer.
A minority — scanned petitions, signed agreements, hand-marked exhibits — are
images with no extractable text. There is no OCR in this image, so those are
detected and reported rather than silently summarized as empty.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger(__name__)

# Below this many extracted characters per page, assume there is no usable text
# layer. A genuinely sparse page (a title page, a signature block) still clears
# it once averaged over a whole document of any length.
MIN_CHARS_PER_PAGE = 40


@dataclass
class ExtractedText:
    text: str
    page_count: int
    truncated: bool = False
    error: str | None = None

    @property
    def has_text_layer(self) -> bool:
        if self.error or not self.page_count:
            return False
        return len(self.text) / self.page_count >= MIN_CHARS_PER_PAGE

    @property
    def usable(self) -> bool:
        return bool(self.text.strip()) and self.has_text_layer


def extract(data: bytes, char_budget: int | None = None) -> ExtractedText:
    """Extract text from PDF bytes, stopping once ``char_budget`` is reached.

    Truncation is page-aligned and flagged so the prompt can tell the model the
    document continues past what it was shown.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PyPdfError, ValueError, OSError) as exc:
        return ExtractedText(text="", page_count=0, error=f"unreadable PDF: {exc}")

    if reader.is_encrypted:
        # Public documents are occasionally saved with an empty owner password,
        # which pypdf can open with a blank user password.
        try:
            if reader.decrypt("") == 0:
                return ExtractedText(
                    text="", page_count=len(reader.pages), error="password protected"
                )
        except (PyPdfError, NotImplementedError) as exc:
            return ExtractedText(text="", page_count=0, error=f"cannot decrypt: {exc}")

    try:
        total_pages = len(reader.pages)
    except (PyPdfError, ValueError) as exc:
        return ExtractedText(text="", page_count=0, error=f"unreadable PDF: {exc}")

    chunks: list[str] = []
    size = 0
    truncated = False

    for index in range(total_pages):
        try:
            page_text = reader.pages[index].extract_text() or ""
        except (PyPdfError, ValueError, KeyError, RecursionError) as exc:
            # One bad page shouldn't discard a 200-page packet item.
            log.debug("page %d extraction failed: %s", index + 1, exc)
            continue

        page_text = page_text.strip()
        if not page_text:
            continue

        chunks.append(page_text)
        size += len(page_text)
        if char_budget is not None and size >= char_budget:
            truncated = index + 1 < total_pages
            break

    return ExtractedText(
        text="\n\n".join(chunks),
        page_count=total_pages,
        truncated=truncated,
    )
