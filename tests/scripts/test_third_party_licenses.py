"""OSSPUB-05: the dmg ships its third-party license texts.

[if] the payload build stops staging THIRD-PARTY-LICENSES.txt [then] this file goes red, [else stop].

Regression lines:
  - if build() stops calling write_payload_license_files, a dmg ships with no attribution
  - if verify() stops calling verify_bundled_licenses, an empty stub file passes the build
  - if the dmg recipe stops checking the mounted app, a payload without the file ships
  - if "MIT" matches inside "Limited", an unidentified license reads as permissive
  - if a copyleft license is absent from the flag report, it ships unreviewed
  - if a vendored _vendor source ships without its license text in the inventory, the dmg redistributes code unattributed
"""
from __future__ import annotations

import inspect
import re
import shutil
from pathlib import Path

import pytest

from scripts import build_engine_payload
from scripts.license_mirrors import PBS_NATIVE_LIBRARIES, REVIEWED_LICENSE_TEXTS
from scripts.license_verify import ECOSYSTEMS, verify_bundled_licenses
from scripts.third_party_licenses import (
    PYTHON_SITES_RELATIVE,
    Cat,
    Component,
    LicenseInventoryError,
    attach_reviewed_license_texts,
    classify_license,
    flag_report,
    python_components,
    render_licenses,
    require_license_texts,
    supplement_components,
    unreviewed_textless_components,
    vendored_source_components,
    write_payload_license_files,
)

pytestmark = pytest.mark.requirement("OSSPUB-05")

REPO_ROOT = Path(__file__).resolve().parents[2]
WIRING_SYMBOL = "write_payload_license_files"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("MIT", Cat.PERMISSIVE),
        ("Apache-2.0 WITH LLVM-exception", Cat.PERMISSIVE),
        ("MIT OR GPL-2.0-or-later", Cat.PERMISSIVE),
        ("GPL-3.0-or-later", Cat.STRONG),
        ("GNU General Public License v3 (GPLv3)", Cat.STRONG),
        ("MPL-2.0 AND MIT", Cat.WEAK),
        ("LGPL-2.1-or-later", Cat.WEAK),
        ("EPL-1.0", Cat.WEAK),
        ("EPL-2.0", Cat.WEAK),
        ("CC-BY-NC-SA-4.0", Cat.NONCOM),
        ("Limited Edition Terms", Cat.UNKNOWN),
        ("", Cat.UNKNOWN),
    ],
)
def test_classification_matches_the_license_family(expression: str, expected: str) -> None:
    assert classify_license(expression) == expected, f"{expression!r} misclassified"


def _component(name: str, license_name: str, texts: list[tuple[str, str]] | None = None) -> Component:
    return Component("python", name, "1.0", license_name, texts=[("LICENSE", f"text of {name}")] if texts is None else texts)


def test_flag_report_lists_copyleft_and_omits_permissive() -> None:
    report = flag_report([_component("gplthing", "GPL-3.0-only"), _component("mitthing", "MIT")])
    flagged_section = report.split("## License identified but no license text")[0]
    assert "gplthing" in flagged_section, "copyleft component missing from the flag table"
    assert "mitthing" not in flagged_section, "control: a permissive component must not be flagged"


def test_flag_report_lists_components_that_ship_without_a_license_text() -> None:
    report = flag_report([_component("textless", "MIT", texts=[])])
    textless_section = report.split("## License identified but no license text")[1]
    assert "textless" in textless_section, "a component with no license file was not reported"


def test_rendered_file_carries_license_text_and_notice_text() -> None:
    component = _component(
        "apachething", "Apache-2.0", [("LICENSE", "APACHE LICENSE BODY"), ("NOTICE", "NOTICE BODY 2020")]
    )
    rendered = render_licenses([component])
    assert "APACHE LICENSE BODY" in rendered, "license text not rendered"
    assert "NOTICE BODY 2020" in rendered, "NOTICE text not rendered"
    assert "apachething 1.0 [python] -- Apache-2.0" in rendered


def test_unreviewed_textless_flags_a_component_with_no_text_or_notice() -> None:
    """Sol P1 (PR #4853): a textless component must block generation unless human-reviewed."""
    textless = _component("mystery-pkg", "MIT", texts=[])
    assert unreviewed_textless_components([textless]) == [textless]


