"""Pyzzel exception types."""


class PyzzelError(Exception):
    """Base exception for Pyzzel errors."""


class ModelDefinitionError(PyzzelError):
    """Raised when a model class is defined incorrectly."""


class NotConnectedError(PyzzelError):
    """Raised when a database operation is attempted without a connection pool."""

    def __init__(self):
        super().__init__("Not connected. Call pyzzel.connect(url) first.")


class AlreadyConnectedError(PyzzelError):
    """Raised when connect is called while a pool is already active."""

    def __init__(self):
        super().__init__("Already connected. Call pyzzel.disconnect() first.")


class NotFound(PyzzelError):
    """Raised when a model row no longer exists."""

    def __init__(self, table, pk_value):
        super().__init__(f"No row found in table {table!r} with primary key {pk_value!r}.")


class VersionConflict(PyzzelError):
    """Raised when a model row has a newer version than the loaded object."""

    def __init__(self, table, pk_value):
        super().__init__(f"Row in table {table!r} with primary key {pk_value!r} was changed by someone else.")


class QueryError(PyzzelError):
    """Raised when a query is invalid for its model."""


class MigrationError(PyzzelError):
    """Raised when a migration cannot be generated safely."""


class TooManyRowsError(QueryError):
    """Raised when an unbounded query exceeds its configured row limit."""
