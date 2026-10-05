"""
Tests for the pure CSV-rendering helpers in ``enterprise_subsidy.apps.api.csv_exports``.

These need no database or JWT setup, so they live here rather than in the view test suite.
"""
import ddt
from django.test import TestCase

from enterprise_subsidy.apps.api.csv_exports import escape_formula


@ddt.ddt
class EscapeFormulaTests(TestCase):
    """
    A cell a spreadsheet would evaluate as a formula must be rendered as text instead.
    https://owasp.org/www-community/attacks/CSV_Injection
    """

    @ddt.data(
        ('=1+1', "'=1+1"),
        ('+1', "'+1"),
        ('-1', "'-1"),
        ('@A1', "'@A1"),
        ('\tx', "'\tx"),
        ('\rx', "'\rx"),
        ('safe=text', 'safe=text'),
        ('', ''),
        (None, None),
        (42, 42),
    )
    @ddt.unpack
    def test_escape_formula(self, value, expected):
        assert escape_formula(value) == expected
