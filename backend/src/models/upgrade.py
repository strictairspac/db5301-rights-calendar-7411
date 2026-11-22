"""Idempotent schema upgrades applied at application startup.

The initial structure lives in ``src.models.tables`` and is created with
``MetaData.create_all``. Columns added after the first release cannot rely on
``create_all`` for existing databases, so the startup path runs the lightweight
checks in this module instead of a separate migration toolchain.
"""

from sqlalchemy import inspect, text

from src.models.tables import cases

# Columns added after the initial release, mapped to their DDL fragment.
ADDED_COLUMNS = {
    cases.name: {
        'cleaned_at': 'VARCHAR(32)',
    },
}


def ensure_schema(engine):
    inspector = inspect(engine)
    existing = {table: set(column['name'] for column in inspector.get_columns(table))
                for table in inspector.get_table_names()}
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            present = existing.get(table, set())
            for name, ddl_type in columns.items():
                if name not in present:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))
