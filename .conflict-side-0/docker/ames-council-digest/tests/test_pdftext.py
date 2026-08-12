"""PDF text extraction, and telling a born-digital packet item from a scan."""

from __future__ import annotations

import pytest

from ames_digest.pdftext import MIN_CHARS_PER_PAGE, ExtractedText, extract

from conftest import make_pdf

# Comfortably over the per-page threshold, so these pages read as real text.
DENSE = " ".join(["The council is asked to approve the agreement."] * 6)


class TestExtract:
    def test_reads_the_text_layer(self):
        result = extract(make_pdf(["Hello agenda item one"]))
        assert "Hello agenda item one" in result.text
        assert result.page_count == 1

    def test_pages_are_joined_and_counted(self):
        result = extract(make_pdf([DENSE, DENSE, DENSE]))
        assert result.page_count == 3
        assert result.text.count("The council is asked") == 18

    def test_a_dense_document_is_usable(self):
        assert extract(make_pdf([DENSE, DENSE])).usable

    def test_empty_pages_yield_nothing_usable(self):
        # A scan with no text layer: pages exist, text does not.
        result = extract(make_pdf(["", ""]))
        assert not result.usable
        assert result.page_count == 2

    def test_sparse_text_reads_as_a_scan(self):
        # A handful of stray characters over many pages is what OCR-less scans
        # look like when the PDF carries a stamp or a page number.
        result = extract(make_pdf(["x"] * 10))
        assert result.page_count == 10
        assert not result.has_text_layer

    def test_garbage_bytes_report_an_error(self):
        result = extract(b"this is not a pdf")
        assert result.error is not None
        assert not result.usable
        assert result.page_count == 0

    def test_empty_bytes(self):
        assert extract(b"").error is not None

    def test_no_budget_reads_everything(self):
        result = extract(make_pdf([DENSE, DENSE, DENSE]))
        assert not result.truncated


class TestTruncation:
    def test_stops_once_the_budget_is_reached(self):
        result = extract(make_pdf([DENSE, DENSE, DENSE]), char_budget=10)
        assert result.truncated
        assert result.text.count("The council is asked") == 6, "truncation is page-aligned"

    def test_page_count_still_reports_the_whole_document(self):
        result = extract(make_pdf([DENSE] * 5), char_budget=10)
        assert result.page_count == 5

    def test_not_truncated_when_the_budget_covers_everything(self):
        assert not extract(make_pdf([DENSE]), char_budget=1_000_000).truncated

    def test_budget_met_exactly_on_the_last_page_is_not_truncated(self):
        # Nothing follows, so there is nothing to warn the model about.
        pdf = make_pdf([DENSE, DENSE])
        full = extract(pdf).text
        assert not extract(pdf, char_budget=len(full)).truncated


class TestExtractedText:
    def test_usable_requires_both_text_and_a_layer(self):
        assert ExtractedText(text="x" * 100, page_count=1).usable
        assert not ExtractedText(text="   ", page_count=1).usable

    def test_an_error_is_never_usable(self):
        assert not ExtractedText(text="x" * 100, page_count=1, error="boom").usable

    def test_zero_pages_is_never_usable(self):
        assert not ExtractedText(text="x" * 100, page_count=0).usable

    @pytest.mark.parametrize("chars, pages, expected", [
        (MIN_CHARS_PER_PAGE * 2, 2, True),
        (MIN_CHARS_PER_PAGE * 2 - 1, 2, False),
        (MIN_CHARS_PER_PAGE * 100, 2, True),
    ])
    def test_the_threshold_is_an_average_over_the_document(self, chars, pages, expected):
        assert ExtractedText(text="x" * chars, page_count=pages).has_text_layer is expected
