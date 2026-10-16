"""Privacy-safe MCP projections; never mutate private client responses."""

import re
import html
from urllib.parse import unquote

_URL = re.compile(r"https?:(?:\\*/){2}[^\s<>\"']+", re.IGNORECASE)
_URL_KEYS = {"url", "uri", "downloadurl", "attachmenturl", "fileurl", "signedurl"}


def _redact_url(match):
    value = match.group(0)
    # Match the whole URL, including escaped or nested redirect URLs. Remove
    # unsigned/truncated Blob URLs too: text extraction may precede projection.
    decoded = value
    for _ in range(3):
        decoded = re.sub(r"\\+/", "/", unquote(html.unescape(decoded))).replace("\\u0026", "&")
    if ".blob.core.windows.net" in decoded.lower():
        return "[signed URL omitted]"

    return value


def sanitize(value, *, attachment=False):
    """Remove image fields, attachment URLs and embedded Azure SAS URLs."""
    if isinstance(value, str):
        return _URL.sub(_redact_url, value)
    if isinstance(value, list):
        return [sanitize(item, attachment=attachment) for item in value]
    if isinstance(value, dict):
        attachment = attachment or ("mimeType" in value and "name" in value)
        out = {}
        if "profilePicture" in value or "profilePictureDto" in value:
            out["hasProfilePicture"] = bool(value.get("profilePicture") or value.get("profilePictureDto"))
        for key, item in value.items():
            if key in {"profilePicture", "profilePictureDto", "hasProfilePicture"}:
                continue
            if attachment and (key.lower() in _URL_KEYS or "url" in key.lower()):
                continue
            out[sanitize(key)] = sanitize(item, attachment=attachment or "attachment" in key.lower())
        if "hasProfilePicture" in value and "hasProfilePicture" not in out:
            out["hasProfilePicture"] = bool(value["hasProfilePicture"])
        return out
    return value


def _sender(sender):
    if not isinstance(sender, dict):
        return None
    out = {"id": sender.get("id"), "name": sender.get("name") or " ".join(
        str(sender[key]) for key in ("firstName", "lastName") if sender.get(key)),
        "communicatorType": sender.get("communicatorType") or sender.get("type")}
    if "profilePicture" in sender or "profilePictureDto" in sender:
        out["hasProfilePicture"] = bool(sender.get("profilePicture") or sender.get("profilePictureDto"))
    return out


def thread_summary(thread):
    last = thread.get("lastMessage") or {}
    out = {key: thread[key] for key in (
        "threadUid", "title", "isDeleted", "isSeen", "hasAttachment", "numberOfRecipients",
        "organizationalUnit", "isReplyDisabled") if key in thread}
    out["lastMessage"] = {"snippet": sanitize(str(last.get("body") or ""))[:200]}
    for key in ("id", "timeCreated", "hasAttachments"):
        if key in last:
            out["lastMessage"][key] = last[key]
    if isinstance(last.get("sender"), dict):
        out["lastMessage"]["sender"] = _sender(last["sender"])
    if isinstance(thread.get("sender"), dict):
        out["threadStarter"] = _sender(thread["sender"])
    if "timeCreated" in last:
        out["timeCreated"] = last["timeCreated"]
    return out


def verified_child_context(client, thread_uid, child_id):
    """Verify roster identity and membership using only child-filtered lists."""
    if not isinstance(child_id, str) or not child_id.strip():
        raise ValueError("child_id må vere ein gyldig ID.")
    matches = [child for child in client.get_children() if isinstance(child, dict)
               and str(child.get("childId") or child.get("id") or "") == child_id]
    if len(matches) != 1:
        raise ValueError("Valt barn kunne ikkje verifiserast i barnelista.")
    page_id = None
    seen = set()
    for _ in range(100):
        page = client.get_message_threads_page(child_id, 50, True, page_id)
        rows = page.get("messageThreads") if isinstance(page, dict) else None
        returned = page.get("pageId") if isinstance(page, dict) else None
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValueError("Ugyldig trådliste ved kontroll av barnetilknyting.")
        if type(returned) is not int or returned < 0 or returned in seen or (
            page_id is not None and returned != page_id
        ):
            raise ValueError("Ugyldig paging ved kontroll av barnetilknyting.")
        seen.add(returned)
        if any(isinstance(row, dict) and row.get("threadUid") == thread_uid for row in rows):
            return {**matches[0], "childId": child_id}
        if len(rows) < 50:
            raise ValueError("Tråden er ikkje verifisert for valt barn.")
        page_id = returned + 1
    raise ValueError("Sidegrense nådd utan verifisert barnetilknyting.")
