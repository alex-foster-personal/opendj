"""Fixtures shared by the split test_ci_plan_helpers suite."""

from __future__ import annotations

from pathlib import Path

from scripts.ci_plan import Change, Config, Scope, Verdict


def _config(**overrides) -> Config:
    base = {
        "full_triggers": ("pyproject.toml", "**/conftest.py"),
        "always": ("tests/*.py",),
        "scopes": (
            Scope("engine", ("apps/engine_core/",), ("tests/engine_core/",)),
            Scope("library", ("apps/library/",), ("tests/library/",)),
        ),
    }
    return Config(**{**base, **overrides})


def _changed(*paths: str) -> tuple[Change, ...]:
    return tuple(Change("M", path) for path in paths)


def _support_tree(tmp_path: Path) -> tuple[Config, Path]:
    """A tests tree where a SUPPORT module is the only carrier of a cross-scope import."""
    root = tmp_path / "tests"
    (root / "b").mkdir(parents=True)
    # The collected module names only its own helper; the helper is what reaches scope a.
    (root / "b" / "test_thing.py").write_text("from tests.b.rig import fixture\n")
    (root / "b" / "rig.py").write_text("from apps.a import thing\n")
    config = Config(
        (),
        (),
        (
            Scope("a", ("apps/a/",), ("tests/a/",)),
            Scope("b", ("apps/b/",), ("tests/b/",)),
        ),
    )
    return config, root


def _nested_config() -> Config:
    return Config(
        (),
        ("tests/*.py",),
        (
            Scope("webui", ("apps/webui/",), ("tests/webui/",)),
            Scope("front", ("apps/webui/frontend/",), (), mentions=("apps/webui/frontend",)),
            Scope("a", ("apps/a/",), ("tests/a/",)),
        ),
        helper_carried=("webui",),
    )


def _mention_tree(tmp_path: Path, test_text: str) -> tuple[Config, Path]:
    root = tmp_path / "tests"
    (root / "a").mkdir(parents=True)
    (root / "a" / "test_build.py").write_text(test_text)
    return _nested_config(), root


def _root_helper_tree(tmp_path: Path, consumer: str) -> tuple[Config, Path]:
    """A root helper importing scope `engine`, consumed by ONE module at `consumer`."""
    root = tmp_path / "tests"
    (root / "library").mkdir(parents=True)
    (root / "rig.py").write_text("from apps.engine_core import thing\n")
    (root / consumer).parent.mkdir(parents=True, exist_ok=True)
    (root / consumer).write_text("from tests.rig import thing\n")
    return _config(), root
