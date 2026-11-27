"""Course read routes (#88) and conversation storage (#92), over HTTP, with two users.

Each rule has a valid control request next to the refused one, so refusing everything
can't pass: T1 (another user's data is 404) and T11 (reads stay inside the course folder).
"""

from collections.abc import Iterator
from typing import Any

import pytest
from course_autopilot_store import Database
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from cloud_storage_service.app import app
from cloud_storage_service.courses import MAX_DOCUMENT_BYTES, MAX_TEXT_CHARACTERS
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    FailingCalendar,
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR

ALICE = {"X-Dev-User": "alice"}
BOB = {"X-Dev-User": "bob"}
COURSE = "CS-GY 6083"
FOLDER = f"/Course Autopilot/{COURSE}"


def make_pdf(text: str | None) -> bytes:
    """Return a one-page PDF showing ``text``, or a page with no text layer when ``None``."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


class Verticals:
    """Controlled implementations; tests swap them."""

    def __init__(self) -> None:
        """Start with empty, working implementations."""
        self.storage = InMemoryDocumentStorage()
        self.issues: Any = InMemoryIssueTracker()
        self.calendar: Any = InMemoryCalendar()


@pytest.fixture
def database() -> Iterator[Database]:
    """Provide a migrated in-memory database with foreign keys enforced."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    db = Database(engine)
    db.upgrade()
    yield db
    engine.dispose()


