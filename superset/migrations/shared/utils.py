# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
import os
import time
from collections.abc import Iterator
from typing import Any, Callable, Optional, Union
from uuid import uuid4

from alembic import op
from sqlalchemy import (
    Column,
    inspect,
    JSON,
    MetaData,
    select,
    String,
    Table,
    text,
    update,
)
from sqlalchemy.dialects.mysql.base import MySQLDialect
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect  # noqa: E402
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.schema import SchemaItem

from superset.utils import json

GREEN = "\033[32m"
RESET = "\033[0m"
YELLOW = "\033[33m"
RED = "\033[31m"
LRED = "\033[91m"

logger = logging.getLogger("alembic.env")

DEFAULT_BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 1000))


def get_table_column(
    table_name: str,
    column_name: str,
) -> Optional[list[dict[str, Any]]]:
    """
    Get the specified column.

    :param table_name: The Table name
    :param column_name: The column name
    :returns: The column
    """

    insp = inspect(op.get_context().bind)

    try:
        for column in insp.get_columns(table_name):
            if column["name"] == column_name:
                return column
    except NoSuchTableError:
        pass

    return None


def table_has_column(table_name: str, column_name: str) -> bool:
    """
    Checks if a column exists in a given table.

    :param table_name: A table name
    :param column_name: A column name
    :returns: True iff the column exists in the table
    """

    return bool(get_table_column(table_name, column_name))


def table_has_index(table: str, index: str) -> bool:
    """
    Checks if an index exists in a given table.

    :param table: A table name
    :param index: A index name
    :returns: True if the index exists in the table
    """

    insp = inspect(op.get_context().bind)

    try:
        return any(ind["name"] == index for ind in insp.get_indexes(table))
    except NoSuchTableError:
        return False


uuid_by_dialect = {
    MySQLDialect: "UNHEX(REPLACE(CONVERT(UUID() using utf8mb4), '-', ''))",
    PGDialect: "uuid_in(md5(random()::text || clock_timestamp()::text)::cstring)",
}


def assign_uuids(
    model: Any, session: Session, batch_size: int = DEFAULT_BATCH_SIZE
) -> None:
    """Generate new UUIDs for all rows in a table"""
    bind = op.get_bind()
    table_name = model.__tablename__
    count = session.query(model).count()
    # silently skip if the table is empty (suitable for db initialization)
    if count == 0:
        return

    start_time = time.time()
    print(f"\nAdding uuids for `{table_name}`...")
    # Use dialect specific native SQL queries if possible
    for dialect, sql in uuid_by_dialect.items():
        if isinstance(bind.dialect, dialect):
            op.execute(
                f"UPDATE {dialect().identifier_preparer.quote(table_name)} SET uuid = {sql}"  # noqa: S608, E501
            )
            print(f"Done. Assigned {count} uuids in {time.time() - start_time:.3f}s.\n")
            return

    for obj in paginated_update(
        session.query(model),
        lambda current, total: print(
            f"  uuid assigned to {current} out of {total}", end="\r"
        ),
        batch_size=batch_size,
    ):
        obj.uuid = uuid4
    print(f"Done. Assigned {count} uuids in {time.time() - start_time:.3f}s.\n")


def paginated_update(
    query: Query,
    print_page_progress: Optional[Union[Callable[[int, int], None], bool]] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[Any]:
    """
    Update models in small batches so we don't have to load everything in memory.
    """

    total = query.count()
    processed = 0
    session: Session = inspect(query).session
    result = session.execute(query)

    if print_page_progress is None or print_page_progress is True:
        print_page_progress = lambda processed, total: print(  # noqa: E731
            f"    {processed}/{total}", end="\r"
        )

    while True:
        rows = result.fetchmany(batch_size)

        if not rows:
            break

        for row in rows:
            yield row[0]

        session.commit()
        processed += len(rows)

        if print_page_progress:
            print_page_progress(processed, total)


def try_load_json(data: Optional[str]) -> dict[str, Any]:
    return data and json.loads(data) or {}


def has_table(table_name: str) -> bool:
    """
    Check if a table exists in the database.

    :param table_name: The table name
    :returns: True if the table exists
    """

    insp = inspect(op.get_context().bind)
    table_exists = insp.has_table(table_name)

    return table_exists


def drop_fks_for_table(
    table_name: str, foreign_key_names: list[str] | None = None
) -> None:
    """
    Drop specific or all foreign key constraints for a table
    if they exist and the database is not sqlite.

    :param table_name: The table name to drop foreign key constraints from
    :param foreign_key_names: Optional list of specific foreign key names to drop.
    If None is provided, all will be dropped.
    """
    connection = op.get_bind()
    inspector = Inspector.from_engine(connection)

    if isinstance(connection.dialect, SQLiteDialect):
        return  # sqlite doesn't like constraints

    if has_table(table_name):
        existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(table_name)}

        # What to delete based on whether the list was passed
        if foreign_key_names is not None:
            foreign_key_names = list(set(foreign_key_names) & existing_fks)
        else:
            foreign_key_names = list(existing_fks)

        for fk_name in foreign_key_names:
            logger.info(
                f"Dropping foreign key {GREEN}{fk_name}{RESET} from table {GREEN}{table_name}{RESET}..."  # noqa: E501
            )
            op.drop_constraint(fk_name, table_name, type_="foreignkey")


