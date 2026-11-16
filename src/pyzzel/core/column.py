"""Column descriptors and their SQL specifications."""

import re
from typing import NamedTuple

from pyzzel.core.types import ColumnType, Integer
from pyzzel.errors import ModelDefinitionError, QueryError
from pyzzel.sql.builder import ColumnSpec, check_default_expression, quote_ident
from pyzzel.sql.conditions import Comparison, InList, NullCheck, OrderBy, Pattern


_FOREIGN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}\.[A-Za-z_][A-Za-z0-9_]{0,62}$")
_ON_DELETE = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL"}


class Labeled(NamedTuple):
    column: "Column"
    alias: str


class Column:
    """A typed model field with change tracking."""

    def __init__(
        self,
        type,
        *,
        primary_key=False,
        nullable=True,
        unique=False,
        default=None,
        server_default=None,
        foreign_key=None,
        on_delete="NO ACTION",
        index=None,
    ):
        """Create a column; server_default is trusted text written by the developer, never user input."""
        if isinstance(type, ColumnType):
            pass
        else:
            try:
                is_column_type = issubclass(type, ColumnType)
            except TypeError:
                is_column_type = False
            if is_column_type:
                type = type()
            else:
                raise TypeError("Column type must be a ColumnType class or instance")
        if default is not None and isinstance(default, (list, dict, set, bytearray)):
            raise ModelDefinitionError("Mutable defaults are not supported; use a function such as default=list")
        if primary_key and default is not None:
            raise ModelDefinitionError("A primary key column cannot have a Python default")
        if server_default is not None:
            try:
                check_default_expression(server_default)
            except ValueError as error:
                raise ModelDefinitionError(str(error)) from error
        if foreign_key is not None and (
            not isinstance(foreign_key, str) or _FOREIGN_KEY.fullmatch(foreign_key) is None
        ):
            raise ModelDefinitionError("foreign_key must be a 'table.column' identifier")
        if primary_key and foreign_key is not None:
            raise ModelDefinitionError("A primary key column cannot have a foreign_key")
        if not isinstance(on_delete, str) or on_delete.upper() not in _ON_DELETE:
            raise ModelDefinitionError("on_delete must be NO ACTION, RESTRICT, CASCADE, or SET NULL")
        on_delete = on_delete.upper()
        if foreign_key is None and on_delete != "NO ACTION":
            raise ModelDefinitionError("on_delete requires foreign_key")
        if foreign_key is not None and on_delete == "SET NULL" and not nullable:
            raise ModelDefinitionError("SET NULL requires nullable=True")
        if index is not None and not isinstance(index, bool):
            raise ModelDefinitionError("index must be True, False, or None")
        self.type = type
        self.primary_key = primary_key
        self.nullable = nullable
        self.unique = unique
        self.default = default
        self.server_default = server_default
        self.foreign_key = foreign_key
        self.on_delete = on_delete
        self.index = False if primary_key or unique else (
            index if index is not None else foreign_key is not None
        )
        self.name = None
        self.table = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.__dict__.get(self.name)

    def __set__(self, obj, value):
        old = obj.__dict__.get(self.name)
        if value is old or (
            not isinstance(value, Column)
            and not isinstance(old, Column)
            and value == old
        ):
            return
        changed = obj.__dict__.get("_changed")
        if changed is None:
            changed = {}
            obj.__dict__["_changed"] = changed
        changed.setdefault(self.name, old)
        original = changed[self.name]
        if value is original or (
            not isinstance(value, Column) and not isinstance(original, Column)
            and value == original
        ):
            del changed[self.name]
        obj.__dict__[self.name] = value

    def __hash__(self):
        return id(self)

    def _clone(self):
        """Copy this column's definition for an inheriting model."""
        clone = Column(
            self.type,
            primary_key=self.primary_key,
            nullable=self.nullable,
            unique=self.unique,
            default=self.default,
            server_default=self.server_default,
            foreign_key=self.foreign_key,
            on_delete=self.on_delete,
            index=self.index,
        )
        clone.name = self.name
        clone.table = self.table
        return clone

    def label(self, alias):
        """Name this selected value in a result row."""
        try:
            quote_ident(alias)
        except ValueError as error:
            raise QueryError(f"Invalid column label: {error}") from error
        return Labeled(self, alias)

    def _compare(self, operator, other):
        if isinstance(other, Column):
            raise TypeError("Comparing two columns is not supported yet")
        return Comparison(self.name, operator, self.to_db(other), self.table)

    def to_db(self, value):
        return None if value is None else self.type.to_db(value)

    def __eq__(self, other):
        return self._compare("=", other)

    def __ne__(self, other):
        return self._compare("<>", other)

    def __lt__(self, other):
        return self._compare("<", other)

    def __le__(self, other):
        return self._compare("<=", other)

    def __gt__(self, other):
        return self._compare(">", other)

    def __ge__(self, other):
        return self._compare(">=", other)

    def in_(self, values):
        """Values are sent as one array parameter, so the number of values is not limited."""
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise TypeError("in_() needs a list, tuple, set, or frozenset")
        if isinstance(values, (set, frozenset)):
            values = list(values)
        return InList(self.name, [self.to_db(value) for value in values], self.table)

    def like(self, pattern):
        return Pattern(self.name, "LIKE", pattern, self.table)

    def ilike(self, pattern):
        return Pattern(self.name, "ILIKE", pattern, self.table)

    def is_null(self):
        return NullCheck(self.name, table=self.table)

    def is_not_null(self):
        return NullCheck(self.name, negated=True, table=self.table)

    def asc(self):
        return OrderBy(self.name, "ASC", self.table)

    def desc(self):
        return OrderBy(self.name, "DESC", self.table)

    def _to_spec(self) -> ColumnSpec:
        """Return the SQL builder specification for this column."""
        return ColumnSpec(
            self.name,
            self.type.sql_type,
            primary_key=self.primary_key,
            identity=self.primary_key,
            nullable=self.nullable,
            unique=self.unique,
            default=self.server_default,
            foreign_key=tuple(self.foreign_key.split(".")) if self.foreign_key else None,
            on_delete=self.on_delete if self.foreign_key else None,
        )


class _VersionColumn(Column):
    """Hidden, Pyzzel-managed model version field."""

    def __init__(self):
        super().__init__(Integer, nullable=False)

    def __set__(self, obj, value):
        raise AttributeError("version is managed by Pyzzel")

    def _to_spec(self) -> ColumnSpec:
        """Return the SQL specification for the hidden version field."""
        return ColumnSpec("version", "INTEGER", nullable=False, default="1")
