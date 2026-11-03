"""GENRE-09: genre provider keys resolve from the macOS Keychain when the engine's environment has none.

- [if] a key is only in the Keychain [then] missing_keys() no longer names it, [else stop].
- [if] the environment holds the key [then] it wins over the Keychain, [else stop].
- [if] neither holds a key [then] require_key raises ProviderUnavailable naming it, [else stop].
- [if] set_key is given an unknown name [then] it refuses before touching the Keychain, [else stop].
- [if] the config was never written [then] auto_infer is on, [else stop].
- [if] a track already has a library or file genre [then] it ranks above the inferred one, [else stop].

-Claude
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator

import pytest

from apps.genre_infer import findings, keystore, providers, service

needs_keychain = pytest.mark.skipif(not keystore.available(), reason="macOS Keychain only")


@pytest.fixture
def scratch_service(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"com.opendj.genre.test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv(keystore.KEYCHAIN_SERVICE_ENV, name)
    monkeypatch.delenv(providers.OPENROUTER_KEY_ENV, raising=False)
    monkeypatch.delenv(providers.SERPER_KEY_ENV, raising=False)
    yield name
    if not keystore.available():
        return
    for key in providers.KEY_NAMES:
        subprocess.run([keystore.SECURITY_BIN, "delete-generic-password", "-s", name, "-a", key], capture_output=True, check=False)


@needs_keychain
def test_a_key_only_in_the_keychain_counts(scratch_service: str) -> None:
    assert providers.missing_keys() == list(providers.KEY_NAMES), "if no key anywhere then both are missing"
    service.set_key(providers.SERPER_KEY_ENV, "serper-from-keychain")
    assert providers.missing_keys() == [providers.OPENROUTER_KEY_ENV], "if Keychain has serper then it is not missing"
    assert providers.require_key(providers.SERPER_KEY_ENV) == "serper-from-keychain"


@needs_keychain
def test_environment_wins_over_keychain(scratch_service: str, monkeypatch: pytest.MonkeyPatch) -> None:
    keystore.write(providers.OPENROUTER_KEY_ENV, "from-keychain")
    monkeypatch.setenv(providers.OPENROUTER_KEY_ENV, "from-env")
    assert providers.require_key(providers.OPENROUTER_KEY_ENV) == "from-env"


def test_no_key_anywhere_is_an_error_naming_it(scratch_service: str) -> None:
    with pytest.raises(providers.ProviderUnavailable, match=providers.OPENROUTER_KEY_ENV):
        providers.require_key(providers.OPENROUTER_KEY_ENV)


def test_explicit_environ_never_reads_the_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keystore, "read", lambda _n: pytest.fail("Keychain read with an explicit environ"))
    assert providers.missing_keys({}) == list(providers.KEY_NAMES)


def test_set_key_refuses_an_unknown_name() -> None:
    with pytest.raises(findings.GenreInputError, match="unknown key"):
        service.set_key("AWS_SECRET", "x")


def test_auto_infer_defaults_on() -> None:
    assert findings.GenreConfig().auto_infer is True, "if default auto_infer is off then new tracks never get genres"


def test_existing_library_genre_outranks_inference() -> None:
    order = findings.DEFAULT_PRECEDENCE
    assert order.index(findings.PROVIDER_LIBRARY_TAG) < order.index(findings.PROVIDER_FUSED), (
        "if inference outranks the user's existing genre then their tags are hidden"
    )
