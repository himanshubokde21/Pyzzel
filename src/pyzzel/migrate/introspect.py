"""Read PostgreSQL catalog metadata and build schema snapshots."""

import re

from pyzzel.core.base import Base
from .schema import canonical_type


def fetch_catalog(conn_or_none=None, *, schema=None):
    """Return tables, columns, constraints, and index metadata as tuples."""
    def fetch(execute):
        tables = execute(
                "SELECT c.oid, c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=COALESCE(%s, current_schema()) AND c.relkind='r' "
                "ORDER BY c.relname", (schema,),
            )
        columns = execute(
                "SELECT a.attrelid, a.attnum, a.attname, "
                "pg_catalog.format_type(a.atttypid,a.atttypmod), a.attnotnull, a.attidentity, "
                "pg_catalog.pg_get_expr(d.adbin,d.adrelid) "
                "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE n.nspname=COALESCE(%s,current_schema()) AND c.relkind='r' "
                "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attrelid,a.attnum", (schema,),
            )
        constraints = execute(
                "SELECT co.conrelid, co.conname, co.contype, co.conkey, rc.relname, "
                "ARRAY(SELECT a.attname FROM unnest(co.confkey) WITH ORDINALITY k(attnum,ord) "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=co.confrelid AND a.attnum=k.attnum "
                "ORDER BY k.ord), co.confdeltype "
                "FROM pg_catalog.pg_constraint co JOIN pg_catalog.pg_class c ON c.oid=co.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_catalog.pg_class rc ON rc.oid=co.confrelid "
                "WHERE n.nspname=COALESCE(%s,current_schema()) AND co.contype IN ('p','u','f') "
                "ORDER BY co.conrelid,co.conname", (schema,),
            )
        indexes = execute(
                "SELECT i.indrelid, ic.relname, i.indisprimary, i.indisunique, "
                "i.indpred IS NOT NULL, i.indexprs IS NOT NULL, i.indnkeyatts, "
                "i.indkey::smallint[], am.amname, co.oid IS NOT NULL "
                "FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid "
                "JOIN pg_catalog.pg_am am ON am.oid=ic.relam "
                "LEFT JOIN pg_catalog.pg_constraint co ON co.conindid=i.indexrelid "
                "AND co.contype IN ('p','u') "
                "WHERE n.nspname=COALESCE(%s,current_schema()) AND c.relkind='r' "
                "ORDER BY i.indrelid,ic.relname", (schema,),
            )
        return {"tables": tables, "columns": columns,
                "constraints": constraints, "indexes": indexes}
    if conn_or_none is None:
        with Base.transaction():
            return fetch(Base.execute)
    with conn_or_none.transaction():
        return fetch(lambda sql, params: conn_or_none.execute(sql, params).fetchall())


_DELETE = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
           "n": "SET NULL", "d": "SET DEFAULT"}
_SUPPORTED = {"integer", "smallint", "bigint", "double precision", "numeric", "text",
              "character varying", "boolean", "date", "time without time zone",
              "timestamp with time zone", "timestamp without time zone", "uuid", "jsonb", "bytea"}


def _supported(sql_type):
    if sql_type in _SUPPORTED:
        return True
    return bool(re.fullmatch(r"numeric\(\d+(?:,\d+)?\)|character varying\(\d+\)", sql_type))


def build_snapshot(catalog, *, ignore=(), exclude=()):
    """Build a snapshot from fetch_catalog tuple rows without database access."""
    skipped = set(ignore) | set(exclude)
    table_names = {oid: name for oid, name in catalog["tables"] if name not in skipped}
    by_oid = {oid: [] for oid in table_names}
    for row in catalog["columns"]:
        if row[0] in by_oid:
            by_oid[row[0]].append(row)
    constraints = {oid: [] for oid in table_names}
    for row in catalog["constraints"]:
        if row[0] in constraints:
            constraints[row[0]].append(row)
    warnings, tables = [], {}
    for oid, table in sorted(table_names.items(), key=lambda item: item[1]):
        cols = by_oid[oid]
        names = {row[1]: row[2] for row in cols}
        pks, uniques, fks = {}, {}, {}
        for _, name, kind, keys, ref_table, ref_cols, delete in constraints[oid]:
            keys = list(keys or ())
            if kind in ("p", "u", "f") and len(keys) > 1:
                label = {"p": "primary key", "u": "unique", "f": "foreign key"}[kind]
                warnings.append(f"composite {label} on {table} is not supported")
            if len(keys) == 1:
                (pks if kind == "p" else uniques if kind == "u" else fks if kind == "f" else {}).update(
                    {keys[0]: (ref_table, list(ref_cols or ()), delete)})
        output_columns = {}
        for _, attnum, name, raw_type, notnull, identity, default in sorted(cols, key=lambda row: row[1]):
            sql_type = canonical_type(raw_type)
            if not _supported(sql_type):
                warnings.append(f"unsupported type {sql_type} on {table}.{name}")
            fk = fks.get(attnum)
            ref = [fk[0], fk[1][0]] if fk and len(fk[1]) == 1 else None
            output_columns[name] = {
                "type": sql_type, "nullable": False if attnum in pks else not notnull,
                "primary_key": attnum in pks, "identity": identity in ("a", "d"),
                "unique": attnum in uniques, "default": default, "references": ref,
                "on_delete": _DELETE.get(fk[2]) if ref else None,
            }
        plain_indexes = []
        for row in catalog["indexes"]:
            (table_oid, idx_name, primary, unique, predicate, expressions,
             nkeys, attnums, method, backs_constraint) = row
            if table_oid != oid:
                continue
            attnums = list(attnums or ())[:nkeys]
            if primary or backs_constraint:
                continue
            reason = nkeys != 1 or predicate or expressions or (unique and not backs_constraint) or method != "btree"
            if reason:
                warnings.append(f"ignored index {idx_name} on {table}")
            elif not unique and attnums[0] in names:
                plain_indexes.append((attnums[0], names[attnums[0]]))
        plain_indexes.sort(key=lambda item: item[0])
        tables[table] = {"columns": output_columns,
                         "indexes": [name for _attnum, name in plain_indexes]}
    return {"version": 1, "tables": tables}, warnings


def schema_from_database(*, schema=None, ignore=(), exclude=()):
    return build_snapshot(fetch_catalog(schema=schema), ignore=ignore, exclude=exclude)