def test_unreviewed_textless_no_longer_excuses_the_bundled_mpg123_component() -> None:
    """Sol P1 (PR #4853 r4167743715): the BUNDLED mpg123 component was wrongly on the
    allowlist, on the mistaken premise that mpg123 itself (not just its npm wrapper) had no
    license text. It does, and is now staged like any other component -- its old allowlist
    entry is gone, not replaced; a same-shaped component must still be rejected."""
    mpg123_shaped = Component(
        "bundled", "mpg123 (compiled to WebAssembly inside mpg123-decoder)", "", "LGPL-2.1-only", texts=[]
    )
    other_bundled_textless = Component("bundled", "some-other-bundled-thing", "", "MIT", texts=[])
    assert unreviewed_textless_components([mpg123_shaped, other_bundled_textless]) == [
        mpg123_shaped,
        other_bundled_textless,
    ]


def test_unreviewed_textless_excuses_the_wasm_audio_decoders_js_wrappers() -> None:
    """Sol P1 (PR #4853 r4167790855): eshaz/wasm-audio-decoders declares MIT in every
    package.json but ships no LICENSE file anywhere in the npm package OR its upstream
    GitHub repo root (verified) -- unlike mpg123, there is no real text to mirror."""
    mpg123_wrapper = Component("javascript", "mpg123-decoder", "1.0.3", "MIT", texts=[])
    flac_wrapper = Component("javascript", "@wasm-audio-decoders/flac", "0.2.11", "MIT", texts=[])
    common_wrapper = Component("javascript", "@wasm-audio-decoders/common", "9.0.7", "MIT", texts=[])
    unrelated_textless = Component("javascript", "some-other-npm-thing", "1.0", "MIT", texts=[])
    assert unreviewed_textless_components(
        [mpg123_wrapper, flac_wrapper, common_wrapper, unrelated_textless]
    ) == [unrelated_textless]


def test_unreviewed_textless_rejects_a_notice_in_place_of_a_license_text() -> None:
    """Sol P1 (PR #4853): a NOTICE is attribution, not a substitute for the license text itself."""
    noticed_only = Component("python", "noticed-pkg", "1.0", "Apache-2.0", texts=[("NOTICE", "notice body")])
    assert unreviewed_textless_components([noticed_only]) == [noticed_only]


def test_unreviewed_textless_rejects_an_empty_license_text() -> None:
    """A present-but-empty text entry must not satisfy the guard either."""
    empty_text = Component("python", "empty-pkg", "1.0", "MIT", texts=[("LICENSE", "")])
    assert unreviewed_textless_components([empty_text]) == [empty_text]


def test_unreviewed_textless_accepts_a_component_with_a_real_license_text() -> None:
    """Control: the ordinary case (actual text present) still passes."""
    real = _component("real-pkg", "MIT")
    assert unreviewed_textless_components([real]) == []


def _fake_repo_for_supplement(repo_root: Path, payload: Path, *, with_native_licenses: bool) -> None:
    (payload / "runtime/lib/python3.11").mkdir(parents=True)
    (payload / "runtime/lib/python3.11/LICENSE.txt").write_text("CPYTHON LICENSE BODY", encoding="utf-8")
    (payload / "models/beatgrid").mkdir(parents=True)
    (payload / "models/beatgrid/LICENSE-beat_this.txt").write_text("BEAT THIS LICENSE", encoding="utf-8")
    font_dir = repo_root / "apps/webui/frontend/static/fonts"
    font_dir.mkdir(parents=True)
    (font_dir / "Anybody-OFL.txt").write_text("FONT LICENSE", encoding="utf-8")
    staged_fonts = payload / "app/apps/webui/frontend/build/fonts"
    staged_fonts.mkdir(parents=True)
    (staged_fonts / "anybody-800-w150-wordmark.woff2").write_bytes(b"wOF2")
    legal_dir = repo_root / "docs/legal"
    legal_dir.mkdir(parents=True, exist_ok=True)
    (legal_dir / "mpg123-COPYING.txt").write_text("MPG123 COPYING BODY", encoding="utf-8")
    (legal_dir / "libFLAC-COPYING.Xiph.txt").write_text("LIBFLAC COPYING BODY", encoding="utf-8")
    if with_native_licenses:
        native_dir = repo_root / "docs/legal/python-build-standalone"
        native_dir.mkdir(parents=True)
        for lib in PBS_NATIVE_LIBRARIES:
            (native_dir / f"LICENSE.{lib}.txt").write_text(f"{lib.upper()} LICENSE BODY", encoding="utf-8")


