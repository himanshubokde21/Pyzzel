"""Deterministic offline schema differences."""

from typing import NamedTuple

from pyzzel.errors import MigrationError
from .schema import canonical_type, normalize_default


class CreateTable(NamedTuple):
    table: str
    columns: dict
    indexes: list


class DropTable(NamedTuple):
    table: str


class AddColumn(NamedTuple):
    table: str
    name: str
    column: dict


class DropColumn(NamedTuple):
    table: str
    name: str


class AlterType(NamedTuple):
    table: str
    name: str
    old: str
    new: str


class SetNotNull(NamedTuple):
    table: str
    name: str


class DropNotNull(NamedTuple):
    table: str
    name: str


class SetDefault(NamedTuple):
    table: str
    name: str
    default: str


class DropDefault(NamedTuple):
    table: str
    name: str


class AddUnique(NamedTuple):
    table: str
    name: str


class DropUnique(NamedTuple):
    table: str
    name: str


class AddForeignKey(NamedTuple):
    table: str
    name: str
    references: list
    on_delete: str


class DropForeignKey(NamedTuple):
    table: str
    name: str


class AddIndex(NamedTuple):
    table: str
    name: str


class DropIndex(NamedTuple):
    table: str
    name: str


def describe(op, old_name, new_name):
    """Return one readable line describing a schema operation."""
    if isinstance(op, CreateTable): return f"table {op.table}: only in {new_name}"
    if isinstance(op, DropTable): return f"table {op.table}: only in {old_name}"
    if isinstance(op, AddColumn): return f"column {op.table}.{op.name}: only in {new_name}"
    if isinstance(op, DropColumn): return f"column {op.table}.{op.name}: only in {old_name}"
    if isinstance(op, AlterType): return f"column {op.table}.{op.name}: type differs ({old_name} {op.old}, {new_name} {op.new})"
    if isinstance(op, SetNotNull): return f"column {op.table}.{op.name}: NOT NULL in {new_name} but nullable in {old_name}"
    if isinstance(op, DropNotNull): return f"column {op.table}.{op.name}: nullable in {new_name} but NOT NULL in {old_name}"
    if isinstance(op, SetDefault): return f"column {op.table}.{op.name}: default differs ({new_name} has {op.default})"
    if isinstance(op, DropDefault): return f"column {op.table}.{op.name}: default only in {old_name}"
    if isinstance(op, AddUnique): return f"column {op.table}.{op.name}: unique only in {new_name}"
    if isinstance(op, DropUnique): return f"column {op.table}.{op.name}: unique only in {old_name}"
    if isinstance(op, AddForeignKey):
        return f"column {op.table}.{op.name}: foreign key to {op.references[0]}.{op.references[1]} (ON DELETE {op.on_delete}) only in {new_name}"
    if isinstance(op, DropForeignKey): return f"column {op.table}.{op.name}: foreign key only in {old_name}"
    if isinstance(op, AddIndex): return f"column {op.table}.{op.name}: index only in {new_name}"
    if isinstance(op, DropIndex): return f"column {op.table}.{op.name}: index only in {old_name}"
    raise TypeError(f"Unsupported schema operation: {type(op).__name__}")


def _order_tables(tables, snapshots, reverse=False):
    result, visiting, visited = [], set(), set()

    def visit(table):
        if table in visited:
            return
        if table in visiting:
            raise MigrationError("Circular foreign key dependency between new tables")
        visiting.add(table)
        for column in snapshots[table]["columns"].values():
            ref = column.get("references")
            if ref and ref[0] in tables and ref[0] != table:
                visit(ref[0])
        visiting.remove(table); visited.add(table); result.append(table)

    for table in tables:
        visit(table)
    return list(reversed(result)) if reverse else result


def diff(old, new):
    before, after = old["tables"], new["tables"]
    drop_fk, drop_unique, drop_index, drop_columns = [], [], [], []
    creates, add_columns, types, defaults, nulls = [], [], [], [], []
    add_unique, add_fk, add_index, drop_tables = [], [], [], []
    for table, previous in before.items():
        if table not in after:
            continue
        current = after[table]
        old_cols, new_cols = previous["columns"], current["columns"]
        for name, column in old_cols.items():
            if name not in new_cols:
                drop_columns.append(DropColumn(table, name)); continue
            updated = new_cols[name]
            if column.get("primary_key") != updated.get("primary_key") or column.get("identity") != updated.get("identity"):
                raise MigrationError(f"changing the primary key or identity of {table}.{name} is not supported; write the migration by hand")
            if column.get("references") != updated.get("references") or column.get("on_delete") != updated.get("on_delete"):
                if column.get("references"):
                    drop_fk.append(DropForeignKey(table, name))
                if updated.get("references"):
                    add_fk.append(AddForeignKey(table, name, updated["references"], updated.get("on_delete") or "NO ACTION"))
            if column.get("unique") != updated.get("unique"):
                (add_unique if updated.get("unique") else drop_unique).append((AddUnique if updated.get("unique") else DropUnique)(table, name))
            old_index, new_index = name in previous.get("indexes", []), name in current.get("indexes", [])
            if old_index != new_index:
                (add_index if new_index else drop_index).append((AddIndex if new_index else DropIndex)(table, name))
            if canonical_type(column["type"]) != canonical_type(updated["type"]):
                types.append(AlterType(table, name, column["type"], updated["type"]))
            if normalize_default(column.get("default")) != normalize_default(updated.get("default")):
                if updated.get("default") is None: defaults.append(DropDefault(table, name))
                else: defaults.append(SetDefault(table, name, updated["default"]))
            if column.get("nullable", True) != updated.get("nullable", True):
                nulls.append((DropNotNull if updated.get("nullable") else SetNotNull)(table, name))
        for name, column in new_cols.items():
            if name not in old_cols:
                if column.get("primary_key"):
                    raise MigrationError(f"adding primary key column {table}.{name} is not supported")
                add_columns.append(AddColumn(table, name, column))
                if name in current.get("indexes", []):
                    add_index.append(AddIndex(table, name))
    for table, value in after.items():
        if table not in before:
            creates.append(table)
            for name in value.get("indexes", []):
                add_index.append(AddIndex(table, name))
    create_ops = [CreateTable(t, after[t]["columns"], after[t].get("indexes", [])) for t in _order_tables(creates, after)]
    for table in _order_tables([t for t in before if t not in after], before, reverse=True):
        drop_tables.append(DropTable(table))
    return (drop_fk + drop_unique + drop_index + drop_columns + create_ops + add_columns
            + types + defaults + nulls + add_unique + add_fk + add_index + drop_tables)
