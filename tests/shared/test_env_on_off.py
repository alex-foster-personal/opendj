"""Direct tests of apps.shared.env_on_off.read_on_off_environ."""

from __future__ import annotations

import pytest

from apps.shared.env_on_off import read_on_off_environ

_ENV = "MUSIC_DJ_TEST_ON_OFF"
_ALLOWED: tuple[str, ...] = ("on", "off")


def _read(environ: dict[str, str], *, normalize: bool = False) -> bool:
    return read_on_off_environ(
        environ,
        env_key=_ENV,
        allowed=_ALLOWED,
        default="on",
        normalize=normalize,
    )


def test_unset_defaults_to_on() -> None:
    """[if] the key is missing and default is on [then] True, [else stop]."""
    assert _read({}) is True


def test_on_and_off() -> None:
    """[if] the value is on or off [then] True or False, [else stop]."""
    assert _read({_ENV: "on"}) is True
    assert _read({_ENV: "off"}) is False


def test_junk_raises_naming_the_key() -> None:
    """[if] the value is junk [then] ValueError names the env key, [else stop]."""
    with pytest.raises(ValueError, match=_ENV):
        _read({_ENV: "maybe"})


def test_strict_rejects_padded_on() -> None:
    """[if] normalize is False and the value is padded [then] ValueError, [else stop]."""
    with pytest.raises(ValueError, match=_ENV):
        _read({_ENV: " ON "})


def test_normalize_folds_and_defaults() -> None:
    """[if] normalize is True [then] padded on arms, off disarms, unset arms, junk names the key, [else stop]."""
    assert _read({_ENV: " ON "}, normalize=True) is True
    assert _read({_ENV: "off"}, normalize=True) is False
    assert _read({}, normalize=True) is True
    with pytest.raises(ValueError, match=_ENV):
        _read({_ENV: "maybe"}, normalize=True)
