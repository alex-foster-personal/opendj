"""Stage the beatgrid backfill producer's own runtime into the engine payload.

NATIVE-10 (issue #2315): a v1 backfill runs on the user's machine with no
network after install, for every lane except stems and lyrics. Until this
module, an installed app could run waveform and loudness but not beatgrid
(and so not key, which reads beatgrid's own downbeats):
``apps/analysis/backends/own_beatgrid.py`` starts ``beat_this_runner.py``
either under ``uv`` (absent from a payload) or under the interpreter named by
``MDT_BEATGRID_RUNNER_PYTHON`` (never set by a payload), on a checkpoint
found through ``MDT_BEATGRID_WEIGHTS`` or the data dir (neither provisioned).

WHAT THIS STAGES, AND WHY EACH IS A SEPARATE PIECE

- ``runners/beatgrid/site``: the runner's dependency closure, exported from
  the runner script's OWN PEP 723 block through its committed lock
  (``beat_this_runner.py.lock``), never from pyproject. The runner pins its
  whole inference stack exactly (torch 2.14.0, torchaudio 2.11.0, numpy
  1.26.4, soundfile 0.14.0) because the record's producer version is only
  honest for ONE environment. The payload's ``pylib`` already carries torch
  2.5.1 for the vocal worker (ADR-NEW-packaged-vocal-runtime); running Beat
  This! on that would put a different environment's beats under the same
  producer version, and torchaudio 2.5.1's ``load`` can pick an ffmpeg
  backend where 2.11 always falls back to soundfile, so even the decoded
  samples could differ by machine. A second site is the price; see
  ADR-NEW-offline-beatgrid-runner-runtime.
- ``bin/opendj-beatgrid-python``: the interpreter ``MDT_BEATGRID_RUNNER_PYTHON``
  names. The payload's own CPython with ``PYTHONPATH`` replaced by the runner
  site ONLY, so nothing from ``app/`` or ``pylib/`` can shadow a pin.
- ``models/beatgrid/beat_this-final0.ckpt``: the checkpoint
  ``MDT_BEATGRID_WEIGHTS`` names. Fetched at BUILD time from Beat This!'s
  upstream URL into a content-addressed cache and refused unless its sha256
  is :data:`apps.analysis_beatgrid.weights.CHECKPOINT_SHA256`, the digest the
  producer asserts again at load. Beat This! code and weights are MIT
  (upstream README "License" section, CPJKU/beat_this b95c8ab, Thu 28 May
  2026; ADR-NEW-beat-this-weights-license).
- ``models/beatgrid/LICENSE-beat_this.txt``: the MIT notice for that
  checkpoint. MIT's one condition is that the copyright and permission notice
  travel with every copy, so the payload carries it next to the weights.

Requirements:

- ✔︎ ✅ 🎯 The runner closure is the runner script's own locked PEP 723
  closure, and every ``==`` pin in that block is installed at that version.
  -> :func:`runner_locked_export`, :func:`verify_bundled_beatgrid_runner`
- ✔︎ ✅ 🎯 The checkpoint is staged only when its digest matches the pinned
  one; a wrong download or a corrupt cache entry fails the build and caches
  nothing. -> :func:`fetch_checkpoint`
- ✔︎ ✅ 🎯 The checkpoint ships with its MIT notice; a payload without it
  fails verification. -> :func:`stage_checkpoint`,
  :func:`verify_bundled_beatgrid_runner`
- ✔︎ ✅ 🎯 The runner interpreter sees the runner site and nothing else.
  -> :func:`write_runner_launcher`

Acceptance tests (tests/scripts/test_engine_payload_beatgrid.py):

- [if] the checkpoint download has the wrong digest [then] the build fails
  and nothing is cached, [else ⛔️].
- [if] a staged payload lacks the checkpoint or the runner site [then]
  verification fails naming it, [else ⛔️].
- [if] the runner launcher runs with the engine's PYTHONPATH [then] the
  interpreter sees only the runner site, [else ⛔️].

-Claude
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from apps.analysis_beatgrid import weights

RUNNER_SCRIPT_RELATIVE = Path("apps/analysis_beatgrid/beat_this_runner.py")
RUNNER_SITE_RELATIVE = Path("runners/beatgrid/site")
RUNNER_LAUNCHER_RELATIVE = Path("bin/opendj-beatgrid-python")
CHECKPOINT_RELATIVE = Path("models/beatgrid") / weights.CHECKPOINT_FILENAME
#: Beat This!'s MIT notice, verbatim from the upstream LICENSE, which upstream
#: states covers the published weights too (ADR-NEW-beat-this-weights-license).
CHECKPOINT_LICENSE_SOURCE = (
    Path(__file__).resolve().parents[1] / "apps/analysis_beatgrid/beat_this_LICENSE.txt"
)
CHECKPOINT_LICENSE_RELATIVE = Path("models/beatgrid") / "LICENSE-beat_this.txt"
#: The copyright line the staged notice must carry; checked, not assumed.
CHECKPOINT_LICENSE_HOLDER = "Institute of Computational Perception, JKU Linz, Austria"

#: Beat This!'s own published location for ``final0`` (``beat_this.inference.
#: CHECKPOINT_URL`` + ``/final0.ckpt``). Read at build time only; the digest,
#: not the host, is what makes the staged file the right one.
CHECKPOINT_URL = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
CHECKPOINT_CACHE_DIR = Path.home() / ".cache" / "opendj-payload" / "beatgrid"
DOWNLOAD_TIMEOUT_S = 300

_PEP723_BLOCK = re.compile(r"^# /// script\n(?P<body>(?:#.*\n)+?)^# ///$", re.MULTILINE)
_EXACT_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;,]+)$")

RUNNER_LAUNCHER_TEMPLATE = f"""#!/bin/sh
# Interpreter for apps/analysis_beatgrid/beat_this_runner.py in an installed
# app (MDT_BEATGRID_RUNNER_PYTHON). The payload's own CPython, but PYTHONPATH
# is REPLACED, not extended: the runner imports its exact PEP 723 pins from
# {RUNNER_SITE_RELATIVE} and nothing from app/ or pylib/ may shadow one.
set -eu
here=$(cd -- "$(dirname -- "$0")" && pwd)
payload=$(cd -- "$here/.." && pwd)
PYTHONPATH="$payload/{RUNNER_SITE_RELATIVE}"
export PYTHONPATH
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
PYTHONSAFEPATH=1
export PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE PYTHONSAFEPATH
exec "$payload/runtime/bin/python3" "$@"
"""

#: Imported under the runner interpreter by :func:`verify_bundled_beatgrid_runner`.
#: ``apps`` must NOT resolve: that is the isolation claim, checked, not assumed.
_RUNNER_PROBE = """
import importlib.metadata as md, importlib.util, json
import beat_this.inference, torch, torchaudio, soundfile, numpy  # noqa: F401
names = json.loads(__import__('sys').argv[1])
print(json.dumps({
    'versions': {n: md.version(n) for n in names},
    'apps_visible': importlib.util.find_spec('apps') is not None,
}))
"""


_SITE = f"{RUNNER_SITE_RELATIVE}/"
_NO_GPU = ("the runner is always started with --device cpu (own_beatgrid.DEFAULT_DEVICE) "
           "on an arm64 Mac, which has no CUDA, ROCm or XPU stack to load")
#: Library-NAME keys are global: one entry classifies that name in EVERY torch
#: copy the payload carries (this site's 2.14.0 and pylib's vocals torch), so
#: its reason must hold for both, not only for the runner's --device cpu.
_NO_GPU_WHEEL = ("every torch copy in the payload is the arm64 macOS wheel, whose "
                 "torch.version.cuda and torch.version.hip are None, so no CUDA, ROCm "
                 "or XPU backend is ever selected")

#: Runtime library loads inside the runner site's torch 2.14.0 / filelock, merged
#: into build_engine_payload.RUNTIME_LOAD_ALLOWLIST. Dynamic sites are keyed by
#: payload path and LINE, so a torch bump moves them and the build fails until
#: each is re-read, the same contract the pylib vocals torch entries follow.
RUNNER_RUNTIME_LOAD_ALLOWLIST: dict[str, str] = {
    f"{_SITE}filelock/_identity.py:162": (
        "CDLL(None) is dlopen(NULL): binds sysctl symbols from the already-loaded "
        "libSystem inside filelock's darwin branch; no filesystem search."
    ),
    f"{_SITE}torch/__init__.py:348": (
        "_preload_cuda_deps loads a CUDA wheel soname found under sys.path; it "
        "raises unless platform.system() == 'Linux'."
    ),
    f"{_SITE}torch/__init__.py:424": (
        "loads libtorch_global_deps.dylib from torch/lib beside __file__: a "
        "payload-relative path, not a system search."
    ),
    f"{_SITE}torch/__init__.py:454": (
        "retry of libtorch_global_deps after the Linux-only CUDA preload in the "
        "OSError branch; same payload-relative path as line 424."
    ),
    f"{_SITE}torch/_inductor/codecache.py:3799": (
        "inductor loads a JIT-compiled library it just built; the runner never "
        "calls torch.compile, so no inductor code path runs."
    ),
    f"{_SITE}torch/_inductor/codecache.py:4926": (
        "inductor DLLWrapper opens a library it compiled; unreachable without "
        "torch.compile, which the runner never calls."
    ),
    f"{_SITE}torch/_inductor/codecache.py:4947": (
        "CDLL(None) in DLLWrapper's Linux dlclose path; unreachable without "
        "torch.compile and on darwin."
    ),
    "version.dll": "inductor's Windows-only compiler probe; darwin never reaches it.",
    f"{_SITE}torch/_inductor/cpp_builder.py:1498": (
        "Windows clang libomp preload in inductor's _IS_WINDOWS OpenMP setup; "
        "unreachable on darwin."
    ),
    f"{_SITE}torch/_inductor/cpp_builder.py:1514": (
        "Windows Intel icx libomp preload in the same _IS_WINDOWS branch; "
        "unreachable on darwin."
    ),
    "ze_loader": f"Intel Level Zero probe for inductor XPU builds; {_NO_GPU_WHEEL}.",
    f"{_SITE}torch/_ops.py:1587": (
        "torch.ops.load_library loads a caller-named extension; the runner and "
        "beat_this never call it."
    ),
    "libamd_smi.so": f"ROCm SMI hook inside torch.cuda device discovery; {_NO_GPU_WHEEL}.",
    f"{_SITE}torch/cuda/__init__.py:122": f"the same ROCm SMI hook's definition; {_NO_GPU}.",
    f"{_SITE}torch/cuda/__init__.py:128": f"the same ROCm SMI hook; {_NO_GPU}.",
    f"{_SITE}torch/cuda/__init__.py:131": f"the same ROCm SMI hook; {_NO_GPU}.",
    f"{_SITE}torch/cuda/_utils.py:32": f"ROCm HIP runtime for the CUDA driver API; {_NO_GPU}.",
    f"{_SITE}torch/cuda/_utils.py:35": f"Windows HIP runtime; {_NO_GPU}.",
    "libamdhip64.so": f"ROCm HIP runtime for the CUDA driver API; {_NO_GPU_WHEEL}.",
    "nvcuda.dll": f"Windows CUDA driver for the CUDA driver API; {_NO_GPU_WHEEL}.",
    "libcuda.so.1": f"Linux CUDA driver for the CUDA driver API; {_NO_GPU_WHEEL}.",
    f"{_SITE}torch/cuda/_utils.py:151": f"ROCm hiprtc for CUDA kernel JIT; {_NO_GPU}.",
    f"{_SITE}torch/cuda/_utils.py:157": f"Windows hiprtc for CUDA kernel JIT; {_NO_GPU}.",
    "libhiprtc.so": f"ROCm hiprtc for CUDA kernel JIT; {_NO_GPU_WHEEL}.",
    f"{_SITE}torch/cuda/_utils.py:188": f"NVRTC for CUDA kernel JIT; {_NO_GPU}.",
    f"{_SITE}torch/cuda/graphs.py:1031": f"CDLL(None).dladdr inside CUDA graph capture; {_NO_GPU}.",
    f"{_SITE}torch/cuda/memory.py:1335": (
        f"CUDAPluggableAllocator loads a caller-named allocator; {_NO_GPU}."
    ),
    f"{_SITE}torch/distributed/_token_switch.py:72": (
        "NCCL EP library for distributed collectives; the runner is one "
        f"process with no process group, and {_NO_GPU}."
    ),
    f"{_SITE}torch/distributed/elastic/multiprocessing/redirects.py:40": (
        "Windows C runtime for elastic launcher redirects; get_libc returns None "
        "on macOS before the loop, and the runner never uses torch.distributed."
    ),
    f"{_SITE}torch/profiler/_cupti/cupti_python.py:707": f"CUPTI for the CUDA profiler; {_NO_GPU}.",
    f"{_SITE}torch/xpu/memory.py:516": f"Intel XPU pluggable allocator; {_NO_GPU}.",
}


class PayloadBeatgridError(RuntimeError):
    """The payload cannot stage or prove the beatgrid runner runtime."""


#-----------------------------------------------------------------------------
# runner closure
#-----------------------------------------------------------------------------

def runner_pins(script: Path) -> dict[str, str]:
    """``{name: version}`` for every ``==`` dependency in the script's PEP 723 block."""
    match = _PEP723_BLOCK.search(script.read_text(encoding="utf-8"))
    if match is None:
        raise PayloadBeatgridError(f"{script} has no PEP 723 `# /// script` block")
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:]
                     for line in match.group("body").splitlines())
    pins: dict[str, str] = {}
    for requirement in tomllib.loads(body).get("dependencies", []):
        pinned = _EXACT_PIN.match(requirement.strip())
        if pinned is not None:
            pins[pinned.group("name").lower()] = pinned.group("version")
    if not pins:
        raise PayloadBeatgridError(f"{script} pins no dependency with ==")
    return pins


