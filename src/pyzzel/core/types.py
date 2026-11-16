"""Column types supported by Pyzzel models."""


class ColumnType:
    """Small common interface for PostgreSQL column types."""

    sql_type = None

    def to_db(self, value):
        return value


class SmallInteger(ColumnType):
    sql_type = "SMALLINT"


class Integer(ColumnType):
    """PostgreSQL integer column type."""

    sql_type = "INTEGER"


class BigInteger(ColumnType):
    sql_type = "BIGINT"


class Float(ColumnType):
    sql_type = "DOUBLE PRECISION"


class Numeric(ColumnType):
    def __init__(self, precision=None, scale=None):
        if precision is not None and (type(precision) is not int or precision <= 0):
            raise ValueError("Numeric precision must be a positive integer")
        if scale is not None and (type(scale) is not int or scale < 0):
            raise ValueError("Numeric scale must be a non-negative integer")
        if scale is not None and precision is None:
            raise ValueError("Numeric scale requires precision")
        if precision is not None and scale is not None and scale > precision:
            raise ValueError("Numeric scale cannot exceed precision")
        if precision is None:
            self.sql_type = "NUMERIC"
        elif scale is None:
            self.sql_type = f"NUMERIC({precision})"
        else:
            self.sql_type = f"NUMERIC({precision},{scale})"


class Boolean(ColumnType):
    """PostgreSQL boolean column type."""

    sql_type = "BOOLEAN"


class Date(ColumnType):
    sql_type = "DATE"


class Time(ColumnType):
    sql_type = "TIME"


class DateTime(ColumnType):
    """PostgreSQL timestamp column type."""

    sql_type = "TIMESTAMPTZ"

    def __init__(self, timezone=True):
        self.sql_type = "TIMESTAMPTZ" if timezone else "TIMESTAMP"


class UUID(ColumnType):
    sql_type = "UUID"


class JSON(ColumnType):
    sql_type = "JSONB"

    def to_db(self, value):
        from psycopg.types.json import Jsonb

        return Jsonb(value)


class LargeBinary(ColumnType):
    sql_type = "BYTEA"


class String(ColumnType):
    """PostgreSQL text or bounded varchar column type."""

    def __init__(self, length=None):
        if length is not None and (type(length) is not int or length <= 0):
            raise ValueError("String length must be a positive integer")
        self.sql_type = "TEXT" if length is None else f"VARCHAR({length})"
