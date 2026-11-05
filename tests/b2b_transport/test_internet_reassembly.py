"""B2BNET-08: door fragments reassemble only from consecutive counters; B2BNET-02: the pairing file is owner-only.

[if] the relay drops or reorders a fragment of a door message [then] that message is discarded and the next whole message still arrives, [else stop].

- [if] fragments with a counter gap are spliced into one message [then] fail
- [if] a gap leaves the channel unable to take the next whole message [then] fail
- [if] the saved internet pairing file is readable by other accounts [then] fail
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.b2b_transport.door import DoorAnswer, DoorRequest
from apps.b2b_transport.internet import (
    FRAGMENT_LAST,
    FRAGMENT_MORE,
    DoorChannel,
    Hello,
    InternetPairing,
    save_internet_pairing,
)
from apps.b2b_transport.noise import NoiseError
from apps.shared.private_files import is_owner_only

PAIRING = InternetPairing(peer_device_id="peer", peer_label="Peer", token="t" * 43, room="r" * 43,
                          role="inviter", paired_at="2026-11-05T10:00:00+00:00")


def _serve(_request: DoorRequest) -> DoorAnswer:
    raise AssertionError("not served in this test")


def _channel() -> DoorChannel:
    return DoorChannel("http://127.0.0.1:9", PAIRING, _serve, Hello("me", "Me"), lambda _hello: None)


@pytest.mark.requirement("B2BNET-08")
def test_consecutive_fragments_reassemble() -> None:
    """[if] two fragments carry consecutive counters [then] they reassemble into one message, [else stop]."""
    channel = _channel()
    assert channel._reassemble(0, bytes([FRAGMENT_MORE]) + b"ab") is None
    assert channel._reassemble(1, bytes([FRAGMENT_LAST]) + b"cd") == b"abcd"


@pytest.mark.requirement("B2BNET-08")
def test_a_dropped_middle_fragment_discards_the_message_and_the_next_one_arrives() -> None:
    """[if] a middle fragment is missing [then] the message is discarded and the next whole message arrives, [else stop]."""
    channel = _channel()
    assert channel._reassemble(0, bytes([FRAGMENT_MORE]) + b"ab") is None
    with pytest.raises(NoiseError, match="counter gap"):
        channel._reassemble(2, bytes([FRAGMENT_LAST]) + b"ef")
    assert channel._reassemble(3, bytes([FRAGMENT_LAST]) + b"next") == b"next"


@pytest.mark.requirement("B2BNET-08")
def test_a_dropped_first_fragment_drops_the_rest_of_that_message() -> None:
    """[if] a first fragment is missing [then] the rest of that message is dropped and the next one arrives, [else stop]."""
    channel = _channel()
    assert channel._reassemble(0, bytes([FRAGMENT_LAST]) + b"one") == b"one"
    with pytest.raises(NoiseError, match="counter gap"):
        channel._reassemble(2, bytes([FRAGMENT_MORE]) + b"mid")
    with pytest.raises(NoiseError, match="counter gap"):
        channel._reassemble(3, bytes([FRAGMENT_LAST]) + b"end")
    assert channel._reassemble(4, bytes([FRAGMENT_LAST]) + b"two") == b"two"


@pytest.mark.requirement("B2BNET-02")
def test_the_saved_pairing_file_is_owner_only(tmp_path: Path) -> None:
    """[if] an internet pairing is saved [then] only this account can read the file, [else stop]."""
    path = tmp_path / "state" / "b2b-internet-pairing.json"
    save_internet_pairing(path, PAIRING)
    assert is_owner_only(path)
