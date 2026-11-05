"""
Common exceptions related to Transactions.
"""


class TransactionException(Exception):
    """
    Base exception class around transactions.
    """


class TransactionFulfillmentCancelationException(TransactionException):
    """
    Raised when a Transaction cannot be unfulfilled (un-enrolled).
    """


class SplitEnrollmentWriteOnceException(TransactionException):
    """
    Raised when a write-once split enrollment value is modified.
    """
