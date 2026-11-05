"""B2BNET-05: the relay URL is explicit config with no hidden default.

[if] the relay URL is unset or not http(s), or an internet code lacks a nameplate in range [then] it is refused, unset as a 503 relay_unconfigured, [else stop].

- [if] an unset MDT_B2B_RELAY_URL resolves to any relay instead of a named 503 refusal [then] fail
- [if] a non-http(s) relay URL is accepted [then] fail
- [if] internet codes without a nameplate, or with a nameplate out of range, parse [then] fail
"""

from __future__ import annotations

import pytest

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_transport.internet import RELAY_URL_ENV, format_internet_code, parse_internet_code, relay_url

pytestmark = pytest.mark.requirement("B2BNET-05")


def test_relay_url_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RELAY_URL_ENV, raising=False)
    with pytest.raises(PairingError) as unset:
        relay_url()
    assert unset.value.code == "relay_unconfigured" and unset.value.status == 503
    monkeypatch.setenv(RELAY_URL_ENV, "ftp://example.invalid")
    with pytest.raises(PairingError):
        relay_url()
    monkeypatch.setenv(RELAY_URL_ENV, "http://127.0.0.1:1/")
    assert relay_url() == "http://127.0.0.1:1"


def test_internet_codes_carry_a_nameplate() -> None:
    assert parse_internet_code("12-k7q-4mx") == ("12", "K7Q4MX")
    assert parse_internet_code(format_internet_code("7", "K7Q-4MX")) == ("7", "K7Q4MX")
    for bad in ("K7Q-4MX", "0-K7Q-4MX", "10000-K7Q-4MX", "12-K7Q", "x-K7Q-4MX"):
        with pytest.raises(PairingError):
            parse_internet_code(bad)
