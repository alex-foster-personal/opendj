"""Ed25519 signatures over catalog bytes.

ONE KEY TYPE, ONE PLACE. The store index is signed by an Open DJ store key;
the app trusts only the public keys in :data:`TRUSTED_STORE_KEYS`, compiled
in like the updater's minisign key is compiled into the shell. Nothing read
from disk, the network or the environment can add a trusted key, so a
compromised index host cannot sign its own catalog (PLUG-6522b).

WHY THE TABLE IS EMPTY. No store key has been generated yet: the private half
belongs in the secret store, created by the owner with
``python -m scripts.plugin_store keygen`` on a trusted machine, and only its
public half lands here. Until then every catalog read reports the named fault
``no-trusted-key`` rather than pretending a catalog exists, and modules can be
installed only by developer sideload (``MDT_PLUGIN_DEV=1``).

The signature file next to ``<channel>.json`` is ``<channel>.json.sig``::

    {"key_id": "odj-store-2026", "signature": "<base64 of 64 raw bytes>"}

It signs the index file's exact bytes, so a re-serialised index fails.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: key_id -> base64 of the 32-byte raw Ed25519 public key. See module docstring.
TRUSTED_STORE_KEYS: Mapping[str, str] = {}

NO_TRUSTED_KEY_MESSAGE: str = (
    "this build trusts no plugin store signing key yet, so no catalog can be verified; "
    "modules install only by developer sideload until one is added (apps/plugins/signing.py)"
)


class SignatureError(ValueError):
    """The bytes are not signed by a trusted key. ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SignatureFile:
    key_id: str
    signature: bytes


def parse_signature_file(data: bytes) -> SignatureFile:
    try:
        raw = json.loads(data.decode("utf-8"))
        key_id = raw["key_id"]
        signature = base64.b64decode(raw["signature"], validate=True)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, binascii.Error) as exc:
        raise SignatureError("bad-signature-file", f"signature file is malformed: {exc}") from exc
    if not isinstance(key_id, str) or len(signature) != 64:
        raise SignatureError("bad-signature-file", "signature file needs a key_id and a 64-byte signature")
    return SignatureFile(key_id=key_id, signature=signature)


def verify(
    payload: bytes,
    signature_file: bytes,
    *,
    trusted: Mapping[str, str] | None = None,
) -> str:
    """Return the signing key id, or raise :class:`SignatureError`.

    ``trusted`` exists for tests, which sign with a throwaway key; production
    callers pass nothing and get :data:`TRUSTED_STORE_KEYS`.
    """
    keys = TRUSTED_STORE_KEYS if trusted is None else trusted
    if not keys:
        raise SignatureError("no-trusted-key", NO_TRUSTED_KEY_MESSAGE)
    sig = parse_signature_file(signature_file)
    encoded = keys.get(sig.key_id)
    if encoded is None:
        raise SignatureError("untrusted-key", f"signed by unknown key {sig.key_id!r}")
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded))
    try:
        public.verify(sig.signature, payload)
    except InvalidSignature as exc:
        raise SignatureError("bad-signature", f"signature by {sig.key_id!r} does not match") from exc
    return sig.key_id


def sign(payload: bytes, private_key: Ed25519PrivateKey, key_id: str) -> bytes:
    """Produce signature-file bytes. Used by ``scripts/plugin_store.py`` and tests."""
    signature = private_key.sign(payload)
    body = {"key_id": key_id, "signature": base64.b64encode(signature).decode("ascii")}
    return (json.dumps(body, indent=2) + "\n").encode("utf-8")


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")