def test_supplement_refuses_a_cpython_runtime_with_no_native_library_licenses(tmp_path: Path) -> None:
    """Sol P1 (PR #4853): a vague note is not attribution for statically-linked native libraries."""
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=False)
    with pytest.raises(LicenseInventoryError, match="python-build-standalone"):
        supplement_components(repo_root, payload)


def test_supplement_attaches_the_mirrored_native_library_license_texts(tmp_path: Path) -> None:
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    components = supplement_components(repo_root, payload)
    cpython = next(c for c in components if c.name == "CPython (python-build-standalone)")
    assert ("LICENSE.zlib.txt", "ZLIB LICENSE BODY") in cpython.license_texts
    assert ("LICENSE.txt", "CPYTHON LICENSE BODY") in cpython.license_texts


def test_supplement_refuses_a_partial_native_library_mirror(tmp_path: Path) -> None:
    """Codex P2 (PR #4853 r4170677873): one surviving LICENSE.*.txt must not excuse a lost one."""
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    (repo_root / "docs/legal/python-build-standalone/LICENSE.openssl-3.txt").unlink()
    with pytest.raises(LicenseInventoryError, match="openssl-3"):
        supplement_components(repo_root, payload)


def test_the_committed_native_mirror_is_complete() -> None:
    """The real docs/legal mirror carries every required library, so the dmg build will not refuse it."""
    mirror = REPO_ROOT / "docs/legal/python-build-standalone"
    assert sorted(p.name for p in mirror.glob("LICENSE.*.txt")) == sorted(f"LICENSE.{lib}.txt" for lib in PBS_NATIVE_LIBRARIES)


def _textless(name: str, version: str) -> Component:
    return Component("python", name, version, "GNU General Public License v3 (GPLv3)", texts=[])


def test_reviewed_text_is_staged_for_the_exact_pinned_rbox_release() -> None:
    """Issue #5143: rbox 0.1.7 ships no text; the committed canonical GPL-3.0 text stands in."""
    (rbox,) = attach_reviewed_license_texts(REPO_ROOT, [_textless("rbox", "0.1.7")])
    assert [name for name, _ in rbox.license_texts] == ["GPL-3.0.txt"]
    assert "GNU GENERAL PUBLIC LICENSE" in rbox.license_texts[0][1]
    assert "issues/5143" in rbox.note, "the staged exception must point at the human decision"
    require_license_texts([rbox])


@pytest.mark.parametrize(("name", "version"), [("rbox", "0.1.8"), ("rbox", "0.1.6"), ("not-rbox", "0.1.7")])
def test_reviewed_text_does_not_cover_any_other_release_or_package(name: str, version: str) -> None:
    """The exception is pinned to name AND version: an rbox upgrade must fail the build until reviewed."""
    (component,) = attach_reviewed_license_texts(REPO_ROOT, [_textless(name, version)])
    assert component.license_texts == []
    with pytest.raises(LicenseInventoryError, match=f"{name} {version}"):
        require_license_texts([component])


def test_reviewed_text_never_replaces_a_text_the_release_does_ship() -> None:
    """Control for the opposite overshoot: the canonical text fills a gap, it never overrides upstream."""
    shipped = Component("python", "rbox", "0.1.7", "GPL-3.0-only", texts=[("LICENSE", "UPSTREAM TEXT")])
    (rbox,) = attach_reviewed_license_texts(REPO_ROOT, [shipped])
    assert rbox.license_texts == [("LICENSE", "UPSTREAM TEXT")]


def test_reviewed_text_refuses_a_text_that_drifted_from_its_pinned_sha256(tmp_path: Path) -> None:
    entry = next(e for e in REVIEWED_LICENSE_TEXTS if e.name == "rbox")
    drifted = tmp_path / entry.text_relative
    drifted.parent.mkdir(parents=True)
    drifted.write_text("not the GPL", encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="sha256"):
        attach_reviewed_license_texts(tmp_path, [_textless(entry.name, entry.version)])


