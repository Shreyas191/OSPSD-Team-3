"""Tests for the cloud storage client API abstract base class.

These tests use mocks to document the contract every ``Client`` implementation
must follow. They double as examples of how consumers are expected to call the API.
"""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest

import cloud_storage_client_api
from cloud_storage_client_api import Client, File


def _mock_file(path: str, *, is_folder: bool = False) -> Mock:
    mock_file = Mock(spec=File)
    mock_file.path = path
    mock_file.name = path.rsplit("/", 1)[-1]
    mock_file.is_folder = is_folder
    return mock_file


def test_client_cannot_be_instantiated() -> None:
    """The Client is abstract: implementations must provide every method."""
    with pytest.raises(TypeError):
        Client()  # type: ignore[abstract]


def test_client_declares_all_crud_methods() -> None:
    """The contract exposes one abstract method per agreed CRUD feature."""
    expected = {
        "upload_file",
        "create_folder",
        "copy",
        "download_file",
        "get_metadata",
        "list_folder",
        "search",
        "rename",
        "move",
        "delete",
    }
    assert expected == Client.__abstractmethods__


def test_get_client_is_unbound_by_default() -> None:
    """The factory raises until an implementation registers itself."""
    from cloud_storage_client_api import client as client_module

    with pytest.raises(NotImplementedError):
        client_module.get_client()


# ----- Create -----


def test_client_upload_file() -> None:
    """``upload_file`` returns the metadata of the uploaded file."""
    mock_client = Mock(spec=Client)
    mock_client.upload_file.return_value = _mock_file("/Docs/report.pdf")

    uploaded = mock_client.upload_file("report.pdf", "/Docs/report.pdf")

    mock_client.upload_file.assert_called_once_with("report.pdf", "/Docs/report.pdf")
    assert uploaded.path == "/Docs/report.pdf"


def test_client_create_folder() -> None:
    """``create_folder`` returns the metadata of the new folder."""
    mock_client = Mock(spec=Client)
    mock_client.create_folder.return_value = _mock_file("/Docs", is_folder=True)

    folder = mock_client.create_folder("/Docs")

    assert folder.is_folder is True


def test_client_copy() -> None:
    """``copy`` returns the metadata of the duplicate."""
    mock_client = Mock(spec=Client)
    mock_client.copy.return_value = _mock_file("/Docs/report (copy).pdf")

    duplicate = mock_client.copy("/Docs/report.pdf", "/Docs/report (copy).pdf")

    assert duplicate.name == "report (copy).pdf"


# ----- Read -----


def test_client_download_file() -> None:
    """``download_file`` returns the metadata of the downloaded file."""
    mock_client = Mock(spec=Client)
    mock_client.download_file.return_value = _mock_file("/Docs/report.pdf")

    downloaded = mock_client.download_file("/Docs/report.pdf", "report.pdf")

    mock_client.download_file.assert_called_once_with("/Docs/report.pdf", "report.pdf")
    assert downloaded.name == "report.pdf"


def test_client_get_metadata() -> None:
    """``get_metadata`` returns a File for the given path."""
    mock_client = Mock(spec=Client)
    mock_client.get_metadata.return_value = _mock_file("/Docs/report.pdf")

    metadata = mock_client.get_metadata("/Docs/report.pdf")

    assert metadata.path == "/Docs/report.pdf"


def test_client_list_folder() -> None:
    """``list_folder`` returns an iterator of File objects."""
    mock_client = Mock(spec=Client)
    mock_client.list_folder.return_value = iter([_mock_file("/a.txt"), _mock_file("/b.txt")])

    entries = mock_client.list_folder("")

    assert isinstance(entries, Iterator)
    assert [entry.name for entry in entries] == ["a.txt", "b.txt"]


def test_client_search() -> None:
    """``search`` returns an iterator of matching File objects."""
    mock_client = Mock(spec=Client)
    mock_client.search.return_value = iter([_mock_file("/Docs/report.pdf")])

    results = list(mock_client.search("report", max_results=5))

    mock_client.search.assert_called_once_with("report", max_results=5)
    assert results[0].name == "report.pdf"


# ----- Update -----


def test_client_rename() -> None:
    """``rename`` keeps the folder and changes only the name."""
    mock_client = Mock(spec=Client)
    mock_client.rename.return_value = _mock_file("/Docs/final.pdf")

    renamed = mock_client.rename("/Docs/report.pdf", "final.pdf")

    assert renamed.path == "/Docs/final.pdf"


def test_client_move() -> None:
    """``move`` returns the metadata at the new location."""
    mock_client = Mock(spec=Client)
    mock_client.move.return_value = _mock_file("/Archive/report.pdf")

    moved = mock_client.move("/Docs/report.pdf", "/Archive/report.pdf")

    assert moved.path == "/Archive/report.pdf"


# ----- Delete -----


def test_client_delete() -> None:
    """``delete`` returns True on success."""
    mock_client = Mock(spec=Client)
    mock_client.delete.return_value = True

    assert mock_client.delete("/Docs/report.pdf") is True
    mock_client.delete.assert_called_once_with("/Docs/report.pdf")


def test_package_exports() -> None:
    """The package exposes the public names consumers depend on."""
    assert set(cloud_storage_client_api.__all__) == {"Client", "File", "file", "get_client"}
