"""Database access: one engine per service, short sessions, and migrations to the latest schema.

The services get the connection string from ``DATABASE_URL`` (Secret Manager in the
deployed services). Queries go through SQLAlchemy, so values are always sent as
parameters, never pasted into SQL text.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Self

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# Any constant works; it only has to be the same for every instance that migrates.
_MIGRATION_LOCK_ID = 7_310_001


def _sqlalchemy_url(url: str) -> str:
    """Use the psycopg 3 driver for a plain ``postgresql://`` URL, as Neon gives it."""
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


class Database:
    """One service's connection to the database."""

    def __init__(self, engine: Engine) -> None:
        """Wrap an engine; ``Database.from_url`` builds one from a connection string."""
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Connect lazily: no connection opens until the first query."""
        # pool_pre_ping replaces connections Neon closed while idle. A small pool, because
        # Neon's pooler already shares connections between instances.
        engine = create_engine(
            _sqlalchemy_url(url),
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=3,
        )
        return cls(engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Open a session that commits on success and rolls back on any error."""
        with self._sessions.begin() as session:
            yield session

    def check(self) -> None:
        """Run one trivial query. Raises a SQLAlchemy error if the database is unreachable."""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def upgrade(self) -> None:
        """Apply every migration not yet applied.

        On Postgres, an advisory lock makes instances that start together migrate one at a
        time; the others then find nothing left to apply.
        """
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:id)"),
                    {"id": _MIGRATION_LOCK_ID},
                )
            config = _alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")


def _alembic_config() -> Config:
    config = Config()
    location = resources.files("course_autopilot_store") / "migrations"
    config.set_main_option("script_location", str(location))
    return config
