"""Send a Telegram message through the Bot API, without the polling bot running.

Used by `sitt-health` for the daily summary and for alerts. The bot token is part of the
request URL, so nothing here ever logs or raises with a URL in it.
"""

from collections.abc import Iterable

import httpx

API_BASE = "https://api.telegram.org"
MESSAGE_LIMIT = 4096  # Telegram's limit for one message
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class NotifyError(Exception):
    """A message could not be sent. The text never contains the bot token."""


def clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 2].rstrip() + "\n…"


def send_message(client: httpx.Client, token: str, chat_id: int, text: str) -> None:
    """Send one plain-text message. Raises NotifyError on any failure."""
    try:
        response = client.post(
            f"{API_BASE}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": clip(text), "disable_web_page_preview": True},
        )
    except httpx.HTTPError as exc:
        # str(exc) can include the URL, and so the token: report the kind of error only.
        raise NotifyError(f"could not reach Telegram ({type(exc).__name__})") from None
    if response.status_code != 200:
        try:
            description = response.json().get("description", "")
        except ValueError:
            description = ""
        raise NotifyError(
            f"Telegram refused the message for chat {chat_id}: HTTP {response.status_code}"
            + (f" ({description})" if description else "")
        )


def send_to_users(
    token: str, user_ids: Iterable[int], text: str, client: httpx.Client | None = None
) -> int:
    """Send `text` to every user. Returns how many were sent; raises NotifyError if any failed.

    Every user is attempted before the error is raised.
    """
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    sent, errors = 0, []
    try:
        for user_id in sorted(user_ids):
            try:
                send_message(client, token, user_id, text)
                sent += 1
            except NotifyError as exc:
                errors.append(str(exc))
    finally:
        if own:
            client.close()
    if errors:
        raise NotifyError("; ".join(errors))
    return sent
