"""Render offline schema operations as PostgreSQL DDL."""

from pyzzel.sql import (
    ColumnSpec, check_default_expression, column_definition, create_index,
    create_table, index_name, quote_ident,
)
from .diff import (
    AddColumn, AddForeignKey, AddIndex, AddUnique, AlterType, CreateTable,
    DropColumn, DropDefault, DropForeignKey, DropIndex, DropNotNull,
    DropTable, DropUnique, SetDefault, SetNotNull,
)


def _constraint(table, column, suffix):
    name = f"{table}_{column}_{suffix}"
    return name[:63]


def _spec(name, column):
    return ColumnSpec(
        name, column["type"].upper(), primary_key=column.get("primary_key", False),
        identity=column.get("identity", False), nullable=column.get("nullable", True),
        unique=column.get("unique", False), default=column.get("default"),
        foreign_key=tuple(column["references"]) if column.get("references") else None,
        on_delete=column.get("on_delete"),
    )


def render(ops):
    statements = []
    for op in ops:
        marker = None
        if isinstance(op, CreateTable):
            specs = [_spec(name, column) for name, column in op.columns.items()]
            statements.append(create_table(op.table, specs) + ";"); continue
        if isinstance(op, DropTable):
            marker, sql = "-- pyzzel:destructive", f"DROP TABLE IF EXISTS {quote_ident(op.table)}"
        elif isinstance(op, AddColumn):
            definition = column_definition(_spec(op.name, op.column))
            if not op.column.get("nullable", True) and op.column.get("default") is None:
                marker = "-- pyzzel:warning NOT NULL column without a default fails if the table already has rows"
            sql = f"ALTER TABLE {quote_ident(op.table)} ADD COLUMN IF NOT EXISTS {definition}"
        elif isinstance(op, DropColumn):
            marker, sql = "-- pyzzel:destructive", f"ALTER TABLE {quote_ident(op.table)} DROP COLUMN IF EXISTS {quote_ident(op.name)}"
        elif isinstance(op, AlterType):
            marker = "-- pyzzel:destructive"
            definition = column_definition(ColumnSpec("column", op.new.upper()))
            sql_type = definition[len('"column" '):]
            sql = f"ALTER TABLE {quote_ident(op.table)} ALTER COLUMN {quote_ident(op.name)} TYPE {sql_type}"
        elif isinstance(op, SetNotNull):
            marker = "-- pyzzel:warning fails if the column contains NULL values"
            sql = f"ALTER TABLE {quote_ident(op.table)} ALTER COLUMN {quote_ident(op.name)} SET NOT NULL"
        elif isinstance(op, DropNotNull):
            sql = f"ALTER TABLE {quote_ident(op.table)} ALTER COLUMN {quote_ident(op.name)} DROP NOT NULL"
        elif isinstance(op, SetDefault):
            sql = f"ALTER TABLE {quote_ident(op.table)} ALTER COLUMN {quote_ident(op.name)} SET DEFAULT {check_default_expression(op.default)}"
        elif isinstance(op, DropDefault):
            sql = f"ALTER TABLE {quote_ident(op.table)} ALTER COLUMN {quote_ident(op.name)} DROP DEFAULT"
        elif isinstance(op, AddUnique):
            sql = f"ALTER TABLE {quote_ident(op.table)} ADD CONSTRAINT {quote_ident(_constraint(op.table, op.name, 'key'))} UNIQUE ({quote_ident(op.name)})"
        elif isinstance(op, DropUnique):
            sql = f"ALTER TABLE {quote_ident(op.table)} DROP CONSTRAINT IF EXISTS {quote_ident(_constraint(op.table, op.name, 'key'))}"
        elif isinstance(op, AddForeignKey):
            ref_table, ref_column = op.references
            if op.on_delete not in ("NO ACTION", "RESTRICT", "CASCADE", "SET NULL"):
                raise ValueError(f"Invalid ON DELETE action: {op.on_delete!r}")
            sql = (f"ALTER TABLE {quote_ident(op.table)} ADD CONSTRAINT {quote_ident(_constraint(op.table, op.name, 'fkey'))} "
                   f"FOREIGN KEY ({quote_ident(op.name)}) REFERENCES {quote_ident(ref_table)} ({quote_ident(ref_column)})")
            if op.on_delete != "NO ACTION": sql += f" ON DELETE {op.on_delete}"
        elif isinstance(op, DropForeignKey):
            sql = f"ALTER TABLE {quote_ident(op.table)} DROP CONSTRAINT IF EXISTS {quote_ident(_constraint(op.table, op.name, 'fkey'))}"
        elif isinstance(op, AddIndex):
            statements.append(create_index(op.table, op.name) + ";"); continue
        elif isinstance(op, DropIndex):
            sql = f"DROP INDEX IF EXISTS {quote_ident(index_name(op.table, op.name))}"
        else:
            raise TypeError(f"Unsupported migration operation: {op!r}")
        statements.append((marker + "\n" if marker else "") + sql + ";")
    return "\n\n".join(statements)