def runner_locked_export(repo_root: Path) -> str:
    """The runner's locked closure as requirements text.

    ``--locked`` makes the committed ``beat_this_runner.py.lock`` the input and
    fails when it no longer matches the script's block, the same "the lock is
    the input" rule the engine closure follows.
    """
    if shutil.which("uv") is None:
        raise PayloadBeatgridError("uv is not on PATH; cannot export the beatgrid runner deps")
    result = subprocess.run(
        ["uv", "export", "--script", str(repo_root / RUNNER_SCRIPT_RELATIVE),
         "--locked", "--no-hashes", "--format", "requirements-txt", "--quiet"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PayloadBeatgridError(
            "uv export --script failed for the beatgrid runner (a stale "
            f"{RUNNER_SCRIPT_RELATIVE}.lock is refreshed with `uv lock --script "
            f"{RUNNER_SCRIPT_RELATIVE}`):\n{result.stderr}"
        )
    return result.stdout


#-----------------------------------------------------------------------------
# checkpoint
#-----------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_checkpoint(
    cache_dir: Path,
    *,
    url: str = CHECKPOINT_URL,
    expected_sha256: str = weights.CHECKPOINT_SHA256,
) -> Path:
    """The verified checkpoint, from the content-addressed cache or downloaded into it.

    A cache entry is named by its expected digest and re-verified on every
    use: a truncated copy fails here, naming the file, rather than reaching a
    payload. A download is verified BEFORE it is moved into the cache, so a
    wrong file never lands where the next build would trust it.
    """
    cached = cache_dir / f"{expected_sha256}.ckpt"
    if cached.is_file():
        actual = _sha256(cached)
        if actual != expected_sha256:
            raise PayloadBeatgridError(
                f"cached checkpoint {cached} has sha256 {actual}, expected "
                f"{expected_sha256}; delete it and rebuild"
            )
        return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache_dir, suffix=".part", delete=False) as part:
        partial = Path(part.name)
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
                shutil.copyfileobj(response, part)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise PayloadBeatgridError(f"could not download {url}: {exc}") from exc
    actual = _sha256(partial)
    if actual != expected_sha256:
        partial.unlink()
        raise PayloadBeatgridError(
            f"checkpoint downloaded from {url} has sha256 {actual}, but the "
            f"beatgrid producer is pinned to {expected_sha256}; refusing to stage it"
        )
    partial.replace(cached)
    return cached


