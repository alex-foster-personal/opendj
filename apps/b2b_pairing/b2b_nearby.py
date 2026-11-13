"""B2BNEAR-02..04: Open DJs nearby, Ask to B2B with Accept / Decline, and Reconnect.

ADR-NEW ((private path)). One per engine, beside the pairing
service (``b2b_service.PairingService``), whose LAN door and pairing record it reuses:

- **Listed nearby.** While running it advertises ``_opendj-b2b._tcp`` with TXT ``v``, ``id``
  (the per-boot device id), ``nv`` (this protocol), ``name`` (the DJ name, percent-encoded),
  ``acc`` (accepting requests, 1/0), ``pk`` (this laptop's X25519 identity public, urlsafe-b64)
  and, when paired, ``pt`` (a tag derived from the shared token, so only the remembered partner
  recognizes it). It browses the same type and lists every OTHER instance that carries ``nv``.
- **Ask to B2B.** The asker posts ``/b2b-nearby/request`` to the other door with an X25519
  public key. The other laptop holds it as an incoming request (60 s, rate-limited) until a
  person presses Accept or Decline. Accept generates the long-lived token, seals it under this
  laptop's advertised X25519 identity (ECDH with the asker's request key, HKDF-SHA256, AES-GCM,
  the request id as associated data) and posts it to the asker's door ``/b2b-nearby/answer``;
  the asker opens with the listing's ``pk``. Both then hold the same pairing record the invite
  code makes. Decline tells the asker only ``declined``. Nothing pairs without Accept.
- **Reconnect.** Two laptops that both still hold the same token prove it to each other by
  HMAC challenge-response (``/b2b-nearby/challenge`` then ``/b2b-nearby/reconnect``) and
  refresh each other's address. No prompt, no code; a laptop that forgot answers
  ``not_remembered``. A Reconnect press (or its CLI/MCP twin) runs it, and so does the peer
  watch (``b2b_readdress``, B2BREADDR-01) when the partner stops answering where it was. The
  proof names the door that was dialed, so a relayed one is refused (B2BREADDR-03).

Every refusal is a ``PairingError`` with a named ``code`` and the HTTP status to answer.
"""

from apps.b2b_pairing import b2b_mdns

from .b2b_nearby_protocol import (
    ANSWER,
    CANCEL,
    CHALLENGE,
    CHALLENGE_TTL_S,
    DOOR_TIMEOUT_S,
    LABEL_MAX,
    MAX_CHALLENGES,
    MAX_PENDING,
    NEARBY_VERSION,
    RATE_PER_SOURCE,
    RATE_WINDOW_S,
    RECONNECT,
    REQUEST,
    REQUEST_TTL_S,
    SCAN_DEADLINE_S,
    SCAN_MAX_AGE_S,
    SETTINGS_FILE,
    IncomingRequest,
    NearbyPeer,
    OutgoingRequest,
    OutgoingState,
    build_txt,
    open_token,
    pairing_tag,
    reconnect_proof,
    seal_token,
)
from .b2b_nearby_service import NearbyService

__all__ = [
    "ANSWER",
    "CANCEL",
    "CHALLENGE",
    "CHALLENGE_TTL_S",
    "DOOR_TIMEOUT_S",
    "IncomingRequest",
    "LABEL_MAX",
    "MAX_CHALLENGES",
    "MAX_PENDING",
    "NEARBY_VERSION",
    "NearbyPeer",
    "NearbyService",
    "OutgoingRequest",
    "OutgoingState",
    "RATE_PER_SOURCE",
    "RATE_WINDOW_S",
    "RECONNECT",
    "REQUEST",
    "REQUEST_TTL_S",
    "SCAN_DEADLINE_S",
    "SCAN_MAX_AGE_S",
    "SETTINGS_FILE",
    "b2b_mdns",
    "build_txt",
    "open_token",
    "pairing_tag",
    "reconnect_proof",
    "seal_token",
]