def create_table(table_name: str, *columns: SchemaItem, **kwargs: Any) -> None:
    """
    Creates a database table with the specified name and columns.

    This function checks if a table with the given name already exists in the database.
    If the table already exists, it logs an informational.
    Otherwise, it proceeds to create a new table using the provided name
    and schema columns.

    :param table_name: The name of the table to be created.
    :param columns: A variable number of arguments representing the schema
    just like when calling alembic's method create_table()
    """
    if has_table(table_name=table_name):
        logger.info(f"Table {LRED}{table_name}{RESET} already exists. Skipping...")
        return

    logger.info(f"Creating table {GREEN}{table_name}{RESET}...")
    op.create_table(table_name, *columns, **kwargs)
    logger.info(f"Table {GREEN}{table_name}{RESET} created.")


def drop_table(table_name: str) -> None:
    """
    Drops a database table with the specified name.

    This function checks if a table with the given name exists in the database.
    If the table does not exist, it logs an informational message and skips the dropping process.
    If the table exists, it first attempts to drop all foreign key constraints associated with the table
    (handled by `drop_fks_for_table`) and then proceeds to drop the table.

    :param table_name: The name of the table to be dropped.
    """  # noqa: E501

    if not has_table(table_name=table_name):
        logger.info(f"Table {GREEN}{table_name}{RESET} doesn't exist. Skipping...")
        return

    logger.info(f"Dropping table {GREEN}{table_name}{RESET}...")
    drop_fks_for_table(table_name)
    op.drop_table(table_name=table_name)
    logger.info(f"Table {GREEN}{table_name}{RESET} dropped.")


def batch_operation(
    callable: Callable[[int, int], None], count: int, batch_size: int
) -> None:
    """
    Executes an operation by dividing a task into smaller batches and tracking progress.

    This function is designed to process a large number of items in smaller batches. It takes a callable
    that performs the operation on each batch. The function logs the progress of the operation as it processes
    through the batches.

    If count is set to 0 or lower, it logs an informational message and skips the batch process.

    :param callable: A callable function that takes two integer arguments:
    the start index and the end index of the current batch.
    :param count: The total number of items to process.
    :param batch_size: The number of items to process in each batch.
    """  # noqa: E501
    if count <= 0:
        logger.info(
            f"No records to process in batch {LRED}(count <= 0){RESET} for callable {LRED}other_callable_example{RESET}. Skipping..."  # noqa: E501
        )
        return
    for offset in range(0, count, batch_size):
        percentage = (offset / count) * 100 if count else 0
        logger.info(f"Progress: {offset:,}/{count:,} ({percentage:.2f}%)")
        callable(offset, min(offset + batch_size, count))

    logger.info(f"Progress: {count:,}/{count:,} (100%)")
    logger.info(
        f"End: {GREEN}{callable.__name__}{RESET} batch operation {GREEN}successfully{RESET} executed."  # noqa: E501
    )


