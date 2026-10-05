"""
Streams Learner Credit spend (transactions) as CSV rows, for the admin "Spent" report download.
"""
import csv
from decimal import Decimal

from openedx_ledger.models import TransactionStateChoices, UnitChoices

# Columns holding text from learners or catalog metadata, which a spreadsheet could otherwise run as a formula.
UNTRUSTED_TEXT_COLUMNS = {'Learner Email', 'Course Title', 'Course Key'}

# https://owasp.org/www-community/attacks/CSV_Injection
FORMULA_TRIGGER_CHARACTERS = ('=', '+', '-', '@', '\t', '\r')

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
    """
    spent = -transaction.quantity
    unit = transaction.ledger.unit
    if unit == UnitChoices.USD_CENTS:
        return f'{Decimal(spent) / 100:.2f}', 'USD'
    if unit == UnitChoices.SEATS:
        return str(spent), 'Seats'
    # Not a unit openedx-ledger defines today; report the raw quantity rather than guess at a conversion.
    return str(spent), unit


def get_spend_status(transaction):
    """
    A committed reversal means the spend was refunded, so report it as such (as the admin portal's Spent table does).
    """
    # getattr() with a default handles the RelatedObjectDoesNotExist raised when there is no reversal.
    reversal = getattr(transaction, 'reversal', None)
    if reversal and reversal.state == TransactionStateChoices.COMMITTED:
        return 'Refunded'
    return transaction.state.capitalize()


# Column header -> function returning that column's (unescaped) value for a transaction.
SPEND_REPORT_COLUMNS = {
    'Learner Email': lambda transaction: transaction.lms_user_email,
    'Learner ID': lambda transaction: transaction.lms_user_id,
    'Course Title': lambda transaction: transaction.content_title,
    'Course Key': lambda transaction: transaction.content_key,
    'Date Spent (UTC)': lambda transaction: transaction.created.strftime('%Y-%m-%d %H:%M:%S'),
    'Amount Spent': lambda transaction: format_amount_spent(transaction)[0],
    'Unit': lambda transaction: format_amount_spent(transaction)[1],
    'Status': get_spend_status,
    'Policy UUID': lambda transaction: transaction.subsidy_access_policy_uuid,
}


class _Echo:
    """
    A file-like object whose ``write()`` returns the value, so ``csv.writer`` can produce one row at a time.
    """
    def write(self, value):
        return value


def iter_spend_report_csv(transactions):
    """
    Yields the spend report as CSV text, one row at a time, so it can be streamed without holding it in memory.
    """
    writer = csv.writer(_Echo())
    yield UTF8_BOM + writer.writerow(SPEND_REPORT_COLUMNS.keys())
    for transaction in transactions:
        yield writer.writerow(
            escape_formula(get_value(transaction)) if column in UNTRUSTED_TEXT_COLUMNS else get_value(transaction)
            for column, get_value in SPEND_REPORT_COLUMNS.items()
        )