def test_supplement_refuses_a_payload_whose_spa_ships_no_anybody_font(tmp_path: Path) -> None:
    """Sol P1 (PR #4853 r4171100737): attribute the font only when the staged SPA carries it."""
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    (payload / "app/apps/webui/frontend/build/fonts/anybody-800-w150-wordmark.woff2").unlink()
    with pytest.raises(LicenseInventoryError, match="Anybody font"):
        supplement_components(repo_root, payload)


def test_supplement_refuses_an_mpg123_payload_with_no_copying_file(tmp_path: Path) -> None:
    """Sol P1 (PR #4853 r4167743715): mpg123's own license is obtainable; stop exempting it."""
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    (repo_root / "docs/legal/mpg123-COPYING.txt").unlink()
    with pytest.raises(LicenseInventoryError, match="mpg123"):
        supplement_components(repo_root, payload)


def test_supplement_refuses_a_payload_with_no_libflac_license(tmp_path: Path) -> None:
    """Codex P1 (PR #4853): the FLAC wrapper embeds libFLAC, whose BSD-3-Clause text must ship."""
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    (repo_root / "docs/legal/libFLAC-COPYING.Xiph.txt").unlink()
    with pytest.raises(LicenseInventoryError, match="libFLAC"):
        supplement_components(repo_root, payload)
    _fake_repo_for_supplement(tmp_path / "r2", tmp_path / "p2", with_native_licenses=True)
    flac = next(c for c in supplement_components(tmp_path / "r2", tmp_path / "p2") if c.name.startswith("libFLAC"))
    assert flac.license_texts == [("libFLAC-COPYING.Xiph.txt", "LIBFLAC COPYING BODY")]


def test_supplement_attaches_the_mpg123_copying_text(tmp_path: Path) -> None:
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    components = supplement_components(repo_root, payload)
    mpg123 = next(c for c in components if c.name.startswith("mpg123"))
    assert mpg123.license_texts == [("mpg123-COPYING.txt", "MPG123 COPYING BODY")]


_VENDOR_HEADER = """\
# Generated from:
#   https://github.com/Deep-Symmetry/crate-digger/blob/master/src/main/kaitai/foo.ksy
# Upstream license: Eclipse Public License 1.0 (EPL-1.0).
"""


def test_vendored_source_scan_attaches_epl_text_for_the_committed_pdb_parser() -> None:
    components = vendored_source_components(REPO_ROOT)
    pdb = [c for c in components if "rekordbox_pdb" in c.name]
    assert len(pdb) == 1, f"expected one rekordbox_pdb component, got { [c.name for c in components] }"
    (component,) = pdb
    assert component.license == "EPL-2.0"
    text = "\n".join(body for _, body in component.license_texts)
    assert "Eclipse Public License - v 2.0" in text
    assert "Secondary Licenses" in text
    rendered = render_licenses(components)
    assert "rekordbox_pdb" in rendered
    assert "Eclipse Public License - v 2.0" in rendered


def test_supplement_includes_a_valid_vendored_source_from_the_fake_repo(tmp_path: Path) -> None:
    repo_root, payload = tmp_path / "repo", tmp_path / "payload"
    _fake_repo_for_supplement(repo_root, payload, with_native_licenses=True)
    vendor = repo_root / "apps/x/_vendor"
    vendor.mkdir(parents=True)
    (vendor / "foo.py").write_text(_VENDOR_HEADER + "x = 1\n", encoding="utf-8")
    (repo_root / "docs/legal/EPL-1.0.txt").write_text(
        "Eclipse Public License - v 1.0\nAgreement Steward\n", encoding="utf-8"
    )
    components = supplement_components(repo_root, payload)
    assert any(c.name.startswith("CPython") for c in components)
    vendored = next(c for c in components if "foo.py" in c.name)
    assert vendored.license == "EPL-1.0"
    assert vendored.ecosystem == "bundled"


