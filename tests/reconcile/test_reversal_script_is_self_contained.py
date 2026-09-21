"""The generated reversal script must run with nothing but the standard library.

`_write_reversal_script` emits the fallback you run when the app itself is the
thing that is broken, so it may import only what its own header declares. A
refactor that reaches into `apps.shared` from inside that template renders a
script that raises `NameError` the moment it is used -- at exactly the moment
somebody needs their library back.

No fixture needed: the template is rendered with a synthetic payload and read
as source, so this runs everywhere, unlike the acceptance tests that need a
real rekordbox database.

[if] the reversal template names something it does not import [then] this fails, [else stop].
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

from apps.reconcile import remove_track


def _render(tmp_path: Path) -> str:
    """Emit a real reversal script through the production path."""
    footprint = remove_track.Footprint(
        id="123456789",
        exists=True,
        content_row={"ID": "123456789", "Title": "Broken Alpha"},
        cascade_rows={"djmdSongPlaylist": [{"ID": "1", "ContentID": "123456789"}]},
    )
    script = remove_track._write_reversal_script(
        [footprint],
        backup=tmp_path / "master.db.bak",
        backup_dir=tmp_path,
        ts="20260920T000000Z",
        db_path=tmp_path / "master.db",
    )
    return script.read_text()


def test_the_reversal_script_is_valid_python(tmp_path: Path) -> None:
    """[if] the template is rendered [then] it compiles, [else stop]."""
    compile(_render(tmp_path), "restore.py", "exec")


def test_the_reversal_script_names_nothing_it_did_not_import(
    tmp_path: Path,
) -> None:
    """[if] a name is used [then] it is imported, assigned or a builtin, [else stop].

    A deliberately small resolver rather than a linter dependency: collect every
    name the module binds (imports, assignments, defs, arguments, comprehension
    and except targets) and require every load to be one of those or a builtin.
    """
    tree = ast.parse(_render(tmp_path))
    bound: set[str] = set(dir(builtins))
    loaded: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (bound if isinstance(node.ctx, ast.Store) else loaded).add(node.id)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    assert not (loaded - bound), (
        f"the reversal script uses names it never imports: {sorted(loaded - bound)}"
    )


def test_the_reversal_script_imports_nothing_from_this_repo(
    tmp_path: Path,
) -> None:
    """[if] the template imports an app module [then] this fails, [else stop].

    The companion to the check above. A name could be made resolvable by adding
    `from apps.shared import rekordbox_db` to the template, which would satisfy
    the resolver and still leave the script unrunnable wherever the repo is not
    on the path -- which is the whole point of shipping it beside the backup.
    """
    tree = ast.parse(_render(tmp_path))
    local = {"apps", "scripts", "tests"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                a.name for a in node.names if a.name.split(".")[0] in local
            ]
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module.split(".")[0] in local or node.level)
        ):
            offenders.append(node.module)
    assert not offenders, f"the reversal script imports from this repo: {offenders}"


def test_the_rendered_script_really_detects_a_plain_header(tmp_path: Path) -> None:
    """[if] the rendered script sees a plain header [then] it skips SQLCipher, [else stop].

    Executes the decision the rendered script would make, against real header
    bytes, rather than matching its source text. A substring assertion passes
    happily on `b"SQLite format 3\\x00"`, whose escape survived one
    interpretation too many and compares against a literal backslash -- so it
    never matches any real file and quietly sends every plain database back
    through SQLCipher. That is the exact bug this file exists to catch, and a
    text match let it through once already.
    """
    source = _render(tmp_path)
    line = next(
        stripped
        for stripped in (raw.strip() for raw in source.splitlines())
        if stripped.startswith("path=LIVE_DB, unlock=")
    )
    expression = line.split("unlock=", 1)[1].strip()
    assert expression.count("(") == expression.count(")"), (
        f"could not isolate the unlock expression from {line!r}"
    )
    decide = eval(
        "lambda header: " + expression
    )

    assert decide(b"SQLite format 3\x00" + b"\x00" * 80) is False, (
        "a real plain SQLite header must not be sent through SQLCipher"
    )
    assert decide(b"\x8a\x1f\xd3" + bytes(13)) is True, (
        "ciphertext must still be unlocked"
    )
    assert decide(b"") is True, "an unreadable file falls back to SQLCipher"


def test_the_shared_magic_still_starts_with_the_prefix_the_script_uses() -> None:
    """[if] the two header checks drift [then] this fails, [else stop]."""
    from apps.shared import rekordbox_db

    assert rekordbox_db.SQLITE_MAGIC.startswith(b"SQLite format 3")
    assert rekordbox_db.SQLITE_MAGIC == b"SQLite format 3\x00"
