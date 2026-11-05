"""B2BNET-03: the Noise NNpsk0 core against the published cacophony vector, plus the replay window.

[if] our NNpsk0 runs [then] it matches the cacophony vector byte for byte, a wrong PSK or room never completes, sessions get fresh direction keys, and replayed, tampered or too-old frames are refused while in-window reordering opens, [else stop].

- [if] our NNpsk0 disagrees with the cacophony vector on any handshake or transport byte [then] fail
- [if] a different PSK (wrong pairing) completes a handshake [then] fail
- [if] a replayed, too-old or tampered frame opens, or a forged counter moves the window [then] fail
- [if] two sessions on the same token share a direction key [then] fail
- [if] an in-order and a reordered stream within the window do not both open fully [then] fail
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from apps.b2b_transport.noise import (
    REPLAY_WINDOW_BITS,
    Initiator,
    NoiseError,
    ReplayWindow,
    Responder,
    Session,
    _nonce,
    derive_psk,
    prologue_for,
)

pytestmark = pytest.mark.requirement("B2BNET-03")

# Noise_NNpsk0_25519_ChaChaPoly_SHA256 from cacophony.txt (snow's copy of the cacophony vectors,
# https://github.com/mcginty/snow/blob/main/tests/vectors/cacophony.txt), fetched Mon 12 Oct 2026.
VECTOR: dict[str, Any] = {
    "prologue": "4a6f686e2047616c74",
    "psk": "54686973206973206d7920417573747269616e20706572737065637469766521",
    "init_ephemeral": "893e28b9dc6ca8d611ab664754b8ceb7bac5117349a4439a6b0569da977c464a",
    "resp_ephemeral": "bbdb4cdbd309f1a1f2e1456967fe288cadd6f712d65dc7b7793d5e63da6b375b",
    "handshake_hash": "f4d03dc34495c95729ea6de9e1b59004b59733102488b3e24bc441e0be208eaf",
    "messages": [
        ("4c756477696720766f6e204d69736573",
         "ca35def5ae56cec33dc2036731ab14896bc4c75dbb07a61f879f8e3afa4c794479b962b8aff8485742ac32f905ba45369e2465fb59e138a93d67a0d1266b6a54"),
        ("4d757272617920526f746862617264",
         "95ebc60d2b1fa672c1f46a8aa265ef51bfe38e7ccb39ec5be34069f144808843d6062704d5a9c422a8e834423f8c1feada7e8d0d910a1a2cd030fb584221e3"),
        ("462e20412e20486179656b", "e632c3763d7669067383433197a3baddf146e9e70ad4b4e9e59e0f"),
        ("4361726c204d656e676572", "64c6bee32ea91c8474bb4c21d7a700109ad45af77b29764ba5eb1e"),
        ("4a65616e2d426170746973746520536179", "e2fa0bed0603b62d3ccac2ecabbf3fe33f3e86514909b323361626266cb2471cc8"),
        ("457567656e2042f6686d20766f6e2042617765726b",
         "0c01dc9cec1fe4ddd692e8dd32188aa351088dc91183639a53b57aa4692b5ebdef8b8ca111"),
    ],
}


def _key(hex_text: str) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(bytes.fromhex(hex_text))


def test_matches_the_cacophony_vector_byte_for_byte() -> None:
    prologue, psk = bytes.fromhex(VECTOR["prologue"]), bytes.fromhex(VECTOR["psk"])
    initiator = Initiator(psk, prologue, _key(VECTOR["init_ephemeral"]))
    responder = Responder(psk, prologue, _key(VECTOR["resp_ephemeral"]))
    msgs = [(bytes.fromhex(p), bytes.fromhex(c)) for p, c in VECTOR["messages"]]

    m1 = initiator.write_message_1(msgs[0][0])
    assert m1 == msgs[0][1]
    assert responder.read_message_1(m1) == msgs[0][0]
    m2, resp_session = responder.write_message_2(msgs[1][0])
    assert m2 == msgs[1][1]
    init_session, payload = initiator.read_message_2(m2)
    assert payload == msgs[1][0]
    assert init_session.handshake_hash.hex() == resp_session.handshake_hash.hex() == VECTOR["handshake_hash"]

    # Transport: the vector uses Noise's implicit nonces (0, 1 per direction) and empty AD.
    sends = {0: (init_session.send_key, 0), 1: (resp_session.send_key, 0)}
    for index, (payload_bytes, cipher) in enumerate(msgs[2:]):
        key, n = sends[index % 2]
        assert ChaCha20Poly1305(key).encrypt(_nonce(n), payload_bytes, b"") == cipher, f"transport message {index + 3}"
        sends[index % 2] = (key, n + 1)


def _pair(token: str = "token-a", room: str = "room-1") -> tuple[Session, Session]:
    initiator, responder = Initiator(derive_psk(token), prologue_for(room)), Responder(derive_psk(token), prologue_for(room))
    responder.read_message_1(initiator.write_message_1())
    m2, resp = responder.write_message_2()
    init, _ = initiator.read_message_2(m2)
    return init, resp


def test_a_wrong_psk_or_room_never_completes_a_handshake() -> None:
    for wrong in (Responder(derive_psk("other-token"), prologue_for("room-1")),
                  Responder(derive_psk("token-a"), prologue_for("room-2"))):
        initiator = Initiator(derive_psk("token-a"), prologue_for("room-1"))
        with pytest.raises(NoiseError):
            wrong.read_message_1(initiator.write_message_1())


def test_sessions_on_the_same_token_get_fresh_direction_keys() -> None:
    first, second = _pair(), _pair()
    keys = {first[0].send_key, first[0].recv_key, second[0].send_key, second[0].recv_key}
    assert len(keys) == 4
    assert first[0].send_key == first[1].recv_key and first[0].recv_key == first[1].send_key


def test_replay_tamper_and_cross_session_frames_are_refused() -> None:
    init, resp = _pair()
    counter, sealed = init.seal(b"door message", b"\x03")
    assert resp.open(counter, sealed, b"\x03") == b"door message"
    with pytest.raises(NoiseError, match="replayed"):
        resp.open(counter, sealed, b"\x03")
    tampered = bytearray(init.seal(b"x", b"\x03")[1])
    tampered[0] ^= 1
    with pytest.raises(NoiseError, match="authenticate"):
        resp.open(1, bytes(tampered), b"\x03")
    # A forged frame must not move the window: counter 1 is still accepted afterwards.
    assert resp.window.would_accept(1)
    other_init, _other_resp = _pair()
    old_counter, old_frame = other_init.seal(b"earlier session", b"\x03")
    with pytest.raises(NoiseError):
        resp.open(old_counter + 50, old_frame, b"\x03")


def test_window_accepts_reordering_and_refuses_frames_older_than_it() -> None:
    window = ReplayWindow()
    for counter in (5, 3, 4, 0, 1, 2):
        assert window.would_accept(counter)
        window.mark(counter)
    assert not any(window.would_accept(c) for c in range(6))
    window.mark(REPLAY_WINDOW_BITS + 10)
    assert not window.would_accept(5), "older than the window must be refused, not re-accepted"
    assert window.would_accept(REPLAY_WINDOW_BITS + 9)
