"""Config contracts for the two Modal bench runners (C2: 2ee770e4, 41049468).

Both scripts `import modal` at module scope, and modal is deliberately absent
from the repo venv (see pyproject's dev extra, which says so and why). So the
same technique tests/test_stem_tiers.py uses for the farm applies here: read
the module's declared configuration out of its source rather than importing
it. Nothing below runs a container, spends a GPU second, or needs Modal auth
-- it pins the knobs a run is launched with, which is where a silent A/B
comparison error would come from.

Regression lines:
  - if the A/B renderer's output dir stops being stems-demucs-ab then it can
    write into the real stems library or the roformer spike dir
  - if the A/B renderer's model/overlap/shifts drift from tiers.py Tier L
    then the comparison scores an htdemucs_ft config nothing else measured
  - if the instrumental stops being drums+bass+other then "instrumental" is
    not the complement of vocals
  - if a per-track failure stops being isolated then one bad file aborts a
    whole 10-track batch of GPU work
  - if a baked checkpoint disappears then the runner's own not-baked guard
    starts rejecting a checkpoint the ledger already has runs for
  - if the not-baked guard is removed then a cold per-container download can
    be launched at scale by accident
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from apps.stems import tiers as tiercfg

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMUCS_AB = REPO_ROOT / "scripts" / "modal_demucs_ab.py"
ROFORMER_SPIKE = REPO_ROOT / "scripts" / "modal_roformer_spike.py"
LEDGER = REPO_ROOT / "scripts" / "bench" / "roformer_ledger.json"


#: Module-level expression kinds this reader will evaluate. Deliberately
#: narrow: constants, the tuples/lists/dicts built from them, f-strings, and
#: references to constants declared earlier in the same module. Anything else
#: (a Path join, a modal.Image chain, a call) is skipped rather than run, so
#: reading the file can never execute the runner's Modal setup.
_SAFE_NODES = (
    ast.Constant, ast.Tuple, ast.List, ast.Dict, ast.Set,
    ast.Name, ast.JoinedStr, ast.FormattedValue, ast.Load,
    ast.BinOp, ast.Add, ast.Mult,
)


def _module_constants(path: Path) -> dict[str, object]:
    """Every module-level constant assignment, without importing the module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: dict[str, object] = {}
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else
            list(node.targets) if isinstance(node, ast.Assign) else []
        )
        value = getattr(node, "value", None)
        if value is None:
            continue
        if not all(isinstance(sub, _SAFE_NODES) for sub in ast.walk(value)):
            continue
        try:
            evaluated = eval(  # noqa: S307 -- gated to _SAFE_NODES above
                compile(ast.Expression(body=value), str(path), "eval"),
                {"__builtins__": {}},
                dict(out),
            )
        except Exception:  # noqa: BLE001 -- an unreadable constant is simply skipped
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = evaluated
    return out


def _function_def(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no function {name!r}")


@pytest.fixture(scope="module")
def ab() -> dict[str, object]:
    return _module_constants(DEMUCS_AB)


@pytest.fixture(scope="module")
def spike() -> dict[str, object]:
    return _module_constants(ROFORMER_SPIKE)


# ----- 41049468: htdemucs_ft A/B renderer -----------------------------------


def test_ab_renderer_writes_only_to_its_own_directory(ab) -> None:
    default_out = DEMUCS_AB.read_text()
    assert 'DEFAULT_OUT_DIR: Path = REPO_ROOT / "data" / "state" / "stems-demucs-ab"' in default_out
    assert '"stems-demucs-ab"' in default_out


def test_ab_renderer_never_targets_the_live_stems_library() -> None:
    """The comparison render is append-only into its own dir (house rule)."""
    writes = [
        line for line in DEMUCS_AB.read_text().splitlines()
        if "write_bytes(" in line or "write_text(" in line or "mkdir(" in line
    ]
    assert writes, "expected the renderer to write something"
    for line in writes:
        assert '"data"' not in line or "stems-demucs-ab" in line, line
    assert 'ROFORMER_SPIKE_DIR' not in "".join(writes)


def test_ab_renderer_config_matches_tiers_tier_l(ab) -> None:
    tier_l = tiercfg.TIERS["L"]
    assert ab["MODEL_NAME"] == tier_l.model
    assert ab["OVERLAP"] == tier_l.overlap
    assert ab["SHIFTS"] == tier_l.shifts
    assert ab["CONFIG_TAG"] == tier_l.preset_tag


def test_ab_renderer_runs_on_the_house_gpu(ab) -> None:
    assert ab["GPU_KIND"] == "H100"


def test_instrumental_is_the_complement_of_vocals(ab) -> None:
    assert ab["VOCALS_SOURCE"] == "vocals"
    assert ab["INSTRUMENTAL_SOURCES"] == ("drums", "bass", "other")
    assert ab["VOCALS_SOURCE"] not in ab["INSTRUMENTAL_SOURCES"]


def test_ab_renderer_encodes_16_bit_flac() -> None:
    assert 'subtype="PCM_16"' in DEMUCS_AB.read_text()
    assert 'format="FLAC"' in DEMUCS_AB.read_text()


def test_a_failed_track_is_isolated_not_raised() -> None:
    """One bad file must not abort ten containers' worth of GPU work."""
    fn = _function_def(DEMUCS_AB, "separate_track")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "separate_track must catch per-track failures"
    returns_in_handler = [
        n for handler in handlers for n in ast.walk(handler) if isinstance(n, ast.Return)
    ]
    assert returns_in_handler, "the handler must return an {error} payload, not re-raise"
    keys = {
        k.value
        for ret in returns_in_handler
        if isinstance(ret.value, ast.Dict)
        for k in ret.value.keys
        if isinstance(k, ast.Constant)
    }
    assert {"stable_id", "error"} <= keys, keys


# ----- 2ee770e4: checkpoint iteration ---------------------------------------


def test_default_checkpoint_is_itself_baked(spike) -> None:
    assert spike["DEFAULT_CHECKPOINT"] in spike["BAKED_CHECKPOINTS"]


def test_round1_viperx_alternates_are_baked(spike) -> None:
    for name in ("BS_ROFORMER_VIPERX_1296", "BS_ROFORMER_VIPERX_1297"):
        assert spike[name] in spike["BAKED_CHECKPOINTS"], name
    assert len(spike["BAKED_CHECKPOINTS"]) == 3


def test_every_baked_checkpoint_is_a_distinct_ckpt_file(spike) -> None:
    baked = spike["BAKED_CHECKPOINTS"]
    assert len(set(baked)) == len(baked)
    assert all(name.endswith(".ckpt") for name in baked), baked


def test_runner_refuses_a_checkpoint_that_is_not_baked() -> None:
    source = ROFORMER_SPIKE.read_text()
    assert "if checkpoint not in BAKED_CHECKPOINTS:" in source
    guard_body = source.split("if checkpoint not in BAKED_CHECKPOINTS:", 1)[1][:400]
    assert "raise SystemExit" in guard_body


def test_every_ledger_run_names_a_baked_checkpoint_family(spike) -> None:
    """Mirrors the runner's own checkpoint -> tag rule (modal_roformer_spike
    line ``checkpoint.split("_ep_")[0]...``): a ledger entry whose family is
    not baked any more is a run nobody can reproduce."""
    families = {
        name.split("_ep_")[0].replace("model_", "").replace("_", "-")
        for name in spike["BAKED_CHECKPOINTS"]
    }
    runs = json.loads(LEDGER.read_text())["runs"]
    assert runs
    for run in runs:
        tag = run["config_tag"]
        assert any(tag.startswith(family) for family in families), (tag, families)
