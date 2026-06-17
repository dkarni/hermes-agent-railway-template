# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
#   "mcp>=1.2.0",
# ]
# ///
import json
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

# === Auto-load env from profile env files (must run before os.environ reads) ===
from pathlib import Path as _Path
_PROFILE_ROOT = _Path(__file__).resolve().parents[1]
for _env_path in (_PROFILE_ROOT / ".env", _PROFILE_ROOT / "hermes.env"):
    if _env_path.exists():
        for _raw in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _raw.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))
# === End auto-load env ===


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["HUBSPOT_TOKEN"]
INBOX_ID = os.environ["HUBSPOT_INBOX_ID"]
CHANNEL_ACCOUNT_ID = os.environ["HUBSPOT_CHANNEL_ACCOUNT_ID"]
SENDER_ACTOR_ID = os.environ.get("HUBSPOT_SENDER_ACTOR_ID", "A-13186164")
CHANNEL_ID = os.environ.get("HUBSPOT_CHANNEL_ID", "1002")

DEFAULT_SENDER = os.environ.get("HUBSPOT_DEFAULT_SENDER", "daniel")
DEFAULT_FROM_EMAIL = os.environ.get("HUBSPOT_DEFAULT_FROM_EMAIL", "support@ethnicmusical.com")


def _field(config: dict, *names: str, default: str | None = None) -> str | None:
    for name in names:
        value = config.get(name)
        if value:
            return str(value)
    return default


def _normalise_sender(key: str, config: str | dict) -> dict:
    if isinstance(config, str):
        config = {"senderActorId": config}
    if not isinstance(config, dict):
        raise ValueError(f"HubSpot sender {key!r} must be a JSON object or actor id string")

    sender = {
        "key": key,
        "senderActorId": _field(config, "senderActorId", "sender_actor_id", "actorId", "actor_id"),
        "channelAccountId": _field(
            config,
            "channelAccountId",
            "channel_account_id",
            default=CHANNEL_ACCOUNT_ID,
        ),
        "inboxId": _field(config, "inboxId", "inbox_id", default=INBOX_ID),
        "fromEmail": _field(config, "fromEmail", "from_email", "email", default=DEFAULT_FROM_EMAIL),
        "firstName": _field(config, "firstName", "first_name", default=""),
        "lastName": _field(config, "lastName", "last_name", default=""),
    }
    if not sender["senderActorId"]:
        raise ValueError(f"HubSpot sender {key!r} is missing senderActorId")
    return sender


def _load_senders() -> dict[str, dict]:
    senders = {
        "daniel": {
            "key": "daniel",
            "senderActorId": SENDER_ACTOR_ID,
            "channelAccountId": CHANNEL_ACCOUNT_ID,
            "inboxId": INBOX_ID,
            "fromEmail": DEFAULT_FROM_EMAIL,
            "firstName": "Daniel",
            "lastName": "Karni",
        },
        "william": {
            "key": "william",
            "senderActorId": "A-90816114",
            "channelAccountId": CHANNEL_ACCOUNT_ID,
            "inboxId": INBOX_ID,
            "fromEmail": DEFAULT_FROM_EMAIL,
            "firstName": "William",
            "lastName": "",
        },
        "marco": {
            "key": "marco",
            "senderActorId": "A-90816084",
            "channelAccountId": CHANNEL_ACCOUNT_ID,
            "inboxId": INBOX_ID,
            "fromEmail": DEFAULT_FROM_EMAIL,
            "firstName": "Marco",
            "lastName": "",
        },
    }

    raw = os.environ.get("HUBSPOT_SENDERS", "").strip()
    if raw:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("HUBSPOT_SENDERS must be valid JSON") from exc
        if not isinstance(configured, dict):
            raise ValueError("HUBSPOT_SENDERS must be a JSON object keyed by sender alias")
        for key, config in configured.items():
            senders[str(key)] = _normalise_sender(str(key), config)

    senders["default"] = {**senders.get(DEFAULT_SENDER, senders["daniel"]), "key": "default"}
    return senders


