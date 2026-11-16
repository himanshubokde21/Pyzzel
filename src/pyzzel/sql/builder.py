"""Pure PostgreSQL SQL statement builders."""

import re
from collections import namedtuple
from typing import NamedTuple, Sequence

from .conditions import Condition, OrderBy


class Statement(NamedTuple):
    """SQL text and its ordered parameter values."""

    sql: str
    params: tuple


class ColumnRef(NamedTuple):
    table: str
    name: str
    alias: str | None = None


class Join(namedtuple(
    "Join", ("kind", "table", "left_table", "left_column", "right_table", "right_column")
)):
    __slots__ = ()

    def __new__(cls, kind, table, left_table, left_column, right_table, right_column):
        if kind not in ("INNER", "LEFT"):
            raise ValueError(f"Invalid join kind: {kind!r}")
        return super().__new__(cls, kind, table, left_table, left_column, right_table, right_column)


class ColumnSpec(NamedTuple):
    """Column definition; default is a trusted Pyzzel SQL expression, never user input."""

    name: str
    sql_type: str
    primary_key: bool = False
    identity: bool = False
    nullable: bool = True
    unique: bool = False
    default: str | None = None
    foreign_key: tuple[str, str] | None = None
    on_delete: str | None = None


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SQL_TYPE = re.compile(r"^[A-Z][A-Z0-9_ ]*(\(\d+(,\s*\d+)?\))?$")


def quote_ident(name: str) -> str:
    """Validate and double-quote a PostgreSQL identifier."""
    if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"Invalid identifier: {name!r}")
    return f'"{name}"'


def check_default_expression(expression):
    """Trusted text written by the developer, never user input."""
    if (
        not isinstance(expression, str)
        or not expression.strip()
        or any(token in expression for token in (";", "--", "/*"))
    ):
        raise ValueError("Invalid DEFAULT expression")
    return expression


def insert(table, values: dict, returning: Sequence[str] | None = None) -> Statement:
    """Build a parameterized INSERT statement."""
    table_sql = quote_ident(table)
    returning_sql = "*" if returning is None else ", ".join(quote_ident(name) for name in returning)
    if not values:
        return Statement(f"INSERT INTO {table_sql} DEFAULT VALUES RETURNING {returning_sql}", ())
    names = [quote_ident(name) for name in values]
    placeholders = ", ".join(["%s"] * len(values))
    sql = f"INSERT INTO {table_sql} ({', '.join(names)}) VALUES ({placeholders}) RETURNING {returning_sql}"
    return Statement(sql, tuple(values.values()))


def insert_many(
    table,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    returning: Sequence[str] | None = None,
) -> Statement:
    """Build one parameterized multi-row INSERT statement."""
    if not columns:
        raise ValueError("INSERT requires at least one column")
    if not rows:
        raise ValueError("INSERT requires at least one row")
    width = len(columns)
    if any(len(row) != width for row in rows):
        raise ValueError("Every INSERT row must match the column count")
    columns_sql = ", ".join(quote_ident(name) for name in columns)
    row_sql = "(" + ", ".join(["%s"] * width) + ")"
    values_sql = ", ".join([row_sql] * len(rows))
    returning_sql = "*" if returning is None else ", ".join(
        quote_ident(name) for name in returning
    )
    sql = (
        f"INSERT INTO {quote_ident(table)} ({columns_sql}) "
        f"VALUES {values_sql} RETURNING {returning_sql}"
    )
    return Statement(sql, tuple(value for row in rows for value in row))


def select_by_pk(table, columns: Sequence[str], pk_column: str, pk_value) -> Statement:
    """Build a parameterized SELECT by primary key."""
    if not columns:
        raise ValueError("SELECT requires at least one column")
    columns_sql = ", ".join(quote_ident(column) for column in columns)
    return Statement(
        f"SELECT {columns_sql} FROM {quote_ident(table)} WHERE {quote_ident(pk_column)} = %s",
        (pk_value,),
    )


def _validate_limit(value, label):
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"{label} must be an int greater than or equal to 0")


def _joins_sql(joins):
    sql = ""
    for join in joins:
        if not isinstance(join, Join):
            raise ValueError("joins must contain Join values")
        if join.kind not in ("INNER", "LEFT"):
            raise ValueError(f"Invalid join kind: {join.kind!r}")
        sql += (
            f" {join.kind} JOIN {quote_ident(join.table)} ON "
            f"{quote_ident(join.left_table)}.{quote_ident(join.left_column)} = "
            f"{quote_ident(join.right_table)}.{quote_ident(join.right_column)}"
        )
    return sql