def test_vendored_source_refuses_a_missing_header_or_missing_legal_text(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    vendor = repo_root / "apps/x/_vendor"
    vendor.mkdir(parents=True)
    (vendor / "bare.py").write_text("# no license header\n", encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="Upstream license"):
        vendored_source_components(repo_root)
    (vendor / "bare.py").write_text(_VENDOR_HEADER + "x = 1\n", encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="EPL-1.0"):
        vendored_source_components(repo_root)


def test_every_apps_vendor_py_file_appears_in_the_vendored_scan() -> None:
    expected = [
        path
        for path in (REPO_ROOT / "apps").rglob("_vendor/*.py")
        if path.name != "__init__.py" and "node_modules" not in path.parts
    ]
    scanned = vendored_source_components(REPO_ROOT)
    scanned_names = {c.name.split(" ", 1)[0] for c in scanned}
    for path in expected:
        assert path.name in scanned_names, f"{path} missing from vendored_source_components"


def test_write_payload_license_files_is_wired_to_the_textless_guard() -> None:
    """Codex P1 (PR #4853 r4170547306): `collect_all` drives real pnpm/cargo/filesystem
    calls that a unit test cannot stage as a disposable fixture (RUST_CRATES names real
    crates under the real repo root, and `--locked` ties it to the live Cargo.lock), so
    this cannot exercise the guard end to end without becoming an acceptance test. Instead
    of monkeypatching `collect_all` (banned: AGENTS.md "No mocks and locked real fixtures"),
    assert the real source wires collect_all's output straight into the real guard -- the
    same static-wiring pattern already used below for build()/verify() -- backed by the
    control in the next test, which proves this check goes false when the wiring breaks.
    The guard itself (`unreviewed_textless_components`) is exercised directly, with real
    `Component` values, by the tests above.
    """
    source = inspect.getsource(write_payload_license_files)
    assert _calls(source, "collect_all"), "write_payload_license_files no longer calls collect_all"
    assert _calls(source, "require_license_texts"), (
        "write_payload_license_files no longer runs the textless guard on what collect_all returns"
    )
    assert _calls(inspect.getsource(require_license_texts), "unreviewed_textless_components")


def test_textless_guard_wiring_check_fires_when_either_call_is_removed() -> None:
    """Control: the wiring predicate must go false when either call is removed."""
    source = inspect.getsource(write_payload_license_files)
    assert _calls(source, "collect_all") and _calls(source, "require_license_texts"), (
        "control precondition: the unmutated source must contain both calls, or removing them proves nothing"
    )
    mutated_collect = source.replace("collect_all(", "noop(")
    assert not _calls(mutated_collect, "collect_all")
    mutated_guard = source.replace("require_license_texts(", "noop(")
    assert not _calls(mutated_guard, "require_license_texts")


def _write_dist_info(site: Path, name: str, license_text: str) -> None:
    dist_info = site / f"{name}-1.0.dist-info"
    (dist_info / "licenses").mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(f"{dist_info.name}/licenses/LICENSE,,\n", encoding="utf-8")
    (dist_info / "licenses" / "LICENSE").write_text(license_text, encoding="utf-8")


def _write_all_python_sites(payload: Path, *, skip: str | None = None) -> None:
    """Stage a minimal dist-info under every configured Python site but `skip`."""
    for i, relative in enumerate(PYTHON_SITES_RELATIVE):
        if relative == skip:
            continue
        _write_dist_info(payload / relative, f"demo-pkg-{i}", f"DEMO LICENSE TEXT {i}")


def test_python_inventory_reads_the_staged_dist_info(tmp_path: Path) -> None:
    _write_all_python_sites(tmp_path)
    components = python_components(tmp_path)
    assert sorted(c.name for c in components) == [f"demo-pkg-{i}" for i in range(len(PYTHON_SITES_RELATIVE))]
    assert components[0].license == "MIT"
    assert components[0].license_texts



def test_python_inventory_collects_a_vendored_native_license_outside_dist_info(tmp_path: Path) -> None:
    """Codex P1 (PR #4853): soundfile ships libsndfile with its COPYING under _soundfile_data/, not .dist-info."""
    _write_all_python_sites(tmp_path)
    site = tmp_path / PYTHON_SITES_RELATIVE[0]
    dist_info = site / "demo-pkg-0-1.0.dist-info"
    (site / "demo_pkg/_data").mkdir(parents=True)
    (site / "demo_pkg/_data/COPYING").write_text("VENDORED LGPL TEXT", encoding="utf-8")
    (site / "demo_pkg/license.py").write_text("LICENSE_URL = 'x'", encoding="utf-8")
    record = dist_info / "RECORD"
    record.write_text(record.read_text(encoding="utf-8") + "demo_pkg/_data/COPYING,,\ndemo_pkg/license.py,,\n", encoding="utf-8")
    demo = next(c for c in python_components(tmp_path) if c.name == "demo-pkg-0")
    assert ("demo_pkg/_data/COPYING", "VENDORED LGPL TEXT") in demo.license_texts
    assert ("LICENSE", "DEMO LICENSE TEXT 0") in demo.license_texts, "control: the dist-info text is still collected"
    assert all(not name.endswith(".py") for name, _ in demo.texts), "control: a license-named module is not a license text"
    vendored = next(c for c in python_components(tmp_path) if c.name == "demo-pkg-0 vendored demo_pkg/_data/COPYING")
    assert vendored.license == "" and vendored.category == Cat.UNKNOWN, "a vendored text is never given the wrapper's license"
    assert "demo-pkg-0 vendored" in flag_report([demo, vendored]).split("## License identified but no license text")[0]


def test_python_inventory_refuses_a_payload_with_no_site(tmp_path: Path) -> None:
    with pytest.raises(LicenseInventoryError):
        python_components(tmp_path)


@pytest.mark.parametrize("missing_site", PYTHON_SITES_RELATIVE)
def test_python_inventory_refuses_one_missing_site_even_with_another_present(
    tmp_path: Path, missing_site: str
) -> None:
    """Sol P1 (PR #4853): a missing site must not be silently excused by another existing one."""
    _write_all_python_sites(tmp_path, skip=missing_site)
    with pytest.raises(LicenseInventoryError, match=re.escape(missing_site)):
        python_components(tmp_path)


def test_python_inventory_refuses_a_site_with_no_measurable_distribution(tmp_path: Path) -> None:
    """An existing but empty site is unmeasurable, not zero components by design."""
    _write_all_python_sites(tmp_path)
    empty_site = tmp_path / PYTHON_SITES_RELATIVE[0]
    for entry in empty_site.iterdir():
        shutil.rmtree(entry)
    with pytest.raises(LicenseInventoryError):
        python_components(tmp_path)


def _good_payload(payload: Path) -> None:
    """A payload whose inventory comes from the REAL renderer, one licensed component per ecosystem."""
    payload.mkdir(parents=True, exist_ok=True)
    components = [
        Component(eco, f"{eco}-pkg", "1.0", "MIT", texts=[("LICENSE", f"{eco.upper()} LICENSE " + "x" * 30_000)])
        for eco in ECOSYSTEMS
    ]
    (payload / "THIRD-PARTY-LICENSES.txt").write_text(render_licenses(components), encoding="utf-8")
    (payload / "NOTICE").write_text("notice", encoding="utf-8")
    (payload / "LICENSE").write_text("license", encoding="utf-8")


def test_verify_accepts_a_complete_payload(tmp_path: Path) -> None:
    _good_payload(tmp_path)
    verify_bundled_licenses(tmp_path)


@pytest.mark.parametrize("missing", ["THIRD-PARTY-LICENSES.txt", "NOTICE", "LICENSE"])
def test_verify_fires_when_a_file_is_missing_or_empty(tmp_path: Path, missing: str) -> None:
    """Control: the same checker that accepts the good payload must reject each break."""
    _good_payload(tmp_path)
    (tmp_path / missing).unlink()
    with pytest.raises(LicenseInventoryError):
        verify_bundled_licenses(tmp_path)
    (tmp_path / missing).write_text("", encoding="utf-8")
    with pytest.raises(LicenseInventoryError):
        verify_bundled_licenses(tmp_path)


def test_verify_fires_when_an_ecosystem_is_missing_or_the_file_is_a_stub(tmp_path: Path) -> None:
    _good_payload(tmp_path)
    inventory = tmp_path / "THIRD-PARTY-LICENSES.txt"
    inventory.write_text(inventory.read_text(encoding="utf-8").replace("[rust]", "[none]"), encoding="utf-8")
    with pytest.raises(LicenseInventoryError):
        verify_bundled_licenses(tmp_path)
    inventory.write_text("[python] [javascript] [rust] [bundled]", encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="chars"):
        verify_bundled_licenses(tmp_path)


def test_verify_rejects_a_padded_stub_that_carries_only_the_markers(tmp_path: Path) -> None:
    """Sol P1 (PR #4853 r4171176565): four ecosystem markers plus padding are not an inventory."""
    _good_payload(tmp_path)
    stub = "".join(f"{n} [{eco}] -- MIT\n" for n, eco in enumerate(ECOSYSTEMS)) + "x" * 200_000
    (tmp_path / "THIRD-PARTY-LICENSES.txt").write_text(stub, encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="component records"):
        verify_bundled_licenses(tmp_path)


def test_verify_rejects_a_declared_count_that_does_not_match_the_records(tmp_path: Path) -> None:
    _good_payload(tmp_path)
    inventory = tmp_path / "THIRD-PARTY-LICENSES.txt"
    inventory.write_text(inventory.read_text(encoding="utf-8").replace("Components: 4", "Components: 5"), encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="declares 5"):
        verify_bundled_licenses(tmp_path)


def test_verify_rejects_a_component_whose_license_text_was_not_rendered(tmp_path: Path) -> None:
    _good_payload(tmp_path)
    inventory = tmp_path / "THIRD-PARTY-LICENSES.txt"
    inventory.write_text(inventory.read_text(encoding="utf-8").replace("Applies to: rust-pkg 1.0", "Applies to: ?"), encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="rust-pkg"):
        verify_bundled_licenses(tmp_path)


def test_verify_rejects_attribution_lines_whose_license_bodies_were_dropped(tmp_path: Path) -> None:
    """Sol P1 (PR #4853 r4171573252): an `Applies to:` header with no body under it is not a license text."""
    _good_payload(tmp_path)
    inventory = tmp_path / "THIRD-PARTY-LICENSES.txt"
    gutted = re.sub(r"(Applies to: .*\n={78}\n)[^=]*", r"\1\n", inventory.read_text(encoding="utf-8"))
    inventory.write_text(gutted + "x" * 200_000, encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match="no rendered license text"):
        verify_bundled_licenses(tmp_path)


def test_verify_matches_attribution_on_whole_member_labels(tmp_path: Path) -> None:
    """`foo 1.0` is not attributed by a block that applies only to `barfoo 1.0` (Grok/Codex P2, PR #4853)."""
    components = [
        Component(eco, f"{eco}-pkg", "1.0", "MIT", texts=[("LICENSE", f"{eco} " + "x" * 30_000)]) for eco in ECOSYSTEMS
    ] + [Component("python", "barfoo", "1.0", "MIT", texts=[("LICENSE", "BARFOO TEXT")])]
    _good_payload(tmp_path)
    inventory = tmp_path / "THIRD-PARTY-LICENSES.txt"
    rendered = render_licenses([*components, Component("python", "foo", "1.0", "MIT", texts=[])])
    inventory.write_text(rendered, encoding="utf-8")
    with pytest.raises(LicenseInventoryError, match=re.escape("'foo 1.0'")):
        verify_bundled_licenses(tmp_path)
    inventory.write_text(render_licenses(components), encoding="utf-8")
    verify_bundled_licenses(tmp_path)


def test_verify_accepts_a_known_textless_component_without_rendered_text(tmp_path: Path) -> None:
    """Control for the opposite overshoot: reviewed textless components must not fail the verify step."""
    components = [
        Component(eco, f"{eco}-pkg", "1.0", "MIT", texts=[("LICENSE", f"{eco} " + "x" * 30_000)]) for eco in ECOSYSTEMS
    ] + [Component("javascript", "mpg123-decoder", "1.0.3", "MIT", texts=[])]
    _good_payload(tmp_path)
    (tmp_path / "THIRD-PARTY-LICENSES.txt").write_text(render_licenses(components), encoding="utf-8")
    verify_bundled_licenses(tmp_path)


def _calls(source: str, symbol: str) -> bool:
    return f"{symbol}(" in source


def test_build_stages_and_verify_checks_the_license_files() -> None:
    assert _calls(inspect.getsource(build_engine_payload.build), WIRING_SYMBOL), (
        "build() no longer stages THIRD-PARTY-LICENSES.txt: the dmg would ship without attribution"
    )
    assert _calls(inspect.getsource(build_engine_payload.verify), "verify_bundled_licenses"), (
        "verify() no longer checks the license files"
    )


def test_wiring_check_fires_on_a_build_without_the_call() -> None:
    """Control: the wiring predicate must go false when the call is removed."""
    mutated = inspect.getsource(build_engine_payload.build).replace(WIRING_SYMBOL, "noop")
    assert not _calls(mutated, WIRING_SYMBOL)


def test_dmg_recipe_checks_the_mounted_app_for_the_license_files() -> None:
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    recipe = justfile.split("\ndmg lane=''", 1)[1]
    assert "for licensed in THIRD-PARTY-LICENSES.txt NOTICE LICENSE" in recipe, (
        "the dmg verify step no longer asserts the license files are inside the mounted app"
    )


# ----- NOTICE credit completeness (issue #6955) -----

NOTICE_CREDIT_RULES: list[tuple[str, list[str]]] = [
    ("crate-digger", ["Eclipse Public License 2.0"]),
    ("rekordcrate", ["Mozilla Public License 2.0"]),
    ("python-prodj-link", ["Apache-2.0"]),
    ("traktor-nml-utils", ["GPL-3.0"]),
    ("beat-link", ["Eclipse Public License 2.0"]),
    ("YapDatabase", ["BSD"]),
    ("CPJKU/madmom", ["BSD-2-Clause"]),
    ("Signalsmith Stretch", ["MIT", "Geraint Luff"]),
]


def _notice_bullet_at_index(text: str, idx: int) -> str | None:
    start = text.rfind("\n* ", 0, idx)
    if start < 0:
        if idx < len(text) and text.startswith("* "):
            start = -1
        else:
            return None
    chunk_start = start + 1 if start >= 0 else 0
    rest = text[chunk_start:]
    lines = rest.splitlines()
    if not lines:
        return None
    collected = [lines[0]]
    for line in lines[1:]:
        if line.startswith("* "):
            break
        if not line.strip():
            break
        if line.startswith("    "):
            collected.append(line)
            continue
        break
    return "\n".join(collected)


def _notice_bullets_for_needle(text: str, needle: str) -> list[str]:
    bullets: list[str] = []
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx < 0:
            break
        bullet = _notice_bullet_at_index(text, idx)
        if bullet and bullet not in bullets:
            bullets.append(bullet)
        pos = idx + len(needle)
    return bullets


def _notice_credit_problems(text: str) -> list[str]:
    problems: list[str] = []
    for needle, tokens in NOTICE_CREDIT_RULES:
        bullets = _notice_bullets_for_needle(text, needle)
        if not bullets:
            problems.append(f"{needle}: missing credit")
            continue
        if not any(all(token in bullet for token in tokens) for bullet in bullets):
            problems.extend(
                f"{needle}: missing {token!r} in credit"
                for token in tokens
                if not any(token in bullet for bullet in bullets)
            )
    return problems


def _pyproject_license_files() -> list[str]:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"license-files\s*=\s*\[(.*?)\]", pyproject, re.DOTALL)
    assert match, "pyproject.toml must declare license-files"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_repo_notice_credits_the_bundled_and_prior_art_licenses() -> None:
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert _notice_credit_problems(notice) == []


