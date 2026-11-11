"""Exceptions the ``Client`` contract adds to Python's built-in ones."""


class FileTooLargeError(ValueError):
    """The file is larger than the provider accepts in one upload.

    It is a ``ValueError``, so code that handles invalid input in general still catches it.
    """
