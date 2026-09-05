"""OAuth 2.0 authentication for Google Drive API.

Handles the initial browser-based consent flow, token caching to disk, and
automatic token refresh on subsequent runs.  The returned ``Credentials``
object is used by both the discovery-based Drive service and the raw
``AuthorizedSession`` used for chunked uploads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gdrivecopy.persistence import write_text_atomic

logger = logging.getLogger(__name__)

# Full drive scope is required because we list existing files (not just ones
# we created).  The narrower ``drive.file`` scope would hide files uploaded
# by other clients.
SCOPES = ["https://www.googleapis.com/auth/drive"]


def authenticate(
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
    *,
    select_account: bool = False,
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
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.debug("Loaded cached token from %s", token_path)
        except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
            # The token cache is replaceable.  Fall back to the consent flow
            # instead of making users diagnose or manually delete a bad file.
            logger.warning("Ignoring unreadable OAuth token cache %s: %s", token_path, exc)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired OAuth token")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Only revoked/expired grants call for fresh consent. Network or
            # OAuth-client configuration failures must remain visible errors.
            details = exc.args[1] if len(exc.args) > 1 else None
            if not isinstance(details, dict) or details.get("error") != "invalid_grant":
                raise
            logger.warning("Cached OAuth authorization expired or was revoked; requesting consent")
            creds = None
    if creds is None or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found at {credentials_path}. "
                "Download them from the Google Cloud Console "
                "(Google Auth Platform → Clients → Desktop app → Download JSON)."
            )
        logger.info("Starting OAuth consent flow (opening browser)")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid OAuth client JSON; download a Desktop app client from Google Cloud Console"
            ) from exc
        options = {"prompt": "select_account consent"} if select_account else {}
        creds = flow.run_local_server(port=0, **options)

    # Persist for next run without leaving a partially-written token behind.
    write_text_atomic(token_path, creds.to_json())
    logger.debug("Saved token to %s", token_path)
    return creds