def add_columns(table_name: str, *columns: Column) -> None:
    """
    Adds new columns to an existing database table.

    If a column already exist, or the table doesn't exist, it logs an informational message and skips the adding process.
    Otherwise, it proceeds to add the new column to the table.

    The operation is performed using Alembic's batch_alter_table.

    :param table_name: The name of the table to which the columns will be added.
    :param columns: A list of SQLAlchemy Column objects that define the name, type, and other attributes of the columns to be added.
    """  # noqa: E501

    cols_to_add = []
    for col in columns:
        if table_has_column(table_name=table_name, column_name=col.name):
            logger.info(
                f"Column {LRED}{col.name}{RESET} already present on table {LRED}{table_name}{RESET}. Skipping..."  # noqa: E501
            )
        else:
            cols_to_add.append(col)

    with op.batch_alter_table(table_name) as batch_op:
        for col in cols_to_add:
            logger.info(
                f"Adding column {GREEN}{col.name}{RESET} to table {GREEN}{table_name}{RESET}..."  # noqa: E501
            )
            batch_op.add_column(col)


def drop_columns(table_name: str, *columns: str) -> None:
    """
    Drops specified columns from an existing database table.

    If a column or table does not exist, it logs an informational message and skips the dropping process.
    Otherwise, it proceeds to remove the column from the table.

    The operation is performed using Alembic's batch_alter_table.

    :param table_name: The name of the table from which the columns will be removed.
    :param columns: A list of column names to be dropped.
    """  # noqa: E501

    cols_to_drop = []
    for col in columns:
        if not table_has_column(table_name=table_name, column_name=col):
            logger.info(
                f"Column {LRED}{col}{RESET} is not present on table {LRED}{table_name}{RESET}. Skipping..."  # noqa: E501
            )
        else:
            cols_to_drop.append(col)

    with op.batch_alter_table(table_name) as batch_op:
        for col in cols_to_drop:
            logger.info(
                f"Dropping column {GREEN}{col}{RESET} from table {GREEN}{table_name}{RESET}..."  # noqa: E501
            )
            batch_op.drop_column(col)


def create_index(
    table_name: str, index_name: str, columns: list[str], *, unique: bool = False
) -> None:
    """
    Creates an index on specified columns of an existing database table.

    If the index already exists, it logs an informational message and skips the creation process.
    Otherwise, it proceeds to create a new index with the specified name on the given columns of the table.

    :param table_name: The name of the table on which the index will be created.
    :param index_name: The name of the index to be created.
    :param columns: A list of column names for which the index will be created
    :param unique: If True, create a unique index.
    """  # noqa: E501

    if table_has_index(table=table_name, index=index_name):
        logger.info(
            f"Table {LRED}{table_name}{RESET} already has index {LRED}{index_name}{RESET}. Skipping..."  # noqa: E501
        )
        return

    logger.info(
        f"Creating index {GREEN}{index_name}{RESET} on table {GREEN}{table_name}{RESET}"
    )

    op.create_index(
        table_name=table_name,
        index_name=index_name,
        unique=unique,
        columns=columns,
    )


def drop_index(table_name: str, index_name: str) -> None:
    """
    Drops an index from an existing database table.

    If the index does not exists, it logs an informational message and skips the dropping process.
    Otherwise, it proceeds with the removal operation.

    :param table_name: The name of the table from which the index will be dropped.
    :param index_name: The name of the index to be dropped.
    """  # noqa: E501

    if not table_has_index(table=table_name, index=index_name):
        logger.info(
            f"Table {LRED}{table_name}{RESET} doesn't have index {LRED}{index_name}{RESET}. Skipping..."  # noqa: E501
        )
        return

    logger.info(
        f"Dropping index {GREEN}{index_name}{RESET} from table {GREEN}{table_name}{RESET}..."  # noqa: E501
    )

    op.drop_index(table_name=table_name, index_name=index_name)