@pytest.fixture
def v(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[Verticals]:
    """Wire the app to the database and controlled verticals, with dev sign-in on."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    monkeypatch.delenv("K_SERVICE", raising=False)
    verticals = Verticals()
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_document_storage] = lambda: verticals.storage
    app.dependency_overrides[get_issue_tracker] = lambda: verticals.issues
    app.dependency_overrides[get_calendar] = lambda: verticals.calendar
    yield verticals
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return a client that doesn't run the lifespan."""
    return TestClient(app)


def run_proposal(http: TestClient, *changes: dict[str, Any], who: dict[str, str] = ALICE) -> None:
    """Create, approve and execute a proposal; every change must complete."""
    created = http.post("/proposals", json={"changes": list(changes)}, headers=who).json()
    http.post(
        f"/proposals/{created['id']}/approve",
        json={"changes_hash": created["changes_hash"]},
        headers=who,
    )
    body = http.post(f"/proposals/{created['id']}/execute", headers=who).json()
    assert body["status"] == "done", body


EVENT = {
    "op": "create_event",
    "course": COURSE,
    "title": "Midterm",
    "start": "2026-12-10T14:00:00-05:00",
    "end": "2026-12-10T15:30:00-05:00",
}
ISSUE = {"op": "create_issue", "course": COURSE, "title": "[CS-GY 6083] Midterm"}


# ----- Documents (#88, T11) -----


def test_lists_a_course_folders_files(v: Verticals, http: TestClient) -> None:
    """Lists a course folders files."""
    v.storage.files[f"{FOLDER}/syllabus.pdf"] = make_pdf("Midterm Dec 10")
    v.storage.files[f"{FOLDER}/photo.png"] = b"\x89PNG"
    v.storage.files[f"{FOLDER}/old/notes.md"] = b"# old"

    response = http.get(f"/courses/{COURSE}/documents", headers=ALICE)

    assert response.status_code == 200
    assert [(d["name"], d["readable"]) for d in response.json()] == [
        ("photo.png", False),
        ("syllabus.pdf", True),
    ]


def test_reads_a_pdf_in_the_course_folder(v: Verticals, http: TestClient) -> None:
    """Reads a pdf in the course folder."""
    v.storage.files[f"{FOLDER}/syllabus.pdf"] = make_pdf("Midterm exam on December 10")

    response = http.get(f"/courses/{COURSE}/documents/syllabus.pdf", headers=ALICE)

    assert response.status_code == 200
    body = response.json()
    assert "Midterm exam on December 10" in body["text"]
    assert body["truncated"] is False


def test_reads_markdown_and_caps_long_text(v: Verticals, http: TestClient) -> None:
    """Reads markdown and caps long text."""
    v.storage.files[f"{FOLDER}/notes.md"] = ("x" * (MAX_TEXT_CHARACTERS + 5)).encode()

    body = http.get(f"/courses/{COURSE}/documents/notes.md", headers=ALICE).json()

    assert len(body["text"]) == MAX_TEXT_CHARACTERS
    assert body["characters"] == MAX_TEXT_CHARACTERS + 5
    assert body["truncated"] is True


@pytest.mark.parametrize(
    "path",
    [
        f"/courses/{COURSE}/documents/..%2F..%2Fother%2Fx.pdf",
        f"/courses/{COURSE}/documents/%2Fetc%2Fpasswd.txt",
        f"/courses/{COURSE}/documents/..",
        f"/courses/{COURSE}/documents/.hidden.md",
        "/courses/../documents/x.pdf".replace("../", "..%2F"),
        "/courses/../documents",
    ],
)
def test_paths_outside_the_course_folder_are_refused(
    v: Verticals,
    http: TestClient,
    path: str,
) -> None:
    """Paths outside the course folder are refused."""
    v.storage.files["/Course Autopilot/other/x.pdf"] = make_pdf("secret")
    v.storage.files["/etc/passwd.txt"] = b"root"

    response = http.get(path, headers=ALICE)

    assert response.status_code in {400, 404}
    assert "secret" not in response.text
    assert "root" not in response.text


def test_traversal_in_the_name_is_400(v: Verticals, http: TestClient) -> None:
    """Traversal in the name is 400."""
    v.storage.files["/Course Autopilot/other/x.pdf"] = make_pdf("secret")

    response = http.get(f"/courses/{COURSE}/documents/..%2Fother%2Fx.pdf", headers=ALICE)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_a_scanned_pdf_is_a_clear_error(v: Verticals, http: TestClient) -> None:
    """A scanned pdf is a clear error."""
    v.storage.files[f"{FOLDER}/scan.pdf"] = make_pdf(None)

    response = http.get(f"/courses/{COURSE}/documents/scan.pdf", headers=ALICE)

    assert response.status_code == 422
    assert "no text layer" in response.json()["error"]["message"]


def test_a_damaged_pdf_is_a_clear_error(v: Verticals, http: TestClient) -> None:
    """A damaged pdf is a clear error."""
    v.storage.files[f"{FOLDER}/broken.pdf"] = b"%PDF-1.4 not really"

    response = http.get(f"/courses/{COURSE}/documents/broken.pdf", headers=ALICE)

    assert response.status_code == 422


def test_an_oversized_file_is_413_and_not_downloaded(
    v: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized file is 413 and not downloaded."""
    v.storage.files[f"{FOLDER}/huge.txt"] = b"x" * (MAX_DOCUMENT_BYTES + 1)
    monkeypatch.setattr(v.storage, "download", lambda _p: pytest.fail("downloaded"))

    response = http.get(f"/courses/{COURSE}/documents/huge.txt", headers=ALICE)

    assert response.status_code == 413


def test_an_unsupported_type_is_422(v: Verticals, http: TestClient) -> None:
    """An unsupported type is 422."""
    v.storage.files[f"{FOLDER}/photo.png"] = b"\x89PNG"

    response = http.get(f"/courses/{COURSE}/documents/photo.png", headers=ALICE)

    assert response.status_code == 422


def test_a_missing_file_or_folder_is_404(v: Verticals, http: TestClient) -> None:
    """A missing file or folder is 404."""
    assert http.get(f"/courses/{COURSE}/documents", headers=ALICE).status_code == 404
    assert http.get(f"/courses/{COURSE}/documents/x.pdf", headers=ALICE).status_code == 404


def test_storage_failures_never_leak_provider_text(
    v: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage failures never leak provider text."""
    from cloud_storage_api.errors import (
        PermissionDeniedError,
        ProviderError,
        ProviderUnavailableError,
    )

    for error, status in [
        (ProviderUnavailableError("token=abc"), 503),
        (ProviderError("token=abc"), 502),
        (PermissionDeniedError("token=abc"), 403),
    ]:

        def fail(_path: str, error: Exception = error) -> None:
            raise error

        monkeypatch.setattr(v.storage, "list_files", fail)
        response = http.get(f"/courses/{COURSE}/documents", headers=ALICE)
        assert response.status_code == status
        assert "token" not in response.text


def test_document_routes_need_sign_in(v: Verticals, http: TestClient) -> None:
    """Document routes need sign in."""
    assert http.get(f"/courses/{COURSE}/documents").status_code == 401


# ----- Courses and plans (T1) -----


def test_lists_courses_from_folders_and_records(v: Verticals, http: TestClient) -> None:
    """Document routes need sign in."""
    v.storage.files[f"{FOLDER}/syllabus.pdf"] = b"x"
    v.storage.files["/Course Autopilot/MA-UY 1024/syllabus.pdf"] = b"x"
    run_proposal(http, EVENT, ISSUE)

    body = http.get("/courses", headers=ALICE).json()

    assert body == [
        {"course": COURSE, "has_folder": True, "events": 1, "issues": 1, "documents": 0},
        {"course": "MA-UY 1024", "has_folder": True, "events": 0, "issues": 0, "documents": 0},
    ]


def test_courses_without_a_root_folder_is_empty(v: Verticals, http: TestClient) -> None:
    """Courses without a root folder is empty."""
    assert http.get("/courses", headers=ALICE).json() == []


def test_the_plan_reads_each_record_back(v: Verticals, http: TestClient) -> None:
    """Courses without a root folder is empty."""
    run_proposal(http, EVENT, ISSUE)

    body = http.get(f"/courses/{COURSE}/plan", headers=ALICE).json()

    assert [(i["kind"], i["title"], i["available"]) for i in body["items"]] == [
        ("event", "Midterm", True),
        ("issue", "[CS-GY 6083] Midterm", True),
    ]
    assert body["items"][0]["start"] == EVENT["start"]


def test_the_plan_marks_unavailable_records(v: Verticals, http: TestClient) -> None:
    """The plan marks unavailable records."""
    run_proposal(http, EVENT, ISSUE)
    v.calendar = FailingCalendar()

    items = http.get(f"/courses/{COURSE}/plan", headers=ALICE).json()["items"]

    assert [(i["kind"], i["available"]) for i in items] == [("event", False), ("issue", True)]


def test_another_users_plan_and_courses_are_empty(v: Verticals, http: TestClient) -> None:
    """Another users plan and courses are empty."""
    run_proposal(http, EVENT, ISSUE)

    assert http.get(f"/courses/{COURSE}/plan", headers=BOB).json()["items"] == []
    assert http.get("/courses", headers=BOB).json() == []
    # Control: the owner sees both records.
    assert len(http.get(f"/courses/{COURSE}/plan", headers=ALICE).json()["items"]) == 2


def test_a_bad_course_name_is_400(v: Verticals, http: TestClient) -> None:
    """A bad course name is 400."""
    assert http.get("/courses/..%5Cx/plan", headers=ALICE).status_code == 400
    assert http.get("/courses/ x/documents", headers=ALICE).status_code == 400


# ----- Conversations (#92, T1) -----


def test_appends_and_lists_messages_in_order(v: Verticals, http: TestClient) -> None:
    """Appends and lists messages in order."""
    cid = http.post("/conversations", json={"title": "CS-GY 6083"}, headers=ALICE).json()["id"]

    first = http.post(
        f"/conversations/{cid}/messages",
        json={"messages": [{"role": "user", "content": {"text": "Set up my course"}}]},
        headers=ALICE,
    )
    second = http.post(
        f"/conversations/{cid}/messages",
        json={
            "messages": [
                {"role": "assistant", "content": {"text": "Is the midterm Oct 13 or 15?"}},
                {"role": "user", "content": {"text": "Oct 15"}},
            ],
        },
        headers=ALICE,
    )

    assert first.status_code == second.status_code == 201
    listed = http.get(f"/conversations/{cid}/messages", headers=ALICE).json()
    assert [(m["seq"], m["role"], m["content"]["text"]) for m in listed] == [
        (0, "user", "Set up my course"),
        (1, "assistant", "Is the midterm Oct 13 or 15?"),
        (2, "user", "Oct 15"),
    ]


def test_another_users_conversation_is_404(v: Verticals, http: TestClient) -> None:
    """Another users conversation is 404."""
    cid = http.post("/conversations", json={}, headers=ALICE).json()["id"]
    message = {"messages": [{"role": "user", "content": {"text": "hi"}}]}

    assert http.get(f"/conversations/{cid}/messages", headers=BOB).status_code == 404
    assert http.post(f"/conversations/{cid}/messages", json=message, headers=BOB).status_code == 404
    # Control: the owner can.
    assert (
        http.post(f"/conversations/{cid}/messages", json=message, headers=ALICE).status_code == 201
    )


def test_a_proposal_cant_attach_to_another_users_conversation(
    v: Verticals,
    http: TestClient,
) -> None:
    """Another users plan and courses are empty."""
    cid = http.post("/conversations", json={}, headers=ALICE).json()["id"]
    body = {"changes": [ISSUE], "conversation_id": cid}

    assert http.post("/proposals", json=body, headers=BOB).status_code == 400
    assert http.post("/proposals", json=body, headers=ALICE).status_code == 201


def test_invalid_messages_are_400(v: Verticals, http: TestClient) -> None:
    """Invalid messages are 400."""
    cid = http.post("/conversations", json={}, headers=ALICE).json()["id"]

    for messages in (
        [],
        [{"role": "system", "content": {}}],
        [{"role": "user", "content": {"text": "x" * 300_000}}],
    ):
        response = http.post(
            f"/conversations/{cid}/messages",
            json={"messages": messages},
            headers=ALICE,
        )
        assert response.status_code == 400, messages


def test_a_concurrent_append_is_409(
    v: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent append is 409."""
    from cloud_storage_service import conversation_routes

    cid = http.post("/conversations", json={}, headers=ALICE).json()["id"]
    message = {"messages": [{"role": "user", "content": {"text": "hi"}}]}
    http.post(f"/conversations/{cid}/messages", json=message, headers=ALICE)
    # Another append "took" seq 0 between our read of the last seq and our insert.
    monkeypatch.setattr(conversation_routes, "func", _MaxIsAlwaysNone())

    response = http.post(f"/conversations/{cid}/messages", json=message, headers=ALICE)

    assert response.status_code == 409


class _MaxIsAlwaysNone:
    """Stand-in for ``sqlalchemy.func`` whose ``max`` finds no rows."""

    @staticmethod
    def max(_column: object) -> object:
        from sqlalchemy import null

        return null()