def test_repo_notice_credits_fail_when_signalsmith_is_removed() -> None:
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    start = notice.index("* Signalsmith Stretch")
    end = notice.index("\nBundled outside the language package managers:")
    mutated = notice[:start] + notice[end:]
    problems = _notice_credit_problems(mutated)
    assert any("Signalsmith" in p for p in problems)


def test_repo_notice_credits_fail_when_madmom_says_bsd_3() -> None:
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    bullets = _notice_bullets_for_needle(notice, "CPJKU/madmom")
    assert bullets
    bullet = bullets[0]
    bad_bullet = bullet.replace("BSD-2-Clause", "BSD-3-Clause")
    mutated = notice.replace(bullet, bad_bullet, 1)
    problems = _notice_credit_problems(mutated)
    assert any("CPJKU/madmom" in p or "madmom" in p for p in problems)


def test_pyproject_license_files_include_license_and_notice() -> None:
    files = _pyproject_license_files()
    assert files == ["LICENSE", "NOTICE"]


def test_pyproject_license_files_control_without_notice() -> None:
    """Control: license-files with LICENSE only must not pass the NOTICE assertion."""
    files = re.findall(r'"([^"]+)"', 'license-files = ["LICENSE"]')
    assert "LICENSE" in files
    assert "NOTICE" not in files