SENDERS = _load_senders()


def _resolve_sender(from_sender: str | None = None) -> dict:
    requested = (from_sender or DEFAULT_SENDER or "daniel").strip()
    if not requested:
        requested = DEFAULT_SENDER or "daniel"

    lookup = {key.lower(): sender for key, sender in SENDERS.items()}
    for sender in SENDERS.values():
        from_email = sender.get("fromEmail")
        if from_email:
            lookup[from_email.lower()] = sender

    sender = lookup.get(requested.lower())
    if sender:
        return sender

    if requested.startswith("A-"):
        return {**SENDERS["default"], "key": requested, "senderActorId": requested}

    known = ", ".join(sorted(SENDERS))
    raise ValueError(f"Unknown HubSpot from_sender {requested!r}. Known senders: {known}")


def _message_payload(to_email: str, body: str, sender: dict, html: str = "") -> dict:
    payload = {
        "type": "MESSAGE",
        "text": body,
        "channelId": CHANNEL_ID,
        "channelAccountId": sender["channelAccountId"],
        "senderActorId": sender["senderActorId"],
        "recipients": [
            {
                "recipientField": "TO",
                "deliveryIdentifier": {"type": "HS_EMAIL_ADDRESS", "value": to_email},
            }
        ],
    }
    if html:
        payload["richText"] = html
    return payload


_client = httpx.AsyncClient(
    base_url="https://api.hubapi.com",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=20,
)

mcp = FastMCP("HubSpot Email Multi Sender")


@mcp.tool()
async def list_configured_senders() -> dict:
    """
    List HubSpot sender aliases available for the from_sender parameter.
    """
    return {
        "default": DEFAULT_SENDER,
        "senders": [
            {
                "key": sender["key"],
                "fromEmail": sender.get("fromEmail"),
                "name": f"{sender.get('firstName', '')} {sender.get('lastName', '')}".strip(),
                "senderActorId": sender.get("senderActorId"),
            }
            for key, sender in SENDERS.items()
            if key != "default"
        ],
    }


@mcp.tool()
async def send_email_reply(
    thread_id: str,
    to_email: str,
    body: str,
    from_sender: str = DEFAULT_SENDER,
    html: str = "",
) -> dict:
    """
    Send an outbound email reply to the customer in an existing HubSpot
    conversation thread. Auto-reopens the thread if closed.

    body: plain-text version (always required — HubSpot uses it as the
        text/plain MIME part and as fallback for clients that don't render HTML).
    html: optional HTML body. If provided, it is sent as the text/html MIME part
        (richText) and may contain real <a href>, <br>, <strong>, etc.
        Pass HTML here — never inside `body`, where tags would be displayed literally.
    from_sender: daniel, william, marco, a configured email/alias, or A-<HubSpot userId>.
    """
    sender = _resolve_sender(from_sender)

    await _client.patch(
        f"/conversations/v3/conversations/threads/{thread_id}",
        json={"status": "OPEN"},
    )
    r = await _client.post(
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        json=_message_payload(to_email, body, sender, html=html),
    )
    r.raise_for_status()
    logger.info(
        "Reply sent to thread %s to %s from %s (html=%s)",
        thread_id, to_email, sender["key"], bool(html),
    )
    return r.json()


@mcp.tool()
async def send_new_email(
    to_email: str,
    subject: str,
    body: str,
    from_sender: str = DEFAULT_SENDER,
    html: str = "",
) -> dict:
    """
    Send a brand-new outbound email to any address.
    Creates a new HubSpot conversation thread then sends the first message.

    body: plain-text version (required, always sent).
    html: optional HTML body for the text/html MIME part. Use this for clickable
        links — never embed raw HTML in `body`.
    from_sender: daniel, william, marco, a configured email/alias, or A-<HubSpot userId>.
    """
    sender = _resolve_sender(from_sender)

    tr = await _client.post(
        "/conversations/v3/conversations/threads",
        json={
            "type": "EMAIL",
            "subject": subject,
            "inboxId": sender["inboxId"],
            "channelAccountId": sender["channelAccountId"],
            "recipients": [{"actorId": f"EMAIL:{to_email}", "recipientField": "TO"}],
        },
    )
    tr.raise_for_status()
    thread_id = tr.json()["id"]

    mr = await _client.post(
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        json=_message_payload(to_email, body, sender, html=html),
    )
    mr.raise_for_status()
    logger.info(
        "New email sent to %s via thread %s from %s (html=%s)",
        to_email, thread_id, sender["key"], bool(html),
    )
    return {"thread_id": thread_id, **mr.json()}


