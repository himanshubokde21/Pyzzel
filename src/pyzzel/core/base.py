"""Base class for Pyzzel models."""

from pyzzel.core.column import Column, Labeled, _VersionColumn
from pyzzel.core.query import Query, _validate_condition_columns
from pyzzel.core.types import BigInteger, Integer, SmallInteger
from pyzzel.errors import ModelDefinitionError, NotFound, PyzzelError, QueryError, VersionConflict
from pyzzel import pool
from pyzzel.pool import connection
from pyzzel.sql import create_table as build_create_table, create_index as build_create_index
from pyzzel.sql import delete as build_delete
from pyzzel.sql import insert as build_insert
from pyzzel.sql import (
    delete_where as build_delete_where,
    select_by_pk,
    update as build_update,
    update_where as build_update_where,
)
from pyzzel.sql.builder import ColumnSpec, quote_ident
from pyzzel.sql.conditions import Condition, and_


_RESERVED = {
    "get",
    "save",
    "create",
    "delete",
    "select",
    "where",
    "transaction",
    "execute",
    "create_tables",
    "update",
    "with_",
    "version",
    "max_rows",
}


def _classify_zero_rows(conn, table, pk_column, pk_value):
    """Raise the appropriate model error after checking existence on this connection."""
    statement = select_by_pk(table, [pk_column], pk_column, pk_value)
    cursor = conn.execute(statement.sql, statement.params)
    if cursor.fetchone() is None:
        raise NotFound(table, pk_value)
    raise VersionConflict(table, pk_value)