def select(
    table,
    columns: Sequence[str],
    where: Condition | None = None,
    order_by: Sequence[OrderBy] = (),
    limit: int | None = None,
    offset: int | None = None,
    joins=(),
    distinct=False,
) -> Statement:
    """Build a parameterized SELECT statement."""
    if not columns:
        raise ValueError("SELECT requires at least one column")
    _validate_limit(limit, "limit")
    _validate_limit(offset, "offset")
    joined = bool(joins)
    column_sql = []
    for column in columns:
        if isinstance(column, ColumnRef):
            table_sql = quote_ident(column.table)
            if joined:
                rendered = f"{table_sql}.{quote_ident(column.name)}"
            else:
                rendered = quote_ident(column.name)
            if column.alias is not None:
                rendered += f" AS {quote_ident(column.alias)}"
            column_sql.append(rendered)
        elif isinstance(column, str) and not joined:
            column_sql.append(quote_ident(column))
        else:
            raise ValueError("With joins, columns must be ColumnRef values")
    select_prefix = "SELECT DISTINCT" if distinct else "SELECT"
    sql = f"{select_prefix} {', '.join(column_sql)} FROM {quote_ident(table)}"
    sql += _joins_sql(joins)
    params = ()
    if where is not None:
        if not isinstance(where, Condition):
            raise TypeError("where must be a Condition")
        where_sql, where_params = where.render(qualify=joined)
        sql += f" WHERE {where_sql}"
        params += where_params
    if order_by:
        order_terms = []
        for item in order_by:
            if not isinstance(item, OrderBy):
                raise ValueError("order_by items must be OrderBy values")
            if item.direction not in ("ASC", "DESC"):
                raise ValueError("order_by direction must be ASC or DESC")
            if joined:
                if item.table is None:
                    raise ValueError("Joined ORDER BY columns need a table")
                order_terms.append(
                    f"{quote_ident(item.table)}.{quote_ident(item.column)} {item.direction}"
                )
            else:
                order_terms.append(f"{quote_ident(item.column)} {item.direction}")
        sql += " ORDER BY " + ", ".join(order_terms)
    if limit is not None:
        sql += " LIMIT %s"
        params += (limit,)
    if offset is not None:
        sql += " OFFSET %s"
        params += (offset,)
    return Statement(sql, params)


def count(table, where: Condition | None = None, joins=()) -> Statement:
    """Build a parameterized COUNT(*) statement."""
    sql = f"SELECT COUNT(*) FROM {quote_ident(table)}"
    joined = bool(joins)
    sql += _joins_sql(joins)
    params = ()
    if where is not None:
        if not isinstance(where, Condition):
            raise TypeError("where must be a Condition")
        where_sql, params = where.render(qualify=joined)
        sql += f" WHERE {where_sql}"
    return Statement(sql, params)


def update(
    table,
    changes: dict,
    pk_column: str,
    pk_value,
    version_column: str,
    version_value,
    returning: Sequence[str] | None = None,
) -> Statement:
    """Build a version-checked parameterized UPDATE."""
    if not changes:
        raise ValueError("UPDATE requires at least one changed column")
    if pk_column in changes or version_column in changes:
        raise ValueError("Changes cannot include the primary key or version column")
    table_sql = quote_ident(table)
    pk_sql = quote_ident(pk_column)
    version_sql = quote_ident(version_column)
    assignments = [f"{quote_ident(name)} = %s" for name in changes]
    assignments.append(f'{version_sql} = {version_sql} + 1')
    returning_sql = "*" if returning is None else ", ".join(quote_ident(name) for name in returning)
    sql = (
        f"UPDATE {table_sql} SET {', '.join(assignments)} WHERE {pk_sql} = %s "
        f"AND {version_sql} = %s RETURNING {returning_sql}"
    )
    return Statement(sql, (*changes.values(), pk_value, version_value))


