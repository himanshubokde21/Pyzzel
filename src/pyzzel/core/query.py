"""Immutable model queries and result conversion."""

from collections import namedtuple

from pyzzel.core.column import Column, Labeled
from pyzzel.errors import QueryError, TooManyRowsError
from pyzzel.sql import (
    ColumnRef, Condition, Join, OrderBy, and_, count as build_count,
    select as build_select,
)


_row_types = {}
_UNSET = object()


def _validate_condition_columns(models, conditions, main_model=None):
    if not isinstance(models, (tuple, list)):
        models = (models,)
    by_table = {model.__tablename__: model for model in models}
    main_table = main_model.__tablename__ if main_model is not None else models[0].__tablename__
    for condition in conditions:
        for table, name in condition.columns():
            table = table or main_table
            model = by_table.get(table)
            if model is None:
                from pyzzel import Base

                joined_model = Base._registry.get(table)
                label = joined_model.__name__ if joined_model is not None else table
                raise QueryError(
                    f"Unknown column {table}.{name} in this query; Join {label} first"
                )
            if name not in model._all_columns:
                raise QueryError(f"Unknown column {table}.{name} for {model.__name__}")


def _row_type(names):
    row_type = _row_types.get(names)
    if row_type is None:
        row_type = namedtuple("Row", names)
        _row_types[names] = row_type
    return row_type


