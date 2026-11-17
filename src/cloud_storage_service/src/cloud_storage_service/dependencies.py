"""How routes get the client for each vertical.

The app builds one client per vertical at startup (see ``app.py``) and keeps them on
``app.state``. Routes receive them through the functions below, so they only ever see the
contract types (``Client``, ``DocumentStorage``, ``IssueProvider``, ``CalendarService``), and tests
replace them with ``app.dependency_overrides[...]``.
"""

from typing import Annotated

import cloud_storage_client_api
from calendar_contract import CalendarService
from cloud_storage_api import DocumentStorage
from fastapi import Depends, Request
from issue_tracker_contract import IssueProvider


def get_storage_client(request: Request) -> cloud_storage_client_api.Client:
    """Return the storage client created at startup."""
    client: cloud_storage_client_api.Client = request.app.state.storage_client
    return client


def get_document_storage(request: Request) -> DocumentStorage:
    """Return the shared-contract view of the same storage client (used by the workflow)."""
    storage: DocumentStorage = request.app.state.document_storage
    return storage


def get_issue_tracker(request: Request) -> IssueProvider:
    """Return the issue tracker created at startup (chosen by ``ISSUE_TRACKER_IMPL``)."""
    issue_tracker: IssueProvider = request.app.state.issue_tracker
    return issue_tracker


def get_calendar(request: Request) -> CalendarService:
    """Return the calendar created at startup (chosen by ``CALENDAR_IMPL``)."""
    calendar: CalendarService = request.app.state.calendar
    return calendar


StorageClient = Annotated[cloud_storage_client_api.Client, Depends(get_storage_client)]
DocumentStore = Annotated[DocumentStorage, Depends(get_document_storage)]
IssueTracker = Annotated[IssueProvider, Depends(get_issue_tracker)]
Calendar = Annotated[CalendarService, Depends(get_calendar)]
