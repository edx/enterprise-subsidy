"""
Streams Learner Credit spend (transactions) as CSV rows, for the admin "Spent" report download.
"""
import csv
from decimal import Decimal

from openedx_ledger.models import TransactionStateChoices, UnitChoices

from enterprise_subsidy.apps.subsidy.constants import CENTS_PER_DOLLAR

# https://owasp.org/www-community/attacks/CSV_Injection
FORMULA_TRIGGER_CHARACTERS = ('=', '+', '-', '@', '\t', '\r', '\n')

# Excel only detects UTF-8 (rather than the system code page) when the file starts with a byte order mark.
UTF8_BOM = '\ufeff'


def escape_formula(value):
    """
    Prefix a cell that a spreadsheet would evaluate as a formula with a single quote, so it's shown as text.
    """
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGER_CHARACTERS):
        return "'" + value
    return value


def format_amount_spent(transaction):
    """
    Returns (amount, unit label) for a spend transaction.

    Spend is recorded as a negative quantity, so it is negated rather than ``abs()``-ed: an unexpected positive
    quantity then shows up as a negative amount instead of being hidden.

    ``UnitChoices`` defines only ``usd_cents`` and ``seats``, and ``Ledger.unit`` defaults to ``usd_cents``, so
    there is no third case to handle here.
    """
    spent = -transaction.quantity
    if transaction.ledger.unit == UnitChoices.SEATS:
        return str(spent), 'Seats'
    return f'{Decimal(spent) / CENTS_PER_DOLLAR:.2f}', 'USD'


def get_spend_status(transaction):
    """
    A committed reversal means the spend was refunded, so report it as such (as the admin portal's Spent table does).
    """
    # getattr() with a default handles the RelatedObjectDoesNotExist raised when there is no reversal.
    reversal = getattr(transaction, 'reversal', None)
    if reversal and reversal.state == TransactionStateChoices.COMMITTED:
        return 'Refunded'
    return transaction.state.capitalize()


SPEND_REPORT_HEADERS = (
    'Learner Email',
    'Learner ID',
    'Course Title',
    'Course Key',
    'Date Spent (UTC)',
    'Amount Spent',
    'Unit',
    'Status',
    'Policy UUID',
)


def spend_report_row(transaction):
    """
    Returns one transaction's cells, in ``SPEND_REPORT_HEADERS`` order, with untrusted text escaped.
    """
    amount, unit = format_amount_spent(transaction)
    return (
        escape_formula(transaction.lms_user_email),
        transaction.lms_user_id,
        escape_formula(transaction.content_title),
        escape_formula(transaction.content_key),
        transaction.created.strftime('%Y-%m-%d %H:%M:%S'),
        amount,
        unit,
        get_spend_status(transaction),
        transaction.subsidy_access_policy_uuid,
    )


class _Echo:
    """
    A file-like object whose ``write()`` returns the value, so ``csv.writer`` can produce one row at a time.
    """
    def write(self, value):
        return value


def iter_spend_report_csv(transactions):
    """
    Yields the spend report as CSV text, one row at a time, so the response can start before the whole report
    has been rendered.
    """
    writer = csv.writer(_Echo())
    yield UTF8_BOM + writer.writerow(SPEND_REPORT_HEADERS)
    for transaction in transactions:
        yield writer.writerow(spend_report_row(transaction))
