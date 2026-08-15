"""
Tests for margin line-number removal in PDF extraction.

Manuscripts under review carry line numbers in the margin, and PDF extraction
drops them inline, so a sentence comes out with integers scattered through it.
Detection is positional: a bare integer, outside the text column, in a vertical
band, repeated down the page. A number inside a sentence fails the position
test, a stray figure label fails the band test, and a page with a couple of
marginal digits fails the count test.
"""

import pytest

from paper_text_fetcher.fetcher import (
    _is_bare_integer, _line_number_span_keys, LINE_NUMBER_MIN_PER_PAGE,
)

PROSE_X = 89.0      # where body text starts, as measured on a real preprint
MARGIN_X = 53.0     # where its line numbers sat


class Rect:
    def __init__(self, x0=0.0, x1=595.0):
        self.x0, self.x1 = x0, x1
        self.width = x1 - x0


PROSE_RIGHT = 520.0   # where the text column ends on a single-column page


def page(spans):
    """
    Build a parsed page from (text, x0) pairs, one span per line.

    Prose spans run the width of the text column, which is what lets the
    detector work out where the margin is. Giving them a token width would make
    anything to their right look like a right-hand margin.
    """
    lines = []
    for i, (text, x0) in enumerate(spans):
        x1 = x0 + 15.0 if text.strip().isdigit() else PROSE_RIGHT
        lines.append({'spans': [{'text': text,
                                 'bbox': (x0, i * 12.0, x1, i * 12.0 + 10.0)}]})
    return {'blocks': [{'type': 0, 'lines': lines}]}


def numbered_page(n=12, margin_x=MARGIN_X):
    """A page of prose with a numbered margin beside it."""
    spans = []
    for i in range(n):
        spans.append((str(300 + i), margin_x))
        spans.append((f'a line of body text number {i}', PROSE_X))
    return page(spans)


class TestIsBareInteger:
    @pytest.mark.parametrize('text', ['1', '42', ' 356 ', '9999'])
    def test_accepts_short_integers(self, text):
        assert _is_bare_integer(text) is True

    @pytest.mark.parametrize('text', ['', 'the', '12a', '3.5', '10000'])
    def test_rejects_everything_else(self, text):
        assert _is_bare_integer(text) is False


class TestLineNumberDetection:
    def test_numbered_margin_is_detected(self):
        assert len(_line_number_span_keys(numbered_page(), Rect())) == 12

    def test_margin_just_inside_a_tenth_of_the_page_is_detected(self):
        """
        The regression that motivated deriving the margin from the prose: a
        preprint whose line numbers sat at 10 to 12 percent of the page width
        was missed by a fixed one-tenth band.
        """
        assert len(_line_number_span_keys(numbered_page(margin_x=59.3), Rect())) == 12

    def test_right_hand_margin_is_detected(self):
        spans = []
        for i in range(12):
            spans.append((f'a line of body text number {i}', PROSE_X))
            spans.append((str(i), 560.0))
        assert len(_line_number_span_keys(page(spans), Rect())) == 12

    def test_numbers_inside_the_text_column_are_kept(self):
        spans = []
        for i in range(12):
            spans.append((str(300 + i), PROSE_X + 40))
            spans.append((f'a line of body text number {i}', PROSE_X))
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_too_few_marginal_digits_are_kept(self):
        spans = [(f'body text {i}', PROSE_X) for i in range(12)]
        spans += [(str(i), MARGIN_X) for i in range(LINE_NUMBER_MIN_PER_PAGE - 1)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_scattered_marginal_digits_are_kept(self):
        # A figure label here, a page number there: marginal but not a column.
        spans = [(f'body text {i}', PROSE_X) for i in range(12)]
        spans += [('1', 10.0), ('7', 25.0), ('3', 40.0), ('12', 15.0),
                  ('9', 30.0), ('4', 45.0)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_prose_alone_is_never_flagged(self):
        spans = [('we recorded from 45 neurons', PROSE_X) for _ in range(12)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_page_with_no_prose_is_safe(self):
        # Without a text column there is nothing to measure a margin against.
        spans = [(str(i), MARGIN_X) for i in range(12)]
        assert _line_number_span_keys(page(spans), Rect()) == set()

    def test_empty_page_is_safe(self):
        assert _line_number_span_keys({'blocks': []}, Rect()) == set()
