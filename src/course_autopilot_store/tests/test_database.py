"""``Database``: migrations, sessions and the constraints the services rely on.

These run on SQLite in memory, so CI needs no database server. The migration is also run
against the Neon test branch by hand (see the pull request); Postgres-only behaviour, the
advisory lock, is checked there.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from course_autopilot_store import Database
from course_autopilot_store.database import _sqlalchemy_url
from course_autopilot_store.models import (
    Approval,
    Base,
    Conversation,
    ExternalRecord,
    Message,
    Proposal,
    ProviderConnection,
    User,
)

TABLES = {
    "users",
    "provider_connections",
    "conversations",
    "messages",
    "proposals",
    "approvals",
    "change_outcomes",
    "external_records",
    "sessions",
    "sign_in_attempts",
    "connection_attempts",
}


def _sqlite_engine() -> Engine:
    # One shared in-memory database for every connection, with foreign keys enforced as
    # Postgres does.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def database() -> Iterator[Database]:
    """Provide a migrated, empty database."""
    db = Database(_sqlite_engine())
    db.upgrade()
    yield db
    db.engine.dispose()


def _user(db: Database, subject: str = "sub-a") -> uuid.UUID:
    with db.session() as session:
        user = User(auth_provider="google", auth_subject=subject)
        session.add(user)
        session.flush()
        return user.id


def test_the_migration_creates_every_table(database: Database) -> None:
    """Upgrading an empty database creates exactly the tables the models describe."""
    tables = set(inspect(database.engine).get_table_names())

    assert tables - {"alembic_version"} == TABLES


def test_the_migration_matches_the_models(database: Database) -> None:
    """A model change without a migration fails here, so the two never drift apart."""
    with database.engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert differences == []


def test_upgrading_twice_changes_nothing(database: Database) -> None:
    """A second instance starting later finds nothing left to apply."""
    database.upgrade()

    assert set(inspect(database.engine).get_table_names()) - {"alembic_version"} == TABLES


def test_check_succeeds_on_a_reachable_database(database: Database) -> None:
    """``check`` runs one query and raises nothing when the database answers."""
    database.check()


def test_a_session_commits_on_success(database: Database) -> None:
    """Rows written in a session that ends normally are there afterwards."""
    user_id = _user(database)

    with database.session() as session:
        assert session.get(User, user_id) is not None


def test_a_session_rolls_back_on_error(database: Database) -> None:
    """An error inside the session leaves nothing behind."""

    def insert_then_fail() -> None:
        with database.session() as session:
            session.add(User(auth_provider="google", auth_subject="rolled-back"))
            session.flush()
            msg = "fail after the insert"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        insert_then_fail()

    with database.session() as session:
        assert session.scalars(select(User)).all() == []


def test_one_account_per_sign_in_identity(database: Database) -> None:
    """The same provider subject can't create a second account."""
    _user(database, "same")

    with pytest.raises(IntegrityError):
        _user(database, "same")


def test_one_connection_per_user_and_provider(database: Database) -> None:
    """Reconnecting a provider must update the connection, not add a second one."""
    user_id = _user(database)
    with database.session() as session:
        session.add(
            ProviderConnection(user_id=user_id, provider="trello", encrypted_credentials=b"x"),
        )

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            ProviderConnection(user_id=user_id, provider="trello", encrypted_credentials=b"y"),
        )


def test_a_proposal_has_at_most_one_approval(database: Database) -> None:
    """Approval is recorded once per proposal; a changed proposal is a new proposal."""
    user_id = _user(database)
    with database.session() as session:
        proposal = Proposal(user_id=user_id, changes=[{"op": "create"}], changes_hash="h1")
        session.add(proposal)
        session.flush()
        session.add(Approval(proposal_id=proposal.id, user_id=user_id, changes_hash="h1"))
        proposal_id = proposal.id

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(Approval(proposal_id=proposal_id, user_id=user_id, changes_hash="h1"))


def test_message_order_is_unique_within_a_conversation(database: Database) -> None:
    """Two turns can't claim the same position in one conversation."""
    user_id = _user(database)
    with database.session() as session:
        conversation = Conversation(user_id=user_id)
        session.add(conversation)
        session.flush()
        session.add(
            Message(conversation_id=conversation.id, seq=1, role="user", content={"text": "hi"}),
        )
        conversation_id = conversation.id

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            Message(conversation_id=conversation_id, seq=1, role="user", content={"text": "x"}),
        )


def test_deleting_a_user_deletes_everything_they_own(database: Database) -> None:
    """Removing an account removes its connections, conversations, proposals and records."""
    user_id = _user(database)
    with database.session() as session:
        session.add(
            ProviderConnection(user_id=user_id, provider="dropbox", encrypted_credentials=b"x"),
        )
        session.add(Conversation(user_id=user_id))
        session.add(Proposal(user_id=user_id, changes=[], changes_hash="h"))
        session.add(
            ExternalRecord(
                user_id=user_id,
                course_key="cs101",
                kind="event",
                provider_record_id="e1",
            ),
        )

    with database.session() as session:
        session.delete(session.get(User, user_id))

    with database.session() as session:
        for model in (ProviderConnection, Conversation, Proposal, ExternalRecord):
            assert session.scalars(select(model)).all() == []


@pytest.mark.parametrize(
    ("given", "expected_driver"),
    [
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://"),
        ("postgres://u:p@host/db", "postgresql+psycopg://"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_plain_postgres_urls_use_the_psycopg_driver(given: str, expected_driver: str) -> None:
    """Neon gives ``postgresql://``; SQLAlchemy needs the driver named to use psycopg 3."""
    converted = _sqlalchemy_url(given)

    assert converted.startswith(expected_driver)
    assert "p@host" in converted or given == "sqlite://"


def test_from_url_builds_an_engine_without_connecting() -> None:
    """Building the database makes no connection, so startup never waits on it."""
    db = Database.from_url("postgresql://user:secret@db.invalid/neondb")

    assert db.engine.dialect.name == "postgresql"
    assert db.engine.dialect.driver == "psycopg"
