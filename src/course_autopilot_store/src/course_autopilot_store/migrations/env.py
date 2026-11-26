"""Alembic environment: migrate over the connection ``Database.upgrade`` passes in."""

from alembic import context

from course_autopilot_store.models import Base

connection = context.config.attributes.get("connection")
if connection is None:  # pragma: no cover (only when alembic's CLI is run without a connection)
    msg = "Run migrations through course_autopilot_store.Database.upgrade()"
    raise RuntimeError(msg)

context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
