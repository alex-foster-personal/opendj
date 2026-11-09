"""scripts/gen_reserved_ports.py generates RESERVED_FIXED_PORTS from the suites (#8583).

[if] the committed RESERVED_FIXED_PORTS block differs from the generator output [then] the currency test fails naming the command, [else stop].
[if] the generator runs on its own output [then] nothing changes, [else stop].
[if] an EXTRA_PORTS entry is also declared by a suite file [then] generation raises naming it, [else stop].
[if] discovery yields Windows-style relative paths [then] provenance comments use POSIX slashes, [else stop].
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from apps.webui.port_config import RESERVED_FIXED_PORTS
from scripts import gen_reserved_ports as gen

pytestmark = pytest.mark.requirement("INFRA-08")

REPO = Path(__file__).resolve().parents[2]
ENTRY = re.compile(r"^        (\d+),  # (\S.*)$")


def _committed_source() -> str:
    return gen.PORT_CONFIG.read_text(encoding="utf-8")


def _block_entries(source: str) -> list[tuple[int, str]]:
    block = source[source.index(gen.BLOCK_START) + len(gen.BLOCK_START) : source.index(gen.BLOCK_END)]
    lines = block.splitlines()
    entries = [ENTRY.match(line) for line in lines]
    assert all(entries), [line for line, match in zip(lines, entries, strict=True) if match is None]
    return [(int(match.group(1)), match.group(2)) for match in entries if match is not None]


def _fake_suite_roots(tmp_path: Path, extra_config: str | None) -> tuple[tuple[Path, str], ...]:
    """The real suite roots plus one tmp root, optionally holding a new config."""
    fake = tmp_path / "e2e"
    fake.mkdir()
    if extra_config is not None:
        (fake / "vite.brand-new-gate.config.ts").write_text(extra_config, encoding="utf-8")
    return (*gen.SUITE_ROOTS, (fake, "tests/fake"))


def test_the_committed_block_is_current() -> None:
    """[if] a suite's pinned port changes without a regeneration [then] this fails naming the command, [else stop]."""
    source = _committed_source()
    assert gen.expected_source(source) == source, (
        f"RESERVED_FIXED_PORTS in apps/webui/port_config.py is stale; run `{gen.GENERATOR_COMMAND}`"
    )


def test_a_new_config_port_makes_the_block_stale_until_regenerated(tmp_path: Path) -> None:
    """[if] a fake config pins a new port [then] the committed block is stale and a regeneration adds it, [else stop]."""
    source = _committed_source()
    roots = _fake_suite_roots(tmp_path, "export const BRAND_NEW_GATE_PORT = 5398;\n")
    regenerated = gen.expected_source(source, roots)
    assert regenerated != source, "a new pinned port must make the committed block stale"
    assert (5398, "tests/fake/vite.brand-new-gate.config.ts") in _block_entries(regenerated)
    assert gen.expected_source(regenerated, roots) == regenerated, "after regeneration the block is current"


def test_an_empty_extra_root_changes_nothing(tmp_path: Path) -> None:
    """CONTROL: the stale case above is caused by the fake port, not by adding a root."""
    source = _committed_source()
    assert gen.expected_source(source, _fake_suite_roots(tmp_path, None)) == source


def test_the_generator_is_idempotent() -> None:
    source = _committed_source()
    once = gen.expected_source(source)
    assert gen.expected_source(once) == once


def test_entries_are_sorted_one_per_line_each_naming_its_file() -> None:
    entries = _block_entries(_committed_source())
    ports = [port for port, _ in entries]
    assert ports == sorted(ports) and len(ports) == len(set(ports))
    for port, comment in entries:
        named = comment.split(" ")[0]
        assert named.endswith(".ts") or comment.endswith("(extra)"), (port, comment)
        if not comment.endswith("(extra)"):
            prefix = dict((label, root) for root, label in gen.SUITE_ROOTS)
            label, rel = named.split("/", 2)[0:2], named.split("/", 2)[2]
            root = prefix["/".join(label)]
            assert str(port) in (root / rel).read_text(encoding="utf-8"), (port, named)


def test_the_generated_set_is_exactly_reserved_fixed_ports() -> None:
    assert {port for port, _ in _block_entries(_committed_source())} == set(RESERVED_FIXED_PORTS)
    assert len(RESERVED_FIXED_PORTS) >= 20, "the generated set looks truncated"


def test_an_extra_that_is_also_discovered_is_refused() -> None:
    labelled = gen.labelled_fixed_ports()
    with pytest.raises(gen.StaleExtraError, match="7700"):
        gen.render_block(labelled, {7700: "Rust PREFERRED_ENGINE_PORT"})


def test_every_committed_extra_is_undiscoverable() -> None:
    assert not set(gen.EXTRA_PORTS) & set(gen.labelled_fixed_ports())


def test_an_undiscovered_extra_is_rendered_with_its_reason() -> None:
    block = gen.render_block({4455: ["apps/desktop/wdio.conf.ts"]}, {7001: "named elsewhere"})
    assert block == (
        gen.BLOCK_START
        + "        4455,  # apps/desktop/wdio.conf.ts\n"
        + "        7001,  # named elsewhere (extra)\n"
        + gen.BLOCK_END
    )


def test_discovered_provenance_paths_are_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] discovery yields Windows-style relative paths [then] provenance uses POSIX slashes, [else stop]."""
    root = tmp_path / "desktop"
    src = root / "electron" / "src"
    src.mkdir(parents=True)
    (src / "engine.ts").write_text("export const PREFERRED_ENGINE_PORT = 7700;\n", encoding="utf-8")

    original_relative_to = Path.relative_to

    def windows_style_relative(self: Path, other: Path) -> PureWindowsPath:
        rel = original_relative_to(self, other)
        return PureWindowsPath(*rel.parts)

    monkeypatch.setattr(Path, "relative_to", windows_style_relative)

    discovered = gen.discovered_fixed_ports((root,))
    assert discovered[7700] == {"electron/src/engine.ts"}
    assert "\\" not in "".join(discovered[7700])

    labelled = gen.labelled_fixed_ports(((root, "apps/desktop"),))
    assert labelled[7700] == ["apps/desktop/electron/src/engine.ts"]


def test_check_mode_passes_on_the_committed_tree() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.gen_reserved_ports", "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[OK] RESERVED_FIXED_PORTS is current" in proc.stdout