def create_fks_for_table(
    foreign_key_name: str,
    table_name: str,
    referenced_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    ondelete: Optional[str] = None,
) -> None:
    """
    Create a foreign key constraint for a table, ensuring compatibility with sqlite.

    :param foreign_key_name: Foreign key constraint name.
    :param table_name: The name of the table where the foreign key will be created.
    :param referenced_table: The table the FK references.
    :param local_cols: Column names in the current table.
    :param remote_cols: Column names in the referenced table.
    :param ondelete: (Optional) The ON DELETE action (e.g., "CASCADE", "SET NULL").
    """
    connection = op.get_bind()

    if not has_table(table_name):
        logger.warning(
            f"Table {LRED}{table_name}{RESET} does not exist. Skipping foreign key creation."  # noqa: E501
        )
        return

    if isinstance(connection.dialect, SQLiteDialect):
        # SQLite requires batch mode since ALTER TABLE is limited
        with op.batch_alter_table(table_name) as batch_op:
            logger.info(
                f"Creating foreign key {GREEN}{foreign_key_name}{RESET} on table {GREEN}{table_name}{RESET} (SQLite mode)..."  # noqa: E501
            )
            batch_op.create_foreign_key(
                foreign_key_name,
                referenced_table,
                local_cols,
                remote_cols,
                ondelete=ondelete,
            )
    else:
        # Standard FK creation for other databases
        logger.info(
            f"Creating foreign key {GREEN}{foreign_key_name}{RESET} on table {GREEN}{table_name}{RESET}..."  # noqa: E501
        )
        op.create_foreign_key(
            foreign_key_name,
            table_name,
            referenced_table,
            local_cols,
            remote_cols,
            ondelete=ondelete,
        )


def cast_text_column_to_json(
    table: str,
    column: str,
    pk: str = "id",
    nullable: bool = True,
    suffix: str = "_tmp",
) -> None:
    """
    Cast a text column to JSON.

    SQLAlchemy now has a nice abstraction for JSON columns, even if the underlying
    database doesn't support the type natively. We should always use it when storing
    JSON payloads.

    :param table: The name of the table.
    :param column: The name of the column to be cast.
    :param pk: The name of the primary key column.
    :param nullable: Whether the new column should be nullable.
    :param suffix: The suffix to be added to the temporary column name.
    """
    conn = op.get_bind()

    if isinstance(conn.dialect, PGDialect):
        conn.execute(
            text(
                f"""
CREATE OR REPLACE FUNCTION safe_to_jsonb(input text)
  RETURNS jsonb
  LANGUAGE plpgsql
  IMMUTABLE
AS $$
BEGIN
  RETURN input::jsonb;
EXCEPTION WHEN invalid_text_representation THEN
  RETURN NULL;
END;
$$;

ALTER TABLE {table}
ALTER COLUMN {column} TYPE jsonb
USING safe_to_jsonb({column});
                """
            )
        )
        return

    tmp_column = column + suffix
    op.add_column(
        table,
        Column(tmp_column, JSON(), nullable=nullable),
    )

    meta = MetaData()
    t = Table(table, meta, autoload_with=conn)
    stmt_select = select(t.c[pk], t.c[column]).where(t.c[column].is_not(None))

    for row_pk, value in conn.execute(stmt_select):
        try:
            json.loads(value)
        except json.JSONDecodeError:
            logger.warning(
                f"Invalid JSON value in column {column} for {pk}={row_pk}: {value}"
            )
            continue
        stmt_update = update(t).where(t.c[pk] == row_pk).values({tmp_column: value})
        conn.execute(stmt_update)

    op.drop_column(table, column)
    op.alter_column(table, tmp_column, existing_type=JSON(), new_column_name=column)

    return


def cast_json_column_to_text(
    table: str,
    column: str,
    pk: str = "id",
    nullable: bool = True,
    suffix: str = "_tmp",
    length: int = 128,
) -> None:
    """
    Cast a JSON column back to text.

    :param table: The name of the table.
    :param column: The name of the column to be cast.
    :param pk: The name of the primary key column.
    :param nullable: Whether the new column should be nullable.
    :param suffix: The suffix to be added to the temporary column name.
    :param length: The length of the text column.
    """
    conn = op.get_bind()

    if isinstance(conn.dialect, PGDialect):
        conn.execute(
            text(
                f"""
                ALTER TABLE {table}
                ALTER COLUMN {column} TYPE text
                USING {column}::text
                """
            )
        )
        return

    tmp_column = column + suffix
    op.add_column(
        table,
        Column(tmp_column, String(length=length), nullable=nullable),
    )

    meta = MetaData()
    t = Table(table, meta, autoload_with=conn)
    stmt_select = select(t.c[pk], t.c[column]).where(t.c[column].is_not(None))

    for row_pk, value in conn.execute(stmt_select):
        stmt_update = (
            update(t).where(t.c[pk] == row_pk).values({tmp_column: json.dumps(value)})
        )
        conn.execute(stmt_update)

    op.drop_column(table, column)
    op.alter_column(
        table,
        tmp_column,
        existing_type=String(length=length),
        new_column_name=column,
    )

    return