def stage_checkpoint(payload_dir: Path, cached: Path) -> Path:
    destination = payload_dir / CHECKPOINT_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, destination)
    shutil.copyfile(CHECKPOINT_LICENSE_SOURCE, payload_dir / CHECKPOINT_LICENSE_RELATIVE)
    return destination


#-----------------------------------------------------------------------------
# launcher
#-----------------------------------------------------------------------------

def write_runner_launcher(payload_dir: Path) -> Path:
    launcher = payload_dir / RUNNER_LAUNCHER_RELATIVE
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(RUNNER_LAUNCHER_TEMPLATE, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


#-----------------------------------------------------------------------------
# verification
#-----------------------------------------------------------------------------

def verify_bundled_beatgrid_runner(payload_dir: Path, repo_root: Path) -> dict[str, object]:
    """Re-derive every claim from the staged bytes; the manifest block on success."""
    site = payload_dir / RUNNER_SITE_RELATIVE
    if not site.is_dir():
        raise PayloadBeatgridError(f"beatgrid runner site missing at {site}")
    checkpoint = payload_dir / CHECKPOINT_RELATIVE
    if not checkpoint.is_file():
        raise PayloadBeatgridError(f"beatgrid checkpoint missing at {checkpoint}")
    digest = _sha256(checkpoint)
    if digest != weights.CHECKPOINT_SHA256:
        raise PayloadBeatgridError(
            f"staged checkpoint {checkpoint} has sha256 {digest}, expected "
            f"{weights.CHECKPOINT_SHA256}"
        )
    notice = payload_dir / CHECKPOINT_LICENSE_RELATIVE
    if not notice.is_file():
        raise PayloadBeatgridError(f"beatgrid checkpoint license notice missing at {notice}")
    notice_text = notice.read_text(encoding="utf-8")
    if not notice_text.startswith("MIT License") or CHECKPOINT_LICENSE_HOLDER not in notice_text:
        raise PayloadBeatgridError(
            f"beatgrid checkpoint license notice at {notice} is not Beat This!'s MIT notice"
        )
    pins = runner_pins(repo_root / RUNNER_SCRIPT_RELATIVE)
    with tempfile.TemporaryDirectory(prefix="opendj-beatgrid-verify-") as sandbox:
        result = subprocess.run(
            [str(payload_dir / RUNNER_LAUNCHER_RELATIVE), "-c", _RUNNER_PROBE,
             json.dumps(sorted(pins))],
            cwd=sandbox, capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": sandbox},
        )
    if result.returncode != 0:
        raise PayloadBeatgridError(
            f"the beatgrid runner interpreter cannot import its stack:\n{result.stderr}"
        )
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    if probe["apps_visible"]:
        raise PayloadBeatgridError("the beatgrid runner interpreter can import `apps`; "
                                   "its site is not isolated from the engine closure")
    wrong = {name: (probe["versions"][name], version)
             for name, version in pins.items() if probe["versions"][name] != version}
    if wrong:
        raise PayloadBeatgridError(f"runner site versions differ from the PEP 723 pins "
                                   f"(installed, pinned): {wrong}")
    return {
        "site": str(RUNNER_SITE_RELATIVE),
        "launcher": str(RUNNER_LAUNCHER_RELATIVE),
        "checkpoint": str(CHECKPOINT_RELATIVE),
        "checkpoint_sha256": digest,
        "checkpoint_license": str(CHECKPOINT_LICENSE_RELATIVE),
        "pins": probe["versions"],
        "bytes": {
            "site": sum(p.stat().st_size for p in site.rglob("*") if p.is_file()),
            "checkpoint": checkpoint.stat().st_size,
        },
    }


__all__ = [
    "CHECKPOINT_LICENSE_RELATIVE",
    "CHECKPOINT_RELATIVE",
    "RUNNER_LAUNCHER_RELATIVE",
    "RUNNER_RUNTIME_LOAD_ALLOWLIST",
    "RUNNER_SITE_RELATIVE",
    "PayloadBeatgridError",
    "fetch_checkpoint",
    "runner_locked_export",
    "runner_pins",
    "stage_checkpoint",
    "verify_bundled_beatgrid_runner",
    "write_runner_launcher",
]
