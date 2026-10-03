"""The jsonschema[format-nongpl] floor must actually check iri/iri-reference.

PR #4754 swapped `jsonschema[format]` (pulls rfc3987, GPL-3.0-or-later) for
`jsonschema[format-nongpl]` (rfc3987-syntax, MIT), to drop the GPL dependency
from the shipped engine payload (C9: permissive-only). That extra only grew an
IRI checker in jsonschema 4.25.0; below that, `iri`/`iri-reference` format
values are accepted unconditionally even with an explicit `FormatChecker`.
Nothing in this repo currently builds a validator with a format checker (see
apps/open_dj/validate.py and scripts/quality_rubric_model.py), so the gap is
latent today -- but the floor itself is the only thing stopping a future
caller from silently losing IRI validation. Pin the behavior, not just the
version string.

Acceptance:
- [if] jsonschema[format-nongpl] resolves below 4.25 [then ⛔️] this test fails
  (floor regression, caught before an installed version drifts under it)
- [if] a FormatChecker validates an invalid iri under the resolved
  jsonschema [then ⛔️] it must raise/report an error, not accept it
"""

from __future__ import annotations

from importlib.metadata import version

from packaging.version import Version


def test_jsonschema_floor_is_at_least_4_25() -> None:
    """Below 4.25, format-nongpl ships no IRI checker at all (REQ: see pyproject.toml:78)."""
    installed = Version(version("jsonschema"))
    assert installed >= Version("4.25"), (
        f"jsonschema {installed} is below 4.25: format-nongpl's IRI checker "
        "(rfc3987-syntax) was only added in 4.25.0, so an iri/iri-reference "
        "format value would silently pass validation"
    )


def test_format_checker_rejects_invalid_iri() -> None:
    """The behavior the floor exists to guarantee, not just the version number."""
    from jsonschema import Draft202012Validator, FormatChecker

    schema = {"type": "string", "format": "iri"}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors("http://[not-a-valid-iri"))
    assert errors, "an invalid iri must be rejected by an explicit FormatChecker"


def test_format_checker_accepts_a_valid_iri() -> None:
    """Negative-control companion: the checker must not reject everything."""
    from jsonschema import Draft202012Validator, FormatChecker

    schema = {"type": "string", "format": "iri"}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors("https://example.com/café"))
    assert not errors, "a valid iri must not be rejected"
