# Pyzzel

Pyzzel is a lightweight Python ORM for PostgreSQL. It combines an Active Record model interface with explicit queries, short-lived pooled connections, and visible transaction boundaries.

The package is under development (version 0.0.1). Its scope is PostgreSQL; it does not support other database systems.

## Contents

- [Design](#design)
- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Connections and configuration](#connections-and-configuration)
- [Models and columns](#models-and-columns)
- [CRUD operations](#crud-operations)
- [Queries](#queries)
- [Relationships and joins](#relationships-and-joins)
- [Bulk writes](#bulk-writes)
- [Transactions and concurrency](#transactions-and-concurrency)
- [Migrations](#migrations)
- [Errors](#errors)
- [Performance notes](#performance-notes)
- [Limitations](#limitations)
- [Project structure](#project-structure)
- [Tests and contribution](#tests-and-contribution)
- [License](#license)

## Design

Pyzzel uses model classes and descriptors to define tables and columns. Database work happens when an operation is called; a model instance does not hold a long-lived session. Each standalone operation borrows a connection from the pool and returns it afterward. A transaction explicitly keeps one connection for its block.

The library keeps reading explicit:

- `Model.get(...)` loads a model object.
- `Model.select(...)` builds a query that returns lightweight row values.
- Relationships are loaded only when requested with `.with_(...)`.
- `.to_sql()` exposes the generated SQL and its parameters without running it.

Values are passed separately from SQL using Psycopg parameters. Identifiers and SQL types are validated by Pyzzel's PostgreSQL SQL builder.

## Features

- Active Record models with `create()`, `get()`, `save()`, and `delete()`.
- PostgreSQL column types, Python defaults, and trusted server defaults.
- Change tracking and optimistic concurrency through a managed `version` column.
- Immutable query construction with conditions, sorting, paging, counting, and a row limit.
- Foreign keys, explicitly requested relationship loading, and inner or left joins.
- Atomic `create_many()`, conditional `update_where()`, and `delete_where()`.
- A Psycopg connection pool and explicit transactions with nested savepoints.
- Optional schema migrations, migration status, schema drift checks, and snapshot pull.

## Installation

Pyzzel requires Python 3.10 or newer and a PostgreSQL database. From a checkout of the project, install the package with:

```console
python -m pip install .
```

For development, including the test dependency:

```console
python -m pip install -e ".[dev]"
```

The runtime dependency is `psycopg[binary,pool]>=3.2`. The `dev` extra installs `pytest`. The project metadata does not specify a minimum PostgreSQL server version.

## Quick start

Create a `models.py` file in your application directory:

```python
from pyzzel import Base, Boolean, Column, Integer, String


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String(255), unique=True)
    age = Column(Integer)
    active = Column(Boolean, default=True)
```

Generate and apply an initial migration. The `generate` command imports the model module, compares the registered models with the migration snapshot, and writes a numbered SQL file.

PowerShell:

```powershell
$env:PYZZEL_DATABASE_URL = "postgresql://user:password@localhost:5432/my_database"
python -m pyzzel.migrate generate create_users --models models
python -m pyzzel.migrate apply
```

Then use the model from your application:

```python
import os

from pyzzel import connect, disconnect
from models import User


connect(os.environ["PYZZEL_DATABASE_URL"])
try:
    user = User.create(name="Amit", email="amit@example.test")
    loaded = User.get(user.id)

    loaded.name = "Rahul"
    loaded.save()

    loaded.delete()
finally:
    disconnect()
```

`generate` is offline; `apply` connects to the database. The command line reads `PYZZEL_DATABASE_URL` when `--url` is not supplied. Migration commands are described in [Migrations](#migrations).

For a small prototype without migrations, `Base.create_tables()` creates the registered model tables if they do not already exist. It does not alter or drop existing tables.

## Connections and configuration

Connect once during application startup and disconnect when the application is shutting down:

```python
import os

from pyzzel import connect, disconnect

connect(
    os.environ["PYZZEL_DATABASE_URL"],
    min_size=1,
    max_size=10,
    timeout=30.0,
)
try:
    # Application database operations
    ...
finally:
    disconnect()
```

`connect(url, *, min_size=1, max_size=10, timeout=30.0, check=False, prepare_threshold=None)` first probes the database, then opens a `psycopg_pool.ConnectionPool` with autocommit enabled. Automatic prepared statements are disabled by default because transaction poolers such as Supabase's shared pooler do not support them. Set `prepare_threshold` to an integer only when the server connection mode supports prepared statements. The driver imports are lazy: importing `pyzzel` does not import Psycopg.

`create_many()` sends bounded multi-row `INSERT ... RETURNING` statements in one transaction. This avoids Psycopg's pipeline-based `executemany()` path, which shared transaction poolers do not support.

- `min_size` and `max_size` configure the pool.
- `timeout` is used for connection setup and pool waits.
- `check=True` checks a pooled connection before each operation. The project measurements show an additional database round trip; leave it off unless checking stale connections is important for your environment.
- Calling `connect()` while already connected raises `AlreadyConnectedError`.
- `disconnect()` closes the pool and is safe when no pool is open.

Pyzzel maintains one process-wide pool, so it currently supports one configured database per process. Database tests use the separate `PYZZEL_TEST_DATABASE_URL` variable and skip when it is unset. Migration CLI commands use `PYZZEL_DATABASE_URL` or accept `--url`.

## Models and columns

Models inherit from the package's `Base`. Each concrete model must define a unique `__tablename__` and exactly one integer primary key.

```python
from pyzzel import Base, Column, Integer, Numeric, String


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
```

A column accepts a type class or an instance. Supported types are:

| Pyzzel type | PostgreSQL type | Python value |
|---|---|---|
| `SmallInteger`, `Integer`, `BigInteger` | `SMALLINT`, `INTEGER`, `BIGINT` | `int` |
| `Float` | `DOUBLE PRECISION` | `float` |
| `Numeric(precision=None, scale=None)` | `NUMERIC` or `NUMERIC(p,s)` | `decimal.Decimal` |
| `String(length=None)` | `TEXT` or `VARCHAR(n)` | `str` |
| `Boolean` | `BOOLEAN` | `bool` |
| `Date`, `Time` | `DATE`, `TIME` | `datetime.date`, `datetime.time` |
| `DateTime(timezone=True)` | `TIMESTAMPTZ` or `TIMESTAMP` | `datetime.datetime` |
| `UUID` | `UUID` | `uuid.UUID` |
| `JSON` | `JSONB` | JSON-compatible values |
| `LargeBinary` | `BYTEA` | `bytes` |

Common column options:

- `primary_key=True` marks the generated integer key.
- `nullable=False` creates a `NOT NULL` column.
- `unique=True` creates a unique constraint.
- `default=value` applies a Python-side default when a new object is constructed. Use a factory such as `default=list` for mutable values.
- `server_default="now()"` includes trusted SQL expression text in the table definition. It is written into DDL, so only use an expression you control.
- `foreign_key="users.id"` declares a PostgreSQL foreign key. See [Relationships and joins](#relationships-and-joins).

The hidden `version` column is created for each model table and managed by Pyzzel. It is used for optimistic concurrency; do not set it yourself. It is not included in `select()`'s default selected fields.

Shared model fields can live in an abstract base:

```python
from pyzzel import Base, Column, DateTime, Integer, String


class Timestamped(Base):
    __abstract__ = True
    created_at = Column(DateTime, server_default="now()")


class User(Timestamped):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
```

Pyzzel clones inherited column descriptors for concrete models. A model cannot inherit from another concrete model.

### Defaults and change tracking

Python defaults are applied when creating model instances (and in `create_many()`). A callable default is called for each new object. Server defaults are applied by PostgreSQL; they are not Python defaults.

Assigning a different field value records that field for the next `save()`. Assigning the original value back clears the pending change. In-place mutation of JSON values is not detected; assign a new value instead:

```python
user.profile = {**user.profile, "theme": "dark"}
user.save()
```

Naive datetimes use the database session's time zone. Prefer timezone-aware datetimes when using `DateTime(timezone=True)`.

## CRUD operations

### Create

`Model.create(**values)` constructs and saves an object, then returns that object. The database generates the primary key and initial version.

```python
user = User.create(name="Amit", email="amit@example.test")
```

You can also create an unsaved instance and call `save()`. Do not set the primary key on a new object.

### Get

`Model.get(primary_key)` returns the matching model instance or `None`. It also accepts one or more conditions, combined with AND, and returns the first matching object or `None`:

```python
user = User.get(12)
user = User.get(User.email == "amit@example.test")
user = User.get(User.active == True, User.age >= 18)
```

### Save

Calling `save()` on a new instance inserts it and returns the same object. Calling it on a saved object updates only changed columns, increments the version, and returns the object. If there are no tracked changes, it performs no query.

### Delete

`user.delete()` deletes a saved row and returns `None`. Calling it on an unsaved instance raises `PyzzelError`.

### Raw SQL

`Base.execute(sql, params=None)` is an escape hatch for SQL that is not expressed by the ORM. It returns rows as tuples, or an empty list for a statement without result rows. Keep values in the parameter sequence:

```python
rows = Base.execute(
    'SELECT "id", "name" FROM "users" WHERE "email" = %s',
    ("amit@example.test",),
)
```

Use PostgreSQL placeholders (`%s`) and never interpolate user-provided values into SQL text.

## Queries

`Model.select(*columns)` returns an immutable query builder. It does not run SQL until you call `.all()`, `.first()`, or `.count()`.

- With no columns, it selects all user-defined columns (not the hidden version column).
- With selected columns, each returned row exposes values by field name and by index.
- Query methods return a new query, so you can reuse an earlier query safely.

```python
adults = (
    User.select(User.name, User.age)
    .where(User.age >= 18, User.active == True)
    .order_by(User.name)
    .limit(50)
    .offset(0)
    .all()
)

for row in adults:
    print(row.name, row[1])
```

Available query methods:

| Method | Behavior |
|---|---|
| `.where(*conditions)` | Adds conditions; multiple and chained conditions are combined with AND. |
| `.order_by(*columns)` | Adds ascending order; pass `.asc()` or `.desc()` to choose direction. |
| `.limit(n)`, `.offset(n)` | Set non-negative integer paging values. |
| `.all()` | Executes and returns a list of row values. |
| `.first()` | Returns the first row or `None`; applies a limit of one. |
| `.count()` | Executes `COUNT(*)` for the query's conditions and joins. `distinct().count()` is unsupported. |
| `.distinct()` | Requests distinct result rows. |
| `.unlimited()` | Disables the automatic row guard for `.all()`. |
| `.to_sql()` | Returns a `Statement` with `.sql` and `.params`; does not execute it. |

Use `Base.max_rows` or `Model.max_rows` to set the default maximum returned by `.all()`. The default is 10,000. When no explicit limit is set, Pyzzel fetches at most one extra row to detect an overflow and raises `TooManyRowsError`. An explicit `.limit()` or `.unlimited()` disables that automatic guard.

### Conditions

Column comparisons create condition objects:

```python
from pyzzel import and_, or_

active_adults = User.select().where(
    (User.active == True) & (User.age >= 18)
).all()

matches = User.select().where(
    or_(User.name.ilike("a%"), User.name.ilike("r%"))
).all()
```

Supported comparisons are `==`, `!=`, `<`, `<=`, `>`, and `>=`. Compare with `None` using `== None` or `!= None`; these become `IS NULL` and `IS NOT NULL`. Other condition helpers are:

- `column.in_(values)` accepts a list, tuple, set, or frozenset and uses one PostgreSQL array parameter. An empty collection matches no rows.
- `column.like(pattern)` and `column.ilike(pattern)`.
- `column.is_null()` and `column.is_not_null()`.
- `&`, `|`, and `~`, or the `and_(...)` and `or_(...)` helpers.

Use parentheses around conditions combined with `&` and `|`. Python's `and`, `or`, `not`, and chained comparisons are not supported for constructing conditions.

## Relationships and joins

Declare a foreign key with a table and column name:

```python
class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        nullable=False,
        foreign_key="users.id",
        on_delete="CASCADE",
    )
    total = Column(Numeric(10, 2), nullable=False)
```

Supported `on_delete` values are `NO ACTION` (the default), `RESTRICT`, `CASCADE`, and `SET NULL`. `SET NULL` requires a nullable child column. Foreign-key columns are indexed by default; set `index=False` to disable that index, or `index=True` to index another non-primary, non-unique column.

### Explicit relationship loading

Use `.with_(...)` to load one level of related rows as part of a query result. The selected row includes the named relation field:

```python
users = User.select().with_(orders=Order.user_id).all()
for user in users:
    print(user.name, user.orders)  # list of Order rows
```

When querying the child model, the relation is a parent row or `None`:

```python
orders = Order.select().with_(user=Order.user_id).all()
```

The key column used for the relationship must be selected. Each requested relation runs an additional query; related rows are ordered by primary key and their model's row guard applies. Relationship loading is one level only and never happens implicitly. Use `Base.transaction()` if the main query and relation queries need one consistent database view. `.to_sql()` shows only the main query.

### Joins

Joins return flat rows. A parent appears once per matching child row. The join condition is inferred from declared foreign keys:

```python
rows = (
    User.select(User.name, Order.total)
    .join(Order)
    .where(Order.total > 100)
    .all()
)

users_with_or_without_orders = User.select(User.name, Order.total).left_join(Order).all()
```

Use `.left_join(Model)` to preserve rows without a match; joined values are `None` when no row matches. If multiple foreign keys link the same models, choose one with `on=` and pass the linking foreign-key column:

```python
messages = Message.select(
    Message.body,
    User.name.label("sender_name"),
).join(User, on=Message.sender_id).all()
```

Use `.label("...")` when selected columns would otherwise have duplicate result field names. `.distinct()` requests distinct rows. Joins cannot be combined with `.with_(...)`.

## Bulk writes

`create_many(rows)` accepts dictionaries with the same column keys, applies Python defaults, inserts the rows in one transaction, and returns generated primary keys in input order:

```python
ids = User.create_many([
    {"name": "Amit", "email": "amit@example.test"},
    {"name": "Rahul", "email": "rahul@example.test"},
])
```

An empty list returns an empty list. If an insert fails, the transaction rolls back. All rows must have the same keys; primary key and version values cannot be supplied.

Conditional writes issue one SQL statement and require at least one condition:

```python
updated = User.update_where(User.age < 18, active=False)
deleted = User.delete_where(User.active == False)
```

Both return the affected row count. `update_where()` also increments the version of each updated row. To explicitly target all rows, pass a condition such as `User.id.is_not_null()`.

## Transactions and concurrency

Each standalone statement runs in autocommit mode. Use `Base.transaction()` when multiple operations must succeed or fail together:

```python
with Base.transaction():
    debit_account.balance -= 10
    debit_account.save()
    credit_account.balance += 10
    credit_account.save()
```

All operations inside the block reuse one connection. An exception escaping the outer block rolls back the work. A nested transaction block uses a PostgreSQL savepoint, so code may catch an inner error and continue the outer transaction.

Every model table has a managed integer `version` column. Updates and deletes match both the primary key and the version loaded by the object. A successful update increments the version. If another writer changed or removed the row, Pyzzel raises `VersionConflict` or `NotFound` respectively. This is optimistic concurrency control: it detects conflicting writes when they occur rather than locking rows in advance.

## Migrations

Pyzzel's optional `pyzzel.migrate` module generates and applies numbered SQL migration files. It is separate from the core package and is not loaded by `import pyzzel`.

### Typical workflow

1. Import your model modules and generate a migration against the current `snapshot.json`:

   ```console
   python -m pyzzel.migrate generate add_orders --models models
   ```

2. Review the generated SQL and snapshot changes. The command does not connect to the database.
3. Apply pending migrations:

   ```console
   python -m pyzzel.migrate apply --url "$PYZZEL_DATABASE_URL"
   ```

4. Check that the models, migration files, and database agree:

   ```console
   python -m pyzzel.migrate check --models models --url "$PYZZEL_DATABASE_URL"
   ```

On PowerShell, use `$env:PYZZEL_DATABASE_URL` to set the environment variable. The CLI accepts `--url` for database commands or reads `PYZZEL_DATABASE_URL`. Import model modules using repeatable `--models module.path` options. `--dir` selects the migration directory (default `migrations`).

### Commands

| Command | Purpose |
|---|---|
| `generate NAME` | Diff registered models against `snapshot.json`; write a numbered SQL file and update the snapshot. If there are no changes, print `No changes.` |
| `create NAME` | Create an empty numbered SQL file for hand-written migration SQL. |
| `apply` | Apply pending files in order, recording each in the history table. |
| `status` | List migration files as `pending`, `applied`, or `edited`, and applied entries missing from disk as `missing`. |
| `check` | Compare models, migration files, and database drift. Requires at least one `--models` module. |
| `pull` | Compare the live database schema with the snapshot; `--write` replaces the snapshot with the live schema. Does not modify the database. |

Database commands use `pyzzel_migrations` as the default history table; change it with `--history-table`. `check` and `pull` accept repeatable `--ignore TABLE` options. `apply` supports `--dry-run` to list pending files and `--allow-destructive` to permit migrations marked destructive.

Generated migrations can contain a `-- pyzzel:destructive` marker for operations such as dropping a table or column or changing a type. Pyzzel refuses to apply pending destructive migration files unless explicitly allowed. Review every generated migration before applying it. Each file and its history row run in a transaction; the migrator takes a transaction-scoped PostgreSQL advisory lock, which works with transaction poolers, and verifies SHA-256 checksums to detect edited applied files.

### Python API

The module exports `generate`, `create`, `apply`, `status`, `check`, and `pull`. The first two are offline. Database operations require an active Pyzzel connection; call `connect()` before invoking them. For example:

```python
import os

from pyzzel import connect, disconnect
from pyzzel.migrate import apply, status

connect(os.environ["PYZZEL_DATABASE_URL"])
try:
    print(status("migrations"))
    apply("migrations")
finally:
    disconnect()
```

### Migration boundaries

There are no down migrations; make a new migration to move the schema forward. Renames are not detected automatically and may appear as a drop plus an add, so edit the generated SQL to use PostgreSQL's rename syntax when appropriate.

Schema comparison reads selected metadata from PostgreSQL's catalogs. It handles columns and types, nullability, defaults, primary keys, single-column unique constraints and foreign keys, and simple single-column indexes. Composite constraints, check constraints, triggers, views, sequences, arrays, enums, partial or expression indexes, and other unsupported objects are not fully compared; some produce warnings. Default expressions are normalized best-effort, so equivalent but unusual expressions may still appear different. If migration files are pending, edited, or missing, `check` reports those issues and does not check database drift.

## Errors

Pyzzel's custom errors are in `pyzzel.errors`:

- `PyzzelError`: base class for library errors.
- `ModelDefinitionError`: invalid model or column definition.
- `NotConnectedError`, `AlreadyConnectedError`: connection lifecycle errors.
- `NotFound`: the row was removed before an optimistic update or delete.
- `VersionConflict`: the row was changed by another writer.
- `QueryError`: invalid query or write operation.
- `TooManyRowsError`: a query exceeds the automatic row limit; this is a subclass of `QueryError`.
- `MigrationError`: migration processing error.

PostgreSQL errors, including constraint violations, are Psycopg errors and are not wrapped as Pyzzel errors. Catch the specific exception you expect:

```python
from pyzzel.errors import NotFound, VersionConflict

try:
    user.save()
except VersionConflict:
    # Reload and reconcile the latest row before retrying.
    user = User.get(user.id)
except NotFound:
    # The row no longer exists.
    user = None
```

## Performance notes

The design notes record measurements from Windows with Dockerized PostgreSQL. Results are workload- and machine-dependent; comparisons are most useful within the same run.

- Single operations were measured within roughly 3–30% of raw Psycopg across the recorded runs.
- Query construction measured about 19–46 microseconds per composed query across several later sessions. The notes attribute much of the spread to machine variability; prepared-query caching was not considered necessary.
- A join loading 100 parents with three children each measured about twice as fast as explicit `.with_()` loading in the recorded comparisons. These are different result shapes and query strategies.
- For 1,000 inserts, `create_many()` measured about 6,300–12,800 rows/second, while plain Psycopg `executemany` measured about 13,900–19,900 rows/second. `create_many()` returns generated IDs and is atomic; the raw comparison does not provide the same ORM behavior.
- The measurements identified transaction commits and database round trips as larger costs than SQL string construction. Group related writes in `Base.transaction()` or use `create_many()` for same-shape inserts.

These are project measurements, not a comparative benchmark against SQLAlchemy, Django, Peewee, or other ORMs.

## Limitations

- PostgreSQL only; one configured database pool per process.
- No automatic lazy loading. Relationships are explicitly loaded and only one level deep.
- Joins support inner and left joins, but not aliases, self-joins, right/full joins, arbitrary `ON` expressions, or combining joins with `.with_(...)`.
- No `GROUP BY`; `.count()` is the only aggregate.
- No array, enum, or interval column types; primary keys must use an integer type.
- In-place JSON mutation is not tracked; assign a replacement value.
- `update_where()` accepts values, not SQL expressions. `create_many()` requires identical keys in every row.
- Migration tooling has no down migrations or automatic rename detection and compares only the schema objects listed above.
- The optional connection check adds a round trip to operations. With it off, the first use of a stale connection after a database restart can fail.
- Raw database errors are surfaced as Psycopg errors.

## Project structure

```text
src/pyzzel/
├── __init__.py          # Public exports
├── errors.py            # Pyzzel exception types
├── pool.py              # Connection pool and transaction context
├── core/
│   ├── base.py          # Base model and Active Record operations
│   ├── column.py        # Column descriptors and model field definitions
│   ├── query.py         # Immutable query object and row conversion
│   └── types.py         # PostgreSQL column types
├── sql/
│   ├── builder.py       # PostgreSQL SQL statement builders
│   └── conditions.py    # Conditions and ordering values
└── migrate/             # Optional migration and schema comparison tools
    ├── __init__.py
    ├── __main__.py
    ├── apply.py
    ├── compare.py
    ├── diff.py
    ├── introspect.py
    ├── schema.py
    └── sqlgen.py
```

The migration package is optional at import time; `import pyzzel` does not import it. The `scripts/` directory contains local measurement and demo scripts and is not included in the installed package.

## Tests and contribution

Run the test suite from the project checkout:

```console
python -m pytest -q
```

Tests requiring PostgreSQL read `PYZZEL_TEST_DATABASE_URL` and are skipped if it is unset. Use a dedicated disposable test database.

A separate contribution process is not documented in the repository yet.

## License

Pyzzel is licensed under the [MIT License](LICENSE).

