"""
Trilium ETAPI client — create notes in configurable sections.

Configure via environment variables:
  TRILIUM_ETAPI       — ETAPI base URL (default: disabled)
  TRILIUM_TOKEN_FILE  — path to file containing the ETAPI token
  TRILIUM_TOKEN       — token value directly (fallback if token file not set)
  TRILIUM_NOTE_IDS    — JSON mapping of section names to noteIds
                        e.g. '{"default": "YOUR_NOTE_ID"}'
                        If not set, all notes are created under the root.
"""
import json
import os
import httpx

ETAPI_BASE = os.getenv("TRILIUM_ETAPI", "")
TOKEN_FILE = os.getenv("TRILIUM_TOKEN_FILE", "")

# Note ID mapping: section name → Trilium noteId
# Configure via TRILIUM_NOTE_IDS env var as JSON, e.g.:
#   TRILIUM_NOTE_IDS='{"analyses": "YOUR_NOTE_ID", "notes": "ANOTHER_ID"}'
_NOTE_IDS_RAW = os.getenv("TRILIUM_NOTE_IDS", "{}")
try:
    NOTE_IDS: dict[str, str] = json.loads(_NOTE_IDS_RAW)
except (json.JSONDecodeError, ValueError):
    NOTE_IDS = {}

# Fallback root note ID (used when section not found in NOTE_IDS)
DEFAULT_NOTE_ID = os.getenv("TRILIUM_DEFAULT_NOTE_ID", "root")


def _token() -> str:
    if TOKEN_FILE:
        try:
            return open(TOKEN_FILE).read().strip()
        except FileNotFoundError:
            pass
    return os.getenv("TRILIUM_TOKEN", "")

def _headers():
    return {"Authorization": _token(), "Content-Type": "application/json"}

def _md_to_html(text: str) -> str:
    import re
    t = text
    t = re.sub(r'^### (.+)$', r'<h3>\1</h3>', t, flags=re.MULTILINE)
    t = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', t, flags=re.MULTILINE)
    t = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', t, flags=re.MULTILINE)
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'^\- (.+)$', r'<li>\1</li>', t, flags=re.MULTILINE)
    t = re.sub(r'\n', '<br>', t)
    return t

async def save_note(title: str, content: str, section: str = "default") -> str | None:
    """Create a note in Trilium. Returns noteId or None if Trilium is not configured or on error."""
    if not ETAPI_BASE:
        print("[trilium] TRILIUM_ETAPI not set — skipping note save (stdout sink):")
        print(f"  title: {title}")
        print(f"  content: {content[:200]}...")
        return None
    parent_id = NOTE_IDS.get(section, NOTE_IDS.get("default", DEFAULT_NOTE_ID))
    html = _md_to_html(content)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{ETAPI_BASE}/create-note",
                headers=_headers(),
                json={
                    "parentNoteId": parent_id,
                    "title": title,
                    "type": "text",
                    "content": html,
                },
            )
            r.raise_for_status()
            return r.json().get("note", {}).get("noteId")
    except Exception as e:
        print(f"[trilium] save error: {e}")
        return None


async def attach_file(note_id: str, data: bytes, mime: str, title: str) -> str | None:
    """Attach original file to a Trilium note. Returns attachmentId or None."""
    if not ETAPI_BASE or not note_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: create attachment metadata
            r = await client.post(
                f"{ETAPI_BASE}/attachments",
                headers=_headers(),
                json={
                    "ownerId": note_id,
                    "role": "attachment",
                    "mime": mime,
                    "title": title,
                    "position": 10,
                },
            )
            r.raise_for_status()
            att_id = r.json().get("attachmentId")
            if not att_id:
                return None
            # Step 2: upload file bytes
            r2 = await client.put(
                f"{ETAPI_BASE}/attachments/{att_id}/content",
                headers={"Authorization": _token(), "Content-Type": "application/octet-stream"},
                content=data,
            )
            r2.raise_for_status()
            return att_id
    except Exception as e:
        print(f"[trilium] attach error: {e}")
        return None


async def append_attachment_link(note_id: str, att_id: str, mime: str, title: str) -> None:
    """Append a reference/link to the attachment at the end of the note content."""
    if not ETAPI_BASE or not note_id or not att_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Get current note content
            r = await client.get(
                f"{ETAPI_BASE}/notes/{note_id}/content",
                headers={"Authorization": _token()},
            )
            r.raise_for_status()
            current_html = r.text

            # Build reference block
            if mime.startswith("image/"):
                ref = (
                    f'<hr>'
                    f'<p>Attachment: <img src="api/attachments/{att_id}/image/{title}" '
                    f'style="max-width:100%;border:1px solid #ccc;border-radius:4px"></p>'
                )
            elif mime == "application/pdf":
                ref = (
                    f'<hr>'
                    f'<p>Attachment PDF: <a href="api/attachments/{att_id}/download">{title}</a></p>'
                )
            else:
                ref = (
                    f'<hr>'
                    f'<p>Attachment: <a href="api/attachments/{att_id}/download">{title}</a> ({mime})</p>'
                )

            r2 = await client.put(
                f"{ETAPI_BASE}/notes/{note_id}/content",
                headers={"Authorization": _token(), "Content-Type": "text/plain"},
                content=(current_html + ref).encode(),
            )
            r2.raise_for_status()
    except Exception as e:
        print(f"[trilium] append_attachment error: {e}")
