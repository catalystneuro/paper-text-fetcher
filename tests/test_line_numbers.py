"""
Tests for margin line-number removal in PDF extraction.

Manuscripts under review carry line numbers in the margin, and PDF extraction
drops them inline, so a sentence comes out with integers scattered through it.
Detection is positional: a bare integer, in the margin, in a vertical band,
repeated down the page. A number inside a sentence fails all three tests.
"""

import pytest

from paper_text_fetcher.fetcher import (
    _is_bare_integer, _line_number_span_keys, LINE_NUMBER_MIN_PER_PAGE,
)


class Rect:
    """Stands in for a PyMuPDF page rectangle."""
    def __init__(self, x0=0, x1=600):
        self.x0, self.x1 = x0, x1
        self.width = x1 - x0


def page(spans):
    """Build a parsed-page dict from (text, x0) pairs, one span per line."""
    return {'blocks': [{'type': 0, 'lines': [
        {'spans': [{'text': t, 'bbox': (x, y * 12, x + 14, y * 12 + 10)}]}
        for y, (t, x) in enumerate(spans)]}]}


class TestIsBareInteger:
    @pytest.mark.parametrize('text', ['1', '42', ' 356 ', '9999'])
    def test_accepts_short_integers(self, text):
        assert _is_bare_integer(text) is True

    @pytest.mark.parametrize('text', ['', 'the', '12a', '3.5', '10000', '1,2'])
    def test_rejects_everything_else(self, text):
        assert _is_bare_integer(text) is False


class TestLineNumberDetection:
    def test_numbered_margin_is_detected(self):
        spans = [(str(300 + i), 30) for i in range(12)]
        keys = _line_number_span_keys(page(spans), Rect())
        assert len(keys) == 12

    def test_right_hand_margin_is_detected(self):
        spans = [(str(i), 585) for i in range(12)]
        assert len(_line_number_span_keys(page(spans), Rect())) == 12

    def test_numbers_in_the_text_column_are_kept(self):
        # Same digits, but sitting where the prose sits.
        spans = [(str(300 + i), 300) for i in range(12)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_too_few_marginal_digits_are_kept(self):
        spans = [(str(i), 30) for i in range(LINE_NUMBER_MIN_PER_PAGE - 1)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_scattered_marginal_digits_are_kept(self):
        # A figure label here, a page number there: marginal but not a column.
        spans = [('1', 20), ('7', 60), ('3', 95), ('12', 25), ('9', 70), ('4', 100)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_prose_is_never_flagged(self):
        spans = [('we recorded from 45 neurons', 30) for _ in range(12)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_a_page_with_no_text_is_safe(self):
        assert _line_number_span_keys({'blocks': []}, Rect()) == set()