class Query:
    """Immutable query for one model."""

    __slots__ = (
        "model", "selected", "condition", "ordering", "_limit", "_offset",
        "_unlimited", "relations", "models", "joins", "_distinct",
    )

    def __init__(
        self, model, selected=(), condition=None, ordering=(), limit=None,
        offset=None, unlimited=False, relations=(), models=None, joins=(), distinct=False,
    ):
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "selected", tuple(selected))
        object.__setattr__(self, "condition", condition)
        object.__setattr__(self, "ordering", tuple(ordering))
        object.__setattr__(self, "_limit", limit)
        object.__setattr__(self, "_offset", offset)
        object.__setattr__(self, "_unlimited", unlimited)
        object.__setattr__(self, "relations", tuple(relations))
        object.__setattr__(self, "models", tuple(models or (model,)))
        object.__setattr__(self, "joins", tuple(joins))
        object.__setattr__(self, "_distinct", distinct)

    def __setattr__(self, name, value):
        raise AttributeError("Query objects are immutable")

    def _new(
        self, *, condition=_UNSET, ordering=_UNSET, limit=_UNSET, offset=_UNSET,
        unlimited=_UNSET, relations=_UNSET, models=_UNSET, joins=_UNSET,
        distinct=_UNSET,
    ):
        return Query(
            self.model,
            self.selected,
            self.condition if condition is _UNSET else condition,
            self.ordering if ordering is _UNSET else ordering,
            self._limit if limit is _UNSET else limit,
            self._offset if offset is _UNSET else offset,
            self._unlimited if unlimited is _UNSET else unlimited,
            self.relations if relations is _UNSET else relations,
            self.models if models is _UNSET else models,
            self.joins if joins is _UNSET else joins,
            self._distinct if distinct is _UNSET else distinct,
        )

    def with_(self, **relations):
        """Return a query that loads explicitly named one-level relations."""
        from pyzzel import Base

        if self.joins:
            raise QueryError("with_() cannot be combined with joins")

        selected_names = set()
        for item in self.selected:
            if isinstance(item, Labeled):
                selected_names.update((item.column.name, item.alias))
            else:
                selected_names.add(item.name)
        if not selected_names:
            selected_names = set(self.model._columns)
        additions = []
        existing_names = {name for name, *_ in self.relations}
        for name, column in relations.items():
            if not isinstance(column, Column) or column.foreign_key is None:
                raise QueryError(f"Relation {name!r} must be a foreign-key Column")
            if name in selected_names or name in existing_names:
                raise QueryError(f"Relation name {name!r} conflicts with a selected column or relation")
            owner = Base._registry.get(column.table)
            if owner is None or owner._all_columns.get(column.name) is not column:
                raise QueryError(f"Could not find exactly one model owning column {column.name!r}")
            parent_table, parent_name = column.foreign_key.split(".")
            parent = Base._registry.get(parent_table)
            if parent is None:
                raise QueryError(f"Foreign-key parent table {parent_table!r} is not registered")
            if parent_name not in parent._columns:
                raise QueryError(f"Foreign-key column {parent_table}.{parent_name} is not registered")
            if self.model is owner and self.model is parent:
                raise QueryError("self-referencing relations are not supported by with_ yet")
            if self.model is owner:
                direction = "many"
                key_column = column
            elif self.model is parent:
                direction = "one"
                key_column = parent._columns[parent_name]
            else:
                raise QueryError(f"{column.name} does not link {self.model.__name__} to another model")
            if key_column.name not in selected_names:
                raise QueryError(f"Add {key_column.name!r} to select() to load relation {name!r}")
            additions.append((name, column, owner, parent, direction, parent._columns[parent_name]))
            existing_names.add(name)
        return self._new(relations=self.relations + tuple(additions))

    def join(self, model, on=None):
        return self._join(model, "INNER", on)

    def left_join(self, model, on=None):
        return self._join(model, "LEFT", on)

    def _join(self, model, kind, on):
        from pyzzel import Base

        if not isinstance(model, type) or not issubclass(model, Base):
            raise QueryError("join() needs a registered model class")
        if Base._registry.get(getattr(model, "__tablename__", None)) is not model:
            raise QueryError("join() needs a registered model class")
        if model in self.models:
            raise QueryError(f"Model {model.__name__} is already in this query (self-joins are not supported)")
        if self.relations:
            raise QueryError("joins cannot be combined with with_()")

        existing_tables = {item.__tablename__ for item in self.models}
        candidates = []
        for column in model._columns.values():
            if column.foreign_key:
                parent_table, parent_column = column.foreign_key.split(".")
                if parent_table in existing_tables:
                    candidates.append((column, model.__tablename__, column.name, parent_table, parent_column))
        for existing in self.models:
            for column in existing._columns.values():
                if column.foreign_key:
                    parent_table, parent_column = column.foreign_key.split(".")
                    if parent_table == model.__tablename__:
                        candidates.append((column, existing.__tablename__, column.name,
                                           parent_table, parent_column))

        if on is not None:
            candidate = next((item for item in candidates if item[0] is on), None)
            if not isinstance(on, Column) or on.foreign_key is None or candidate is None:
                raise QueryError("on= must be a foreign-key column linking the joined model to this query")
            candidates = [candidate]
        elif not candidates:
            existing_names = ", ".join(item.__name__ for item in self.models)
            raise QueryError(f"no foreign key links {existing_names} and {model.__name__}")
        elif len(candidates) > 1:
            raise QueryError(f"Several foreign keys link this query and {model.__name__}; pass on=<foreign key column>")

        _, left_table, left_column, right_table, right_column = candidates[0]
        join = Join(kind, model.__tablename__, left_table, left_column, right_table, right_column)
        return self._new(models=self.models + (model,), joins=self.joins + (join,))

    def distinct(self):
        return self._new(distinct=True)

    def where(self, *conditions):
        if not conditions or any(not isinstance(condition, Condition) for condition in conditions):
            raise TypeError("where() needs conditions such as User.age > 18")
        combined = and_(*conditions)
        if self.condition is not None:
            combined = self.condition & combined
        return self._new(condition=combined)

    def order_by(self, *items):
        additions = []
        for item in items:
            if isinstance(item, Column):
                name = item.name
                order_item = OrderBy(name, "ASC", item.table)
            elif isinstance(item, OrderBy):
                name = item.column
                order_item = item
            else:
                raise TypeError("order_by() needs Columns or OrderBy values")
            additions.append(order_item)
        return self._new(ordering=self.ordering + tuple(additions))

    def limit(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QueryError("limit() needs an int greater than or equal to 0")
        return self._new(limit=value)

    def offset(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QueryError("offset() needs an int greater than or equal to 0")
        return self._new(offset=value)

    def _validated_parts(self):
        from pyzzel import Base

        by_table = {model.__tablename__: model for model in self.models}
        entries = []
        if self.selected:
            for selected in self.selected:
                if isinstance(selected, Labeled):
                    column, alias = selected
                else:
                    column, alias = selected, None
                if not isinstance(column, Column):
                    raise QueryError("select() accepts Columns or labeled Columns")
                table = column.table
                model = by_table.get(table)
                if model is None:
                    owner = Base._registry.get(table)
                    label = owner.__name__ if owner is not None else str(table)
                    raise QueryError(
                        f"Unknown column {table}.{column.name} in this query; Join {label} first"
                    )
                if model._all_columns.get(column.name) is not column:
                    raise QueryError(f"Column {table}.{column.name} does not belong to {model.__name__}")
                entries.append((column, alias))
        else:
            entries = [(column, None) for column in self.model._columns.values()]

        names = tuple(alias or column.name for column, alias in entries)
        if len(set(names)) != len(names):
            raise QueryError("A selected result field appears more than once; use .label(\"...\")")

        if self.condition is not None:
            _validate_condition_columns(self.models, (self.condition,), self.model)
        for item in self.ordering:
            table = item.table or self.model.__tablename__
            target = by_table.get(table)
            if target is None or item.column not in target._all_columns:
                from pyzzel import Base

                joined = Base._registry.get(table)
                label = joined.__name__ if joined is not None else table
                raise QueryError(
                    f"Unknown column {table}.{item.column} in this query; Join {label} first"
                )
        refs = [ColumnRef(column.table, column.name, alias) for column, alias in entries]
        return refs, names, entries

    def to_sql(self):
        """Build the full query statement, including any joins."""
        refs, _, _ = self._validated_parts()
        return build_select(
            self.model.__tablename__,
            refs,
            where=self.condition,
            order_by=self.ordering,
            limit=self._limit,
            offset=self._offset,
            joins=self.joins,
            distinct=self._distinct,
        )

    def all(self):
        ".all() adds LIMIT max_rows + 1 at run time only to detect an overflow."
        statement = self.to_sql()
        max_rows = self.model.max_rows
        guarded = self._limit is None and not self._unlimited and max_rows is not None
        if guarded:
            refs, _, _ = self._validated_parts()
            statement = build_select(
                self.model.__tablename__,
                refs,
                where=self.condition,
                order_by=self.ordering,
                limit=max_rows + 1,
                offset=self._offset,
                joins=self.joins,
                distinct=self._distinct,
            )
        rows = self.model.execute(statement.sql, statement.params)
        if guarded and len(rows) > max_rows:
            raise TooManyRowsError(
                f"Query returned more than {max_rows} rows. Add limit(), page with offset(), or call unlimited()."
            )
        _, names, entries = self._validated_parts()
        if not self.relations:
            row_type = _row_type(names)
            return [row_type(*row) for row in rows]

        related_by_name = {}
        for name, fk_column, owner, parent, direction, parent_column in self.relations:
            if direction == "one":
                key_index = next(index for index, (column, _) in enumerate(entries)
                                 if column is parent_column)
                keys = list(dict.fromkeys(row[key_index] for row in rows if row[key_index] is not None))
                grouped = {}
                if keys:
                    children = owner.select().where(fk_column.in_(keys)).order_by(
                        owner._all_columns[owner._pk_name]
                    ).all()
                    for child in children:
                        grouped.setdefault(getattr(child, fk_column.name), []).append(child)
                related_by_name[name] = [
                    grouped.get(row[key_index], []) if row[key_index] is not None else []
                    for row in rows
                ]
            else:
                key_index = next(index for index, (column, _) in enumerate(entries)
                                 if column is fk_column)
                keys = list(dict.fromkeys(row[key_index] for row in rows if row[key_index] is not None))
                matched = {}
                if keys:
                    parents = parent.select().where(parent_column.in_(keys)).all()
                    matched = {getattr(row, parent_column.name): row for row in parents}
                related_by_name[name] = [
                    matched.get(row[key_index]) if row[key_index] is not None else None
                    for row in rows
                ]
        row_type = _row_type((*names, *(relation[0] for relation in self.relations)))
        return [
            row_type(*row, *(related_by_name[relation[0]][index] for relation in self.relations))
            for index, row in enumerate(rows)
        ]

    def unlimited(self):
        """Return a query without the automatic row guard."""
        return self._new(unlimited=True)

    def first(self):
        rows = self.limit(1).all()
        return rows[0] if rows else None

    def count(self):
        if self._distinct:
            raise QueryError("count() with distinct() is not supported yet")
        self._validated_parts()
        statement = build_count(self.model.__tablename__, self.condition, joins=self.joins)
        rows = self.model.execute(statement.sql, statement.params)
        return rows[0][0]

    def __iter__(self):
        return iter(self.all())

    def __repr__(self):
        return f"Query({self.to_sql().sql!r})"
