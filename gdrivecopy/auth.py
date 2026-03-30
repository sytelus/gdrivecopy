"""OAuth 2.0 authentication for Google Drive API.

Handles the initial browser-based consent flow, token caching to disk, and
automatic token refresh on subsequent runs.  The returned ``Credentials``
object is used by both the discovery-based Drive service and the raw
``AuthorizedSession`` used for chunked uploads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Full drive scope is required because we list existing files (not just ones
# we created).  The narrower ``drive.file`` scope would hide files uploaded
# by other clients.
SCOPES = ["https://www.googleapis.com/auth/drive"]


def authenticate(
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
) -> Credentials:
    """Return valid Google OAuth 2.0 credentials.

    On the first run this opens a browser window for the consent flow and
    stores the resulting token at *token_path*.  On subsequent runs the
    cached token is loaded and refreshed if expired.

    Args:
        credentials_path: Path to the OAuth client-secret JSON downloaded
            from the Google Cloud Console.
        token_path: Path where the access/refresh token is cached.

    Returns:
        A ``Credentials`` instance ready for use with the Drive API.

    Raises:
        FileNotFoundError: If *credentials_path* does not exist and no
            cached token is available.
    """
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        logger.debug("Loaded cached token from %s", token_path)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired OAuth token")
        creds.refresh(Request())
    else:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found at {credentials_path}. "
                "Download them from the Google Cloud Console "
                "(APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON)."
            )
        logger.info("Starting OAuth consent flow (opening browser)")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)

    # Persist for next run.
    token_path.write_text(creds.to_json())
    logger.debug("Saved token to %s", token_path)
    return creds
