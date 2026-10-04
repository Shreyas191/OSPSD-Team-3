"""Main module for demonstrating the cloud storage client."""

import logging

import cloud_storage_client_api
import dropbox_client_impl  # noqa: F401  (import registers the Dropbox implementation)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    """Initialize the client and list the root folder of the Dropbox account."""
    # get_client() returns a DropboxClient because dropbox_client_impl registered itself.
    client = cloud_storage_client_api.get_client()
    logger.info("Using %s", type(client).__name__)

    try:
        for entry in client.list_folder(""):
            kind = "folder" if entry.is_folder else f"{entry.size} bytes"
            logger.info("%s (%s)", entry.path, kind)
    except NotImplementedError:
        logger.info("list_folder is not implemented yet.")

    print("Demo complete.")  # noqa: T201


if __name__ == "__main__":
    main()