def _extract_thread(thread: dict) -> dict:
    return {
        "thread_id": thread.get("id"),
        "subject": thread.get("subject", ""),
        "status": thread.get("status", ""),
        "latestMessageTimestamp": thread.get("latestMessageTimestamp", ""),
    }


@mcp.tool()
async def find_thread_by_email(contact_email: str) -> dict:
    """
    Find the HubSpot Conversations thread ID(s) for a customer by their email address.
    Returns thread_id values ready for use with send_email_reply.
    Results are sorted most-recent first; OPEN threads are listed before CLOSED ones.
    Use find_thread_by_ticket_id instead when you have a ticket ID — it is more reliable.
    """
    results = {"contact": None, "threads": []}

    crm_r = await _client.post(
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": contact_email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname"],
            "limit": 1,
        },
    )
    contact_id = None
    if crm_r.status_code == 200:
        contacts = crm_r.json().get("results", [])
        if contacts:
            contact_id = contacts[0]["id"]
            props = contacts[0].get("properties", {})
            results["contact"] = {
                "id": contact_id,
                "email": props.get("email"),
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
            }

    seen_ids: set[str] = set()

    if contact_id:
        t_r = await _client.get(
            "/conversations/v3/conversations/threads",
            params={"associatedContactId": contact_id, "limit": 25, "sort": "-latestMessageTimestamp"},
        )
        if t_r.status_code == 200:
            for thread in t_r.json().get("results", []):
                tid = thread.get("id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    results["threads"].append(_extract_thread(thread))

    if not results["threads"]:
        t_r2 = await _client.get(
            "/conversations/v3/conversations/threads",
            params={"email": contact_email, "limit": 25, "sort": "-latestMessageTimestamp"},
        )
        if t_r2.status_code == 200:
            for thread in t_r2.json().get("results", []):
                tid = thread.get("id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    results["threads"].append(_extract_thread(thread))

    # Sort: OPEN threads first, then by most recent
    results["threads"].sort(
        key=lambda t: (0 if t["status"] == "OPEN" else 1, t["latestMessageTimestamp"] or ""),
        reverse=False,
    )
    results["threads"].sort(key=lambda t: t["status"] != "OPEN")

    logger.info(
        "find_thread_by_email(%s) returned contact=%s threads=%d",
        contact_email,
        contact_id,
        len(results["threads"]),
    )
    return results


@mcp.tool()
async def find_thread_by_ticket_id(ticket_id: str) -> dict:
    """
    Find the HubSpot Conversations thread ID(s) associated with a specific ticket.
    More reliable than find_thread_by_email: works even when the ticket was created
    from a forwarded/no-reply address, or when the customer has many threads.
    Returns thread_id values ready for use with send_email_reply.
    Always prefer this tool when you have a ticket ID.
    """
    results = {"ticket_id": ticket_id, "threads": []}

    # Primary: conversations API associatedTicketId filter
    t_r = await _client.get(
        "/conversations/v3/conversations/threads",
        params={"associatedTicketId": ticket_id, "limit": 10, "sort": "-latestMessageTimestamp"},
    )
    seen_ids: set[str] = set()
    if t_r.status_code == 200:
        for thread in t_r.json().get("results", []):
            tid = thread.get("id")
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                results["threads"].append(_extract_thread(thread))

    # Fallback: CRM associations (ticket → conversation objects)
    if not results["threads"]:
        assoc_r = await _client.get(
            f"/crm/v4/objects/tickets/{ticket_id}/associations/conversations",
        )
        if assoc_r.status_code == 200:
            for assoc in assoc_r.json().get("results", []):
                conv_id = assoc.get("toObjectId")
                if not conv_id:
                    continue
                th_r = await _client.get(f"/conversations/v3/conversations/threads/{conv_id}")
                if th_r.status_code == 200:
                    thread = th_r.json()
                    tid = thread.get("id")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        results["threads"].append(_extract_thread(thread))

    logger.info(
        "find_thread_by_ticket_id(%s) returned threads=%d",
        ticket_id,
        len(results["threads"]),
    )
    return results


@mcp.tool()
async def get_thread_messages(thread_id: str, limit: int = 10) -> dict:
    """
    Fetch recent messages from a HubSpot Conversations thread.
    Useful for verifying you have the right thread and reading the conversation history
    before composing a reply.
    """
    r = await _client.get(
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        params={"limit": min(limit, 50)},
    )
    r.raise_for_status()
    data = r.json()
    messages = []
    for msg in data.get("results", []):
        sender = msg.get("senders", [{}])[0] if msg.get("senders") else {}
        messages.append({
            "id": msg.get("id"),
            "type": msg.get("type"),
            "direction": msg.get("direction"),
            "timestamp": msg.get("createdAt"),
            "from": sender.get("deliveryIdentifier", {}).get("value") or sender.get("actorId"),
            "text": msg.get("text", "")[:2000],  # truncate very long messages
        })
    logger.info("get_thread_messages(%s) returned %d messages", thread_id, len(messages))
    return {"thread_id": thread_id, "messages": messages}


@mcp.tool()
async def close_thread(thread_id: str) -> dict:
    """
    Close a HubSpot conversation thread.
    """
    r = await _client.patch(
        f"/conversations/v3/conversations/threads/{thread_id}",
        json={"status": "CLOSED"},
    )
    r.raise_for_status()
    logger.info("Thread %s closed", thread_id)
    return r.json()


@mcp.tool()
async def log_sent_email(
    to_email: str,
    subject: str,
    body: str,
    from_sender: str = DEFAULT_SENDER,
    from_email: str = "",
) -> dict:
    """
    Log an outbound email in HubSpot CRM as an email activity on the contact record.
    Use after Gmail fallback sends so the email is visible on the HubSpot timeline.

    from_sender: daniel, william, marco, a configured email/alias, or A-<HubSpot userId>.
    from_email: optional manual override for the logged From email.
    """
    sender = _resolve_sender(from_sender)

    contact_id = None
    crm_r = await _client.post(
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": to_email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        },
    )
    if crm_r.status_code == 200:
        contacts = crm_r.json().get("results", [])
        if contacts:
            contact_id = int(contacts[0]["id"])

    payload = {
        "engagement": {"active": True, "type": "EMAIL"},
        "associations": {
            "contactIds": [contact_id] if contact_id else [],
            "companyIds": [],
            "dealIds": [],
            "ownerIds": [],
            "ticketIds": [],
        },
        "metadata": {
            "from": {
                "email": from_email or sender["fromEmail"],
                "firstName": sender.get("firstName", ""),
                "lastName": sender.get("lastName", ""),
            },
            "to": [{"email": to_email}],
            "cc": [],
            "bcc": [],
            "subject": subject,
            "text": body,
        },
    }
    eng_r = await _client.post("/engagements/v1/engagements", json=payload)
    eng_r.raise_for_status()
    engagement_id = eng_r.json()["engagement"]["id"]
    logger.info("Logged email engagement %s for %s from %s", engagement_id, to_email, sender["key"])

    return {
        "email_engagement_id": str(engagement_id),
        "contact_id": str(contact_id) if contact_id else None,
        "associated": contact_id is not None,
    }


if __name__ == "__main__":
    mcp.run()
