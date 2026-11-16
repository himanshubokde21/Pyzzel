"""Composable, parameterized PostgreSQL query conditions."""

from typing import NamedTuple


class Condition:
    """Base class for query conditions."""

    def render(self, qualify=False) -> tuple[str, tuple]:
        raise NotImplementedError

    def columns(self) -> list[tuple[str | None, str]]:
        raise NotImplementedError

    def __and__(self, other):
        if not isinstance(other, Condition):
            return NotImplemented
        left = self.parts if isinstance(self, And) else (self,)
        right = other.parts if isinstance(other, And) else (other,)
        return And(left + right)

    def __or__(self, other):
        if not isinstance(other, Condition):
            return NotImplemented
        left = self.parts if isinstance(self, Or) else (self,)
        right = other.parts if isinstance(other, Or) else (other,)
        return Or(left + right)

    def __invert__(self):
        return Not(self)

    def __bool__(self):
        raise TypeError(
            "Use & | ~ (with parentheses), or and_()/or_(), instead of and/or/not or chained comparisons."
        )


def _quote(name, table=None, qualify=False):
    from .builder import quote_ident

    column = quote_ident(name)
    return f"{quote_ident(table)}.{column}" if qualify and table is not None else column


class Comparison(Condition):
    __slots__ = ("name", "operator", "value", "table")
    _operators = {"=", "<>", "<", "<=", ">", ">="}

    def __init__(self, name, operator, value, table=None):
        if operator not in self._operators:
            raise ValueError(f"Unsupported comparison operator: {operator!r}")
        if value is None and operator not in ("=", "<>"):
            raise ValueError("None supports only = and <> comparisons")
        self.name = name
        self.operator = operator
        self.value = value
        self.table = table

    def render(self, qualify=False):
        column = _quote(self.name, self.table, qualify)
        if self.value is None:
            suffix = "IS NULL" if self.operator == "=" else "IS NOT NULL"
            return f"{column} {suffix}", ()
        return f"{column} {self.operator} %s", (self.value,)

    def columns(self):
        return [(self.table, self.name)]


class InList(Condition):
    __slots__ = ("name", "values", "table")

    def __init__(self, name, values, table=None):
        self.name = name
        self.values = tuple(values)
        self.table = table

    def render(self, qualify=False):
        column = _quote(self.name, self.table, qualify)
        if not self.values:
            return "FALSE", ()
        return f"{column} = ANY(%s)", (list(self.values),)

    def columns(self):
        return [(self.table, self.name)]


class Pattern(Condition):
    __slots__ = ("name", "operator", "pattern", "table")

    def __init__(self, name, operator, pattern, table=None):
        self.name = name
        self.operator = operator
        self.pattern = pattern
        self.table = table

    def render(self, qualify=False):
        return f"{_quote(self.name, self.table, qualify)} {self.operator} %s", (self.pattern,)

    def columns(self):
        return [(self.table, self.name)]


class NullCheck(Condition):
    __slots__ = ("name", "negated", "table")

    def __init__(self, name, negated=False, table=None):
        self.name = name
        self.negated = negated
        self.table = table

    def render(self, qualify=False):
        suffix = "IS NOT NULL" if self.negated else "IS NULL"
        return f"{_quote(self.name, self.table, qualify)} {suffix}", ()

    def columns(self):
        return [(self.table, self.name)]


class And(Condition):
    __slots__ = ("parts",)

    def __init__(self, parts):
        flattened = []
        for part in parts:
            flattened.extend(part.parts if isinstance(part, And) else (part,))
        self.parts = tuple(flattened)

    def render(self, qualify=False):
        rendered = [part.render(qualify=qualify) for part in self.parts]
        return "(" + " AND ".join(sql for sql, _ in rendered) + ")", tuple(
            param for _, params in rendered for param in params
        )

    def columns(self):
        return [column for part in self.parts for column in part.columns()]


class Or(Condition):
    __slots__ = ("parts",)

    def __init__(self, parts):
        flattened = []
        for part in parts:
            flattened.extend(part.parts if isinstance(part, Or) else (part,))
        self.parts = tuple(flattened)

    def render(self, qualify=False):
        rendered = [part.render(qualify=qualify) for part in self.parts]
        return "(" + " OR ".join(sql for sql, _ in rendered) + ")", tuple(
            param for _, params in rendered for param in params
        )

    def columns(self):
        return [column for part in self.parts for column in part.columns()]


class Not(Condition):
    __slots__ = ("condition",)

    def __init__(self, condition):
        self.condition = condition

    def render(self, qualify=False):
        sql, params = self.condition.render(qualify=qualify)
        return f"NOT ({sql})", params

    def columns(self):
        return self.condition.columns()


class OrderBy(NamedTuple):
    column: str
    direction: str
    table: str | None = None


def and_(*conditions):
    if not conditions:
        raise ValueError("and_() needs at least one condition")
    if any(not isinstance(condition, Condition) for condition in conditions):
        raise TypeError("and_() arguments must be conditions")
    if len(conditions) == 1:
        return conditions[0]
    return And(conditions)


def or_(*conditions):
    if not conditions:
        raise ValueError("or_() needs at least one condition")
    if any(not isinstance(condition, Condition) for condition in conditions):
        raise TypeError("or_() arguments must be conditions")
    if len(conditions) == 1:
        return conditions[0]
    return Or(conditions)