class Base:
    """Base class for model definitions and in-memory row data."""

    __abstract__ = True
    max_rows: int | None = 10_000
    _registry: dict[str, type] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__", False):
            return

        if "__tablename__" not in cls.__dict__ or not isinstance(cls.__dict__["__tablename__"], str):
            raise ModelDefinitionError("Concrete models must define a string __tablename__")
        try:
            quote_ident(cls.__dict__["__tablename__"])
        except ValueError as error:
            raise ModelDefinitionError(f"Invalid table name: {error}") from error

        if any(parent is not Base and issubclass(parent, Base) and not parent.__dict__.get("__abstract__", False)
               for parent in cls.__mro__[1:]):
            raise ModelDefinitionError("Subclassing a concrete model is not supported")

        columns = {}
        for parent in reversed(cls.__mro__):
            if not issubclass(parent, Base):
                continue
            for name, value in parent.__dict__.items():
                if isinstance(value, Column):
                    columns[name] = value

        table_name = cls.__dict__["__tablename__"]
        for name, column in tuple(columns.items()):
            if name not in cls.__dict__:
                column = column._clone()
                setattr(cls, name, column)
                columns[name] = column
            column.table = table_name

        primary_keys = [column for column in columns.values() if column.primary_key]
        if len(primary_keys) != 1:
            raise ModelDefinitionError("A model must define exactly one primary key column")
        pk_name = next(name for name, column in columns.items() if column.primary_key)
        if not isinstance(primary_keys[0].type, (SmallInteger, Integer, BigInteger)):
            raise ModelDefinitionError(
                "The primary key must be an integer type (SmallInteger, Integer or BigInteger)."
            )

        reserved = _RESERVED | {
            name for name in Base.__dict__ if not name.startswith("_")
        }
        for name in columns:
            try:
                quote_ident(name)
            except ValueError as error:
                raise ModelDefinitionError(f"Invalid column name {name!r}: {error}") from error
            if name.startswith("_"):
                raise ModelDefinitionError("Column names must not start with '_'")
            if name in reserved:
                raise ModelDefinitionError(f"Column name {name!r} is reserved")

        if table_name in Base._registry:
            raise ModelDefinitionError(f"Table name {table_name!r} is already registered")

        version = _VersionColumn()
        version.name = "version"
        version.table = table_name
        setattr(cls, "version", version)
        cls._columns = columns
        cls._all_columns = {**columns, "version": version}
        cls._pk_name = pk_name
        cls._defaults = tuple(
            (name, column.default)
            for name, column in columns.items()
            if column.default is not None
        )
        Base._registry[table_name] = cls

    def __init__(self, **kwargs):
        unknown = [name for name in kwargs if name not in self._columns]
        if unknown:
            names = ", ".join(repr(name) for name in unknown)
            raise TypeError(f"Unknown model column(s): {names}")
        self.__dict__.update(kwargs)
        for name, default in self._defaults:
            if name not in kwargs:
                self.__dict__[name] = default() if callable(default) else default

    @classmethod
    def _from_row(cls, row):
        """Construct a model from ordered row values without change tracking."""
        instance = cls.__new__(cls)
        instance._apply_row(row)
        return instance

    def _apply_row(self, row):
        """Copy an ordered database row directly onto this model instance."""
        if len(row) != len(self._all_columns):
            raise ValueError("Row length does not match model columns")
        for name, value in zip(self._all_columns, row):
            self.__dict__[name] = value

    def _changes(self) -> dict:
        """Return current values for fields changed since loading or cleaning."""
        changed = self.__dict__.get("_changed", {})
        return {
            name: self.__dict__.get(name)
            for name in self._columns
            if name in changed
        }

    def _mark_clean(self):
        """Clear tracked field changes."""
        self.__dict__.pop("_changed", None)

    @classmethod
    def _column_specs(cls) -> list[ColumnSpec]:
        """Return SQL column specifications in model order."""
        return [column._to_spec() for column in cls._all_columns.values()]

    @classmethod
    def _index_statements(cls) -> list[str]:
        """Return index DDL for columns configured with an index."""
        return [
            build_create_index(cls.__tablename__, column.name)
            for column in cls._columns.values()
            if column.index
        ]

    @classmethod
    def _ordered_models(cls) -> list[type]:
        """Validate foreign keys and return registered models in dependency order."""
        registry = Base._registry
        dependencies = {table: [] for table in registry}
        for table, model in registry.items():
            for column in model._columns.values():
                if column.foreign_key is None:
                    continue
                parent_table, parent_name = column.foreign_key.split(".")
                parent = registry.get(parent_table)
                if parent is None:
                    raise ModelDefinitionError(
                        f"Foreign key {table}.{column.name} references unregistered table {parent_table!r}"
                    )
                target = parent._columns.get(parent_name)
                if target is None:
                    raise ModelDefinitionError(
                        f"Foreign key {table}.{column.name} references missing column {parent_table}.{parent_name}"
                    )
                if not target.primary_key and not target.unique:
                    raise ModelDefinitionError(
                        f"Foreign key {table}.{column.name} references {parent_table}.{parent_name}, which is not primary key or unique"
                    )
                if parent_table != table and parent_table not in dependencies[table]:
                    dependencies[table].append(parent_table)

        remaining = {table: set(parents) for table, parents in dependencies.items()}
        ordered = []
        ordered_names = set()
        while len(ordered_names) < len(registry):
            ready = next(
                (table for table in registry if table not in ordered_names
                 and remaining[table] <= ordered_names),
                None,
            )
            if ready is None:
                cycle_tables = [table for table in registry if table not in ordered_names]
                raise ModelDefinitionError(
                    "Circular foreign key references between tables: "
                    + ", ".join(cycle_tables)
                )
            ordered.append(registry[ready])
            ordered_names.add(ready)
        return ordered

    def __repr__(self):
        values = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._columns)
        return f"{type(self).__name__}({values})"

    @classmethod
    def _run(cls, fn):
        """Run a function on one autocommit pooled connection."""
        with connection() as conn:
            return fn(conn)

    @classmethod
    def _run_tx(cls, fn):
        """Run a function atomically, reusing an active transaction when possible."""
        if pool.in_transaction():
            return cls._run(fn)
        with pool.transaction():
            return cls._run(fn)

    @classmethod
    def execute(cls, sql, params=None) -> list[tuple]:
        """Execute raw SQL and return fetched rows when the statement has results."""
        def run(conn):
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description is not None else []

        return cls._run(run)

    @classmethod
    def create_tables(cls) -> None:
        """Create all registered model tables in a single transaction."""
        models = Base._ordered_models()

        def run(conn):
            for model in models:
                conn.execute(build_create_table(model.__tablename__, model._column_specs()))
            for model in models:
                for statement in model._index_statements():
                    conn.execute(statement)

        cls._run_tx(run)

    @classmethod
    def transaction(cls):
        """Return a context manager for one shared database transaction."""
        return pool.transaction()

    @classmethod
    def create(cls, **values):
        """Create and persist a new instance of this model."""
        return cls(**values).save()

    @classmethod
    def create_many(cls, rows) -> list:
        """Create rows atomically and return their generated primary keys."""
        rows = list(rows)
        if not rows:
            return []
        if any(not isinstance(row, dict) for row in rows):
            raise TypeError("create_many() rows must be dictionaries")
        rows = [dict(row) for row in rows]
        for row in rows:
            for name, default in cls._defaults:
                if name not in row:
                    row[name] = default() if callable(default) else default
        keys = tuple(rows[0])
        for row in rows:
            if set(row) != set(keys):
                raise QueryError("All rows must have the same columns.")
            for name in keys:
                if name not in cls._columns:
                    raise QueryError(f"Unknown column {name!r} for {cls.__name__}")
                if name == cls._pk_name or name == "version":
                    raise QueryError(f"Column {name!r} cannot be set by create_many()")
        statement = build_insert(
            cls.__tablename__, rows[0], returning=[cls._pk_name]
        )
        params = [
            tuple(cls._all_columns[name].to_db(row[name]) for name in keys)
            for row in rows
        ]

        def run(conn):
            with conn.cursor() as cur:
                cur.executemany(statement.sql, params, returning=True)
                ids = []
                while True:
                    ids.append(cur.fetchone()[0])
                    if not cur.nextset():
                        break
                return ids

        return cls._run_tx(run)

    @classmethod
    def _write_condition(cls, method, conditions):
        if not conditions:
            raise QueryError(
                f"{method}() needs at least one condition. To change every row use User.<pk>.is_not_null()."
            )
        if any(not isinstance(condition, Condition) for condition in conditions):
            raise TypeError(f"{method}() arguments must be conditions")
        _validate_condition_columns(cls, conditions)
        return and_(*conditions)

    @classmethod
    def _write_values(cls, method, values):
        if not values:
            raise QueryError(f"{method}() needs at least one value.")
        for name in values:
            if name not in cls._columns:
                raise QueryError(f"Unknown column {name!r} for {cls.__name__}")
            if name == cls._pk_name or name == "version":
                raise QueryError(f"Column {name!r} cannot be changed by {method}()")

    @classmethod
    def update_where(cls, *conditions, **values) -> int:
        """Update matching rows and bump each row's version."""
        where = cls._write_condition("update_where", conditions)
        cls._write_values("update_where", values)
        statement = build_update_where(
            cls.__tablename__,
            {name: cls._all_columns[name].to_db(value) for name, value in values.items()},
            where,
            "version",
        )
        return cls._run(
            lambda conn: conn.execute(statement.sql, statement.params).rowcount
        )

    @classmethod
    def delete_where(cls, *conditions) -> int:
        """Delete rows matching one or more conditions."""
        where = cls._write_condition("delete_where", conditions)
        statement = build_delete_where(cls.__tablename__, where)
        return cls._run(
            lambda conn: conn.execute(statement.sql, statement.params).rowcount
        )

    @classmethod
    def get(cls, *args):
        """Load a row by primary key or conditions, returning None when absent."""
        if args and all(isinstance(arg, Condition) for arg in args):
            row = cls.select(*cls._all_columns.values()).where(*args).first()
            return cls._from_row(row) if row is not None else None
        if len(args) != 1 or isinstance(args[0], Condition):
            raise TypeError("get() needs one primary key value or one or more conditions")
        pk_value = args[0]
        statement = select_by_pk(
            cls.__tablename__, list(cls._all_columns), cls._pk_name, pk_value
        )
        rows = cls.execute(statement.sql, statement.params)
        return cls._from_row(rows[0]) if rows else None

    @classmethod
    def select(cls, *columns) -> Query:
        for column in columns:
            if isinstance(column, Labeled):
                if not isinstance(column.column, Column):
                    raise TypeError("select() accepts Columns or labeled Columns")
            elif not isinstance(column, Column):
                raise TypeError("select() arguments must be Columns")
        return Query(cls, tuple(columns))

    def save(self):
        """Insert a new model or persist its tracked changes."""
        cls = type(self)
        version = self.version
        pk_name = cls._pk_name
        if version is None:
            if getattr(self, pk_name) is not None:
                raise TypeError(
                    "The primary key is generated by the database; do not set it on a new object."
                )
            values = {
                name: cls._all_columns[name].to_db(self.__dict__[name])
                for name in cls._columns
                if name != pk_name and name in self.__dict__
            }
            statement = build_insert(
                cls.__tablename__, values, returning=list(cls._all_columns)
            )
            rows = cls.execute(statement.sql, statement.params)
            self._apply_row(rows[0])
            self._mark_clean()
            return self

        changes = {
            name: cls._all_columns[name].to_db(value)
            for name, value in self._changes().items()
        }
        if not changes:
            return self
        if pk_name in changes:
            raise PyzzelError("The primary key cannot be changed.")
        pk_value = getattr(self, pk_name)
        statement = build_update(
            cls.__tablename__,
            changes,
            pk_name,
            pk_value,
            "version",
            version,
            returning=list(cls._all_columns),
        )

        def run(conn):
            cursor = conn.execute(statement.sql, statement.params)
            row = cursor.fetchone()
            if row is None:
                _classify_zero_rows(conn, cls.__tablename__, pk_name, pk_value)
            return row

        row = cls._run(run)
        self._apply_row(row)
        self._mark_clean()
        return self

    def delete(self) -> None:
        """Delete this saved model using its primary key and version."""
        cls = type(self)
        version = self.version
        if version is None:
            raise PyzzelError("This object has not been saved.")
        pk_name = cls._pk_name
        pk_value = getattr(self, pk_name)
        statement = build_delete(
            cls.__tablename__, pk_name, pk_value, "version", version
        )

        def run(conn):
            cursor = conn.execute(statement.sql, statement.params)
            if cursor.rowcount == 0:
                _classify_zero_rows(conn, cls.__tablename__, pk_name, pk_value)

        cls._run(run)
