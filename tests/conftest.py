import os

# Set a dummy DSN so db/session.py can import without raising at collection time.
# Tests that need the DB override get_db_session via patch() instead.
os.environ.setdefault(
    "POSTGRES_DSN", "postgresql+asyncpg://test:test@localhost:5432/testdb"
)
