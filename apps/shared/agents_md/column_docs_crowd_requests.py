"""Column docs for the crowd request session and list (CROWDREQ-10749).

Owned by ``apps/webui/server/crowd_requests.py``, additive on state.db.
"""

from __future__ import annotations

CROWD_REQUEST_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "crowd_request_session": {
        "id": "Always 1: a single row holds the current crowd request session.",
        "open": "1 while the DJ is taking requests, 0 when the session is closed.",
        "catalog_playlist_id": "Playlist id the crowd may request from, or NULL when only free text is allowed.",
        "free_text_allowed": "1 when guests may type a short message instead of picking a catalog track.",
        "cooldown_s": "Seconds the public listener waits between submits from the same phone (default 180).",
        "opened_at": "When the session last went from closed to open, UTC; NULL if it has never opened.",
    },
    "crowd_requests": {
        "id": "Opaque request id (uuid hex) used by dismiss, loaded and vote routes.",
        "created_at": "When the request was posted, UTC.",
        "kind": "track when the guest picked a catalog id, text when they typed a message.",
        "stable_id": "Catalog track id for a track request; NULL for a text request.",
        "text": "Free-text message, at most 140 characters; NULL for a track request.",
        "requester_name": "Display name the guest gave, 1 to 24 characters.",
        "requester_key": "sha256 the public listener computed from a cookie plus source IP; never an address.",
        "votes": "How many times the store has recorded a vote for this request.",
        "status": "new, loaded or dismissed.",
        "status_at": "When status last changed, UTC (equals created_at while still new).",
    },
}
