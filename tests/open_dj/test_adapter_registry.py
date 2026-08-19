"""Every registry entry must match the family it declares (T3b S7).

``apps/open_dj/registry.py`` documents two adapter families in prose -- a
module family (rekordbox, djay) driven through ``module.export_library`` and a
class family (serato, traktor) implementing ``apps.open_dj.Adapter`` with
``read`` / ``write`` / ``capabilities``. The CLI branches on
``AdapterSpec.family`` and calls whichever shape the spec claims, so a spec
that lies about its target is only discovered when a user runs that adapter.

These tests make the split machine-checked, which is the prerequisite for the
phase-4 gate's "adapter families unified on the class Protocol": you cannot
prove a migration finished if nothing verifies which family each entry is in.

Regression one-liners:
  - if a registry entry does not satisfy the Protocol its family names then
    broken
  - if a module-family adapter loses export_library then broken
  - if a class-family adapter loses read/write/capabilities then broken
  - if a spec claims import_supported without a write entry point then broken
  - if an adapter's name disagrees with its registry key then broken
"""
from __future__ import annotations

import pytest

from apps.open_dj import Adapter
from apps.open_dj.adapters._base import OpenDjAdapter
from apps.open_dj.registry import ADAPTERS, available_adapters, load_adapter

pytestmark = pytest.mark.requirement("OPEN-02")

ADAPTER_NAMES = sorted(ADAPTERS)


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_entry_satisfies_the_protocol_its_family_names(name: str) -> None:
    """One assertion per entry, chosen by the family the spec declares.

    Both Protocols are runtime_checkable, so this is a structural check
    against the real loaded object -- the same object the CLI gets from
    ``load_adapter`` -- rather than a restatement of the registry table.
    """
    spec = ADAPTERS[name]
    loaded = load_adapter(name)
    if spec.family == "module":
        assert isinstance(loaded, OpenDjAdapter), (
            f"{name}: registry declares family 'module' but "
            f"{spec.target_path} does not satisfy OpenDjAdapter"
        )
    elif spec.family == "class":
        assert isinstance(loaded, Adapter), (
            f"{name}: registry declares family 'class' but "
            f"{spec.target_path} does not satisfy apps.open_dj.Adapter"
        )
    else:
        pytest.fail(f"{name}: unhandled adapter family {spec.family!r}")


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_adapter_name_matches_its_registry_key(name: str) -> None:
    """``--adapter <key>`` and the adapter's own ``name`` must agree.

    They feed different things -- the key selects the spec, ``name`` is what
    the adapter stamps into reports and provenance -- so a mismatch produces
    output attributed to the wrong vendor.
    """
    assert ADAPTERS[name].name == name
    assert getattr(load_adapter(name), "name", None) == name


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_import_supported_implies_a_write_entry_point(name: str) -> None:
    """A spec may not advertise a write path it cannot reach.

    The CLI refuses live writes when ``import_supported`` is False; the
    failure this pins is the other direction, where the flag is True and the
    call site has nothing to call.
    """
    spec = ADAPTERS[name]
    if not spec.import_supported:
        pytest.skip(f"{name}: import deferred by design (spec.import_supported False)")
    loaded = load_adapter(name)
    entry_point = "import_library" if spec.family == "module" else "write"
    assert callable(getattr(loaded, entry_point, None)), (
        f"{name}: import_supported=True but {spec.target_path} exposes no "
        f"callable {entry_point}"
    )


def test_every_registered_name_loads() -> None:
    """``available_adapters`` is what the CLI prints; each entry must resolve.

    Listing a name the loader cannot resolve turns a typo in ``target_path``
    into a runtime crash for whoever picks that adapter off the help text.
    """
    unresolved = []
    for name in available_adapters():
        try:
            load_adapter(name)
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            unresolved.append(f"{name}: {type(exc).__name__}: {exc}")
    assert unresolved == [], f"advertised but unloadable: {unresolved}"


def test_the_two_families_are_still_split() -> None:
    """Records the state the phase-4 gate wants changed, so it cannot drift.

    The gate calls for both families on the class Protocol. Until rekordbox
    and djay ship ``read``/``write`` -- blocked on a vendor write path that is
    deliberately deferred to the Phase 4 6-rail harness -- the split is real
    and this asserts exactly which side each vendor is on. When the migration
    lands, this test fails and is deleted, which is the point: the gate stops
    being a claim in a document.
    """
    by_family: dict[str, list[str]] = {}
    for name in ADAPTER_NAMES:
        by_family.setdefault(ADAPTERS[name].family, []).append(name)
    assert by_family == {
        "module": ["djay", "rekordbox"],
        "class": ["serato", "traktor"],
    }
