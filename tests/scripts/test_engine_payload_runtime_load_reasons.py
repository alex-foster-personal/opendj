"""Runtime-load allowlist reasons must name the platform gate that makes them safe.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if a Windows runtime load is allowlisted with a reason that never says
  Windows/win32, a generic "unreachable on macOS" can hide a load that became
  portable -> broken.
- if a Linux runtime load is allowlisted without a Linux rationale, the same
  generic reason hides a load that a darwin code path now reaches -> broken.
- if a CUDA runtime load is allowlisted without a CUDA rationale, a GPU load
  that became reachable ships under someone else's explanation -> broken.

Every check reads the reason the classifier actually returns for the site
(``classify_runtime_load_sites``), not the raw table, so a file-suffix reason
that shadows an allowlist entry is held to the same rule (spec
``(private path)``, "Classify the 22 sites").
"""

from __future__ import annotations

import pytest

from scripts.build_engine_payload import (
    LINE_KEY_PATTERN,
    RUNTIME_LOAD_ALLOWLIST,
    RuntimeLoadSite,
    classify_runtime_load_sites,
)

# The words that count as naming each gate, matched case-insensitively.
GATE_WORDS: dict[str, tuple[str, ...]] = {
    "windows": ("windows", "win32"),
    "linux": ("linux",),
    "cuda": ("cuda",),
}

# Line-keyed pylib sites (scripts/payload_runtime_allowlist.py) whose only
# safety is a platform or GPU gate. Re-read these when torch or filelock moves.
LINE_KEY_GATES: dict[str, tuple[str, ...]] = {
    "pylib/torch/_inductor/cpp_builder.py:1430": ("windows",),
    "pylib/torch/_inductor/cpp_builder.py:1446": ("windows",),
    "pylib/torch/distributed/elastic/multiprocessing/redirects.py:40": ("windows",),
    "pylib/torch/cuda/_utils.py:33": ("windows", "cuda"),
    "pylib/torch/cuda/_utils.py:113": ("windows", "cuda"),
    "pylib/torch/__init__.py:327": ("linux", "cuda"),
    "pylib/torch/_inductor/codecache.py:4748": ("linux",),
    "pylib/filelock/_util.py:312": ("linux",),
    "pylib/torch/cuda/__init__.py:93": ("cuda",),
    "pylib/torch/cuda/__init__.py:99": ("cuda",),
    "pylib/torch/cuda/__init__.py:102": ("cuda",),
    "pylib/torch/cuda/_utils.py:30": ("cuda",),
    "pylib/torch/cuda/_utils.py:107": ("cuda",),
    "pylib/torch/cuda/_utils.py:144": ("cuda",),
    "pylib/torch/cuda/memory.py:1246": ("cuda",),
    "pylib/torch/profiler/_cupti_monitor.py:293": ("cuda",),
    "pylib/torch/profiler/_cupti_monitor.py:1101": ("cuda",),
    "pylib/torch/xpu/memory.py:513": ("cuda",),
}

# Path fragments whose every line-keyed load is a GPU backend load.
GPU_PATH_FRAGMENTS: tuple[str, ...] = ("torch/cuda/", "torch/xpu/", "torch/profiler/_cupti")


def _site_for(key: str) -> RuntimeLoadSite:
    """Build the RuntimeLoadSite an allowlist key would classify."""
    match = LINE_KEY_PATTERN.match(key)
    if match is not None:
        return RuntimeLoadSite(
            path=match.group("path"),
            line=int(match.group("line")),
            call="CDLL",
            library=None,
            source="ctypes.CDLL(path)",
        )
    return RuntimeLoadSite(path="pylib/example/loader.py", line=1, call="CDLL", library=key, source=f'CDLL("{key}")')


def _classified_reason(key: str) -> str:
    """The reason the classifier records for the site an allowlist key names."""
    site = _site_for(key)
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == [], f"{key} is not classified at all"
    return classified[site.describe()]


def _cites(reason: str, gate: str) -> bool:
    """Whether a reason names the given platform or GPU gate."""
    lowered = reason.lower()
    return any(word in lowered for word in GATE_WORDS[gate])


def _required_gates(key: str) -> tuple[str, ...]:
    """The gates a literal library name or GPU-tree site must cite, by its shape alone."""
    if LINE_KEY_PATTERN.match(key) is not None:
        return ("cuda",) if any(part in key for part in GPU_PATH_FRAGMENTS) else ()
    name = key.lower()
    if name.endswith(".dll") or name.startswith("kernel32"):
        return ("windows",)
    if any(part in name for part in ("nvidia", "cuda", "nvrtc", "cupti")):
        return ("cuda",)
    return ()


def _is_shared_object(key: str) -> bool:
    """Whether a literal library name is an ELF soname (``foo.so`` or ``foo.so.N``)."""
    if LINE_KEY_PATTERN.match(key) is not None:
        return False
    name = key.lower()
    return name.endswith(".so") or ".so." in name


def _missing_rationale(allowlist: dict[str, str]) -> list[str]:
    """Allowlist keys whose reason omits a gate their name or path implies."""
    missing: list[str] = []
    for key, reason in allowlist.items():
        if any(not _cites(reason, gate) for gate in _required_gates(key)):
            missing.append(key)
        elif _is_shared_object(key) and not (_cites(reason, "linux") or _cites(reason, "cuda")):
            missing.append(key)
    return missing


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize("key,gates", sorted(LINE_KEY_GATES.items()))
def test_a_gated_line_site_reason_names_its_gate(key: str, gates: tuple[str, ...]) -> None:
    """[if] a gated dynamic site's reason omits its platform/CUDA gate [then] fail, [else stop]."""
    assert key in RUNTIME_LOAD_ALLOWLIST, f"{key} moved; re-read the site and re-pin it here"
    reason = _classified_reason(key)
    for gate in gates:
        assert _cites(reason, gate), f"{key} reason does not name the {gate} gate: {reason}"


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize("key", sorted(k for k in RUNTIME_LOAD_ALLOWLIST if _required_gates(k)))
def test_a_platform_named_entry_reason_names_its_platform(key: str) -> None:
    """[if] a .dll, CUDA or GPU-tree entry's classified reason omits its gate [then] fail, [else stop]."""
    reason = _classified_reason(key)
    assert _missing_rationale({key: reason}) == [], f"{key}: {reason}"


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize("key", sorted(k for k in RUNTIME_LOAD_ALLOWLIST if _is_shared_object(k)))
def test_a_linux_soname_entry_reason_names_linux_or_cuda(key: str) -> None:
    """[if] a .so entry's classified reason names neither Linux nor CUDA [then] fail, [else stop]."""
    reason = _classified_reason(key)
    assert _cites(reason, "linux") or _cites(reason, "cuda"), f"{key}: {reason}"


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize(
    "key",
    [
        "newlib.dll",
        "libnew.so.2",
        "libcuda_future",
        "pylib/torch/cuda/_new_probe.py:7",
    ],
)
def test_a_generic_reason_cannot_classify_a_platform_load(key: str) -> None:
    """[if] a Windows/Linux/CUDA load carries a generic reason [then] report it, [else stop]."""
    generic = {key: "unreachable in the shipped macOS bundle; never called."}
    assert _missing_rationale(generic) == [key]


@pytest.mark.requirement("INSTALL-11")
def test_the_real_allowlist_has_no_missing_rationale() -> None:
    """[if] any merged allowlist entry hides its platform gate [then] name it, [else stop]."""
    assert _missing_rationale(dict(RUNTIME_LOAD_ALLOWLIST)) == []