def update_where(table, changes, where, version_column="version") -> Statement:
    """Build a parameterized UPDATE for rows matching a condition."""
    if not changes:
        raise ValueError("UPDATE requires at least one changed column")
    if where is None:
        raise ValueError("UPDATE requires a condition")
    if not isinstance(where, Condition):
        raise TypeError("where must be a Condition")
    if version_column in changes:
        raise ValueError("Changes cannot include the version column")
    table_sql = quote_ident(table)
    assignments = [f"{quote_ident(name)} = %s" for name in changes]
    version_sql = quote_ident(version_column)
    assignments.append(f'{version_sql} = {version_sql} + 1')
    where_sql, where_params = where.render()
    return Statement(
        f"UPDATE {table_sql} SET {', '.join(assignments)} WHERE {where_sql}",
        (*changes.values(), *where_params),
    )


def delete_where(table, where) -> Statement:
    """Build a parameterized DELETE for rows matching a condition."""
    if where is None:
        raise ValueError("DELETE requires a condition")
    if not isinstance(where, Condition):
        raise TypeError("where must be a Condition")
    where_sql, params = where.render()
    return Statement(f"DELETE FROM {quote_ident(table)} WHERE {where_sql}", params)


def delete(table, pk_column, pk_value, version_column=None, version_value=None) -> Statement:
    """Build a parameterized DELETE, optionally guarded by a version."""
    if (version_column is None) != (version_value is None):
        raise ValueError("version_column and version_value must be provided together")
    sql = f"DELETE FROM {quote_ident(table)} WHERE {quote_ident(pk_column)} = %s"
    params = (pk_value,)
    if version_column is not None:
        version_sql = quote_ident(version_column)
        sql += f" AND {version_sql} = %s"
        params += (version_value,)
    return Statement(sql, params)


def create_table(table, columns: Sequence[ColumnSpec]) -> str:
    """Build CREATE TABLE SQL from validated column specifications."""
    if not columns:
        raise ValueError("CREATE TABLE requires at least one column")
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ValueError("Column names must be unique")
    if sum(column.primary_key for column in columns) != 1:
        raise ValueError("Exactly one primary key is required")

    definitions = [column_definition(column) for column in columns]
    return f'CREATE TABLE IF NOT EXISTS {quote_ident(table)} ({", ".join(definitions)})'


def column_definition(column: ColumnSpec) -> str:
    """Build one validated SQL column definition."""
    name_sql = quote_ident(column.name)
    if not isinstance(column.sql_type, str) or _SQL_TYPE.fullmatch(column.sql_type) is None:
        raise ValueError(f"Invalid SQL type: {column.sql_type!r}")
    if column.identity and not column.primary_key:
        raise ValueError("Identity requires primary_key=True")
    parts = [name_sql, column.sql_type]
    if column.identity:
        parts.append("GENERATED ALWAYS AS IDENTITY")
    if column.primary_key:
        parts.append("PRIMARY KEY")
    elif not column.nullable:
        parts.append("NOT NULL")
    if column.unique:
        parts.append("UNIQUE")
    if column.default is not None:
        parts.extend(("DEFAULT", check_default_expression(column.default)))
    if column.foreign_key is not None:
        if not isinstance(column.foreign_key, tuple) or len(column.foreign_key) != 2:
            raise ValueError("foreign_key must be a (table, column) pair")
        ref_table, ref_column = column.foreign_key
        parts.extend(("REFERENCES", f"{quote_ident(ref_table)} ({quote_ident(ref_column)})"))
        on_delete = column.on_delete or "NO ACTION"
        if on_delete not in ("NO ACTION", "RESTRICT", "CASCADE", "SET NULL"):
            raise ValueError(f"Invalid ON DELETE action: {on_delete!r}")
        if on_delete != "NO ACTION":
            parts.extend(("ON DELETE", on_delete))
    elif column.on_delete not in (None, "NO ACTION"):
        raise ValueError("on_delete requires foreign_key")
    return " ".join(parts)


def index_name(table, column) -> str:
    """Return the deterministic PostgreSQL-safe name for an index."""
    quote_ident(table)
    quote_ident(column)
    key = f"{table}_{column}"
    name = f"ix_{key}"
    if len(name) > 63:
        import zlib

        name = f"ix_{key[:40]}_{zlib.crc32(key.encode('utf-8')):08x}"
    return name


def create_index(table, column) -> str:
    """Build an idempotent index statement with a PostgreSQL-safe name."""
    table_sql = quote_ident(table)
    column_sql = quote_ident(column)
    return f"CREATE INDEX IF NOT EXISTS {quote_ident(index_name(table, column))} ON {table_sql} ({column_sql})"
