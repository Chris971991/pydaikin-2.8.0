"""Daikin exceptions."""


class DaikinException(Exception):
    """Daikin base exception class."""


class DaikinRejectedValueError(DaikinException):
    """Device rejected a value (rsc 4000)."""
