"""Third-party items ported into the title fixer carry an SPDX id per item (rule R8), not only an aggregate NOTICE."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOCAB = ROOT / "apps" / "tags" / "title_fix_vocab.json"
MODULE = ROOT / "apps" / "tags" / "title_fix_vocab.py"


@pytest.mark.requirement("TFIX-07")
def test_the_ported_vocabulary_file_names_its_license_and_copyright_holder() -> None:
    """[if] the vocabulary file derives from MIT data [then] it carries an SPDX id and the source's copyright line, [else stop]."""
    data = json.loads(VOCAB.read_text(encoding="utf-8"))
    assert data.get("SPDX-License-Identifier") == "MIT", (
        "if the id is missing then the reused item has attribution but no per-item license"
    )
    assert "Maksimov" in data.get("SPDX-FileCopyrightText", ""), "the copyright holder of the ported data must be named"
    assert "mix_descriptors" in data, "control: the loader's key is still there"


@pytest.mark.requirement("TFIX-07")
def test_each_ported_rule_block_is_a_closed_spdx_snippet_with_a_license() -> None:
    """[if] rules are ported into the module [then] each is a closed SPDX snippet with an MIT id, not a file-wide license, [else stop]."""
    source = MODULE.read_text(encoding="utf-8")
    begins, ends = source.count("SPDX-SnippetBegin"), source.count("SPDX-SnippetEnd")
    assert begins == ends and begins >= 2, (
        f"if a snippet is not closed it licenses the rest of the file as MIT: {begins} begin, {ends} end"
    )
    for block in re.findall(r"SPDX-SnippetBegin(.*?)SPDX-SnippetEnd", source, flags=re.S):
        assert "SPDX-License-Identifier: MIT" in block and "SPDX-SnippetCopyrightText" in block, (
            f"a ported block lacks its id: {block[:80]!r}"
        )
    assert not re.search(r"^\s*#\s*SPDX-License-Identifier:.*$", source.split("SPDX-SnippetBegin")[0], flags=re.M), (
        "no file-level license may precede the snippets: the module's own code is not MIT-licensed by this marker"
    )
