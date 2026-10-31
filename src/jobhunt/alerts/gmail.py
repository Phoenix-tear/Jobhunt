"""Read-only Gmail access: OAuth desktop flow, token handling and a minimal REST client.

Safety properties (each covered by a test):
* the only scope ever requested or accepted is `gmail.readonly`;
* the client implements GET `profile`, `messages` (list) and `messages/{id}` (get) and nothing
  else: no modify, label, send, trash or delete call exists in this module;
* credentials and tokens live under `.secrets/` (gitignored) and are never logged.

Spring Boot analogy: GmailApiClient ~ a thin RestTemplate wrapper; TokenStore ~ an
OAuth2AuthorizedClientService backed by a file.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from jobhunt.errors import InvalidResponseError, JobHuntError
from jobhunt.http import HttpClient
from jobhunt.sources.base import load_json

logger = logging.getLogger(__name__)

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE_SIZE = 100


class MailAuthError(JobHuntError):
    """The mailbox cannot be read until `jobhunt gmail-auth` is run again."""


@dataclass(frozen=True)
class RawEmail:
    """The parts of one message the pipeline needs. Bodies are parsed, then dropped."""

    message_id: str
    sender: str            # bare address, lower-case
    received_at: datetime | None
    html: str
    text: str


class MailSource(Protocol):
    """What the email pipeline needs from a mailbox (the real client and test fakes both fit)."""

    def list_message_ids(self, query: str, limit: int) -> list[str]: ...

    def get_message(self, message_id: str) -> RawEmail: ...


class GmailApiClient:
    """GET-only Gmail REST client. `token_provider` returns a fresh bearer token."""

    def __init__(self, http: HttpClient, token_provider: Callable[[], str]) -> None:
        self._http = http
        self._token_provider = token_provider

    def profile_email(self) -> str:
        payload = self._get(f"{API_BASE}/profile")
        address = payload.get("emailAddress") if isinstance(payload, dict) else None
        if not isinstance(address, str):
            raise InvalidResponseError("gmail profile: no emailAddress")
        return address.lower()

    def list_message_ids(self, query: str, limit: int) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < limit:
            params: dict[str, Any] = {"q": query, "maxResults": min(PAGE_SIZE, limit - len(ids))}
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(f"{API_BASE}/messages", params=params)
            if not isinstance(payload, dict):
                raise InvalidResponseError("gmail list: unexpected response")
            for item in payload.get("messages") or []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.append(item["id"])
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    def get_message(self, message_id: str) -> RawEmail:
        payload = self._get(f"{API_BASE}/messages/{message_id}", params={"format": "full"})
        if not isinstance(payload, dict):
            raise InvalidResponseError(f"gmail message {message_id}: unexpected response")
        return message_from_api(payload)

    def _get(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token_provider()}", "Accept": "application/json"}
        response = self._http.get(url, params=params, headers=headers)
        if response.status_code in (401, 403):
            raise MailAuthError(
                f"Gmail refused the request (HTTP {response.status_code}). Run `jobhunt gmail-auth` again; "
                "see SETUP.md (testing-mode tokens expire after 7 days)."
            )
        return load_json(response, what="gmail")


# -- MIME ---------------------------------------------------------------------------------------

def message_from_api(payload: Mapping[str, Any]) -> RawEmail:
    """Turn a Gmail `messages.get(format=full)` payload into a RawEmail."""
    top = payload.get("payload") or {}
    headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in top.get("headers") or []}
    sender = parseaddr(headers.get("from", ""))[1].lower()

    received: datetime | None = None
    if headers.get("date"):
        try:
            received = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError):
            received = None
    if received is None and str(payload.get("internalDate", "")).isdigit():
        from datetime import timezone

        received = datetime.fromtimestamp(int(payload["internalDate"]) / 1000, tz=timezone.utc)

    html_parts: list[str] = []
    text_parts: list[str] = []
    _walk(top, html_parts, text_parts)
    return RawEmail(
        message_id=str(payload.get("id", "")), sender=sender, received_at=received,
        html="\n".join(html_parts), text="\n".join(text_parts),
    )


def _walk(part: Mapping[str, Any], html_parts: list[str], text_parts: list[str]) -> None:
    mime_type = str(part.get("mimeType", "")).lower()
    body = part.get("body") or {}
    data = body.get("data")
    if data and mime_type in {"text/html", "text/plain"}:
        decoded = _decode_body(str(data), part)
        (html_parts if mime_type == "text/html" else text_parts).append(decoded)
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _walk(child, html_parts, text_parts)


def _decode_body(data: str, part: Mapping[str, Any]) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    for header in part.get("headers") or []:
        if str(header.get("name", "")).lower() == "content-type" and "charset=" in str(header.get("value", "")).lower():
            charset = str(header["value"]).lower().split("charset=")[1].split(";")[0].strip(" \"'")
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def message_from_eml(data: bytes, message_id: str = "local") -> RawEmail:
    """Parse a saved .eml file (offline helper behind `jobhunt parse-email`)."""
    message = BytesParser(policy=policy.default).parsebytes(data)
    received: datetime | None = None
    if message["date"]:
        try:
            received = parsedate_to_datetime(str(message["date"]))
        except (TypeError, ValueError):
            received = None
    html_part = message.get_body(preferencelist=("html",))
    text_part = message.get_body(preferencelist=("plain",))
    return RawEmail(
        message_id=message_id,
        sender=parseaddr(str(message["from"] or ""))[1].lower(),
        received_at=received,
        html=html_part.get_content() if html_part is not None else "",
        text=text_part.get_content() if text_part is not None else "",
    )


# -- auth ---------------------------------------------------------------------------------------

class TokenStore:
    """Loads `token.json`, refreshes it when expired and writes it back. Read-only scope only."""

    def __init__(self, credentials_file: Path, token_file: Path) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file

    def bearer_token(self) -> str:
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as error:
            raise MailAuthError("Gmail support is not installed: pip install -e \".[gmail]\"") from error

        if not self.token_file.is_file():
            raise MailAuthError(f"no token at {self.token_file}; run `jobhunt gmail-auth` first (see SETUP.md)")
        try:
            stored = json.loads(self.token_file.read_text(encoding="utf-8"))
        except ValueError as error:
            raise MailAuthError(f"{self.token_file} is not valid JSON; run `jobhunt gmail-auth` again") from error
        granted = set(stored.get("scopes") or []) if isinstance(stored, dict) else set()
        if granted != {READONLY_SCOPE}:  # checked on the file itself, before google-auth can override it
            raise MailAuthError(
                f"token scopes {sorted(granted) or 'missing'} are not exactly gmail.readonly; "
                "delete the token and run `jobhunt gmail-auth` again"
            )
        credentials = Credentials.from_authorized_user_file(str(self.token_file), [READONLY_SCOPE])
        if not credentials.valid:
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                raise MailAuthError(
                    "Google rejected the refresh token (expired or revoked). Run `jobhunt gmail-auth` again. "
                    "Apps left in 'Testing' publishing status get 7-day refresh tokens; see SETUP.md."
                ) from error
            self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        return str(credentials.token)


def run_auth_flow(credentials_file: Path, token_file: Path, *, port: int = 0, open_browser: bool = True) -> None:
    """Interactive desktop OAuth flow (opens a browser, listens on 127.0.0.1)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise MailAuthError("Gmail support is not installed: pip install -e \".[gmail]\"") from error
    if not credentials_file.is_file():
        raise MailAuthError(f"{credentials_file} not found; follow SETUP.md to download it from Google Cloud")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes=[READONLY_SCOPE])
    credentials = flow.run_local_server(port=port, open_browser=open_browser, access_type="offline", prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:  # Windows: ACLs, not POSIX modes
        pass


def sender_allowed(sender: str, allowed: tuple[str, ...]) -> bool:
    """Exact address match, or `@domain` matching the domain and its subdomains."""
    sender = sender.lower().strip()
    domain = sender.rpartition("@")[2]
    for entry in allowed:
        entry = entry.lower().strip()
        if entry.startswith("@"):
            suffix = entry[1:]
            if domain == suffix or domain.endswith("." + suffix):
                return True
        elif sender == entry:
            return True
    return False


__all__ = [
    "API_BASE", "GmailApiClient", "MailAuthError", "MailSource", "RawEmail", "READONLY_SCOPE", "TokenStore",
    "message_from_api", "message_from_eml", "run_auth_flow", "sender_allowed",
]
