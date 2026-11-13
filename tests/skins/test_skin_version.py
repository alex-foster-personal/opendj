import pytest

from apps.shared.semver import parse_semver
from scripts.skins.skin_version import SkinVersionError, check_skin_version

GOOD = {"schema": 1, "version": "1.2.0", "min_app_version": "0.9.0"}


@pytest.mark.parametrize("schema", ["1", 3, True, None])
def test_bad_schema_names_key_and_supported(schema):
    with pytest.raises(SkinVersionError, match=r"schema .*supported: \[1, 2\]"):
        check_skin_version({**GOOD, "schema": schema}, "1.0.0", {1, 2})


@pytest.mark.parametrize("key", ["version", "min_app_version"])
def test_bad_semver_names_key_and_raw(key):
    with pytest.raises(SkinVersionError, match=rf"^{key} 'one.two'"):
        check_skin_version({**GOOD, key: "one.two"}, "1.0.0", {1})


def test_newer_min_app_names_both_versions():
    with pytest.raises(SkinVersionError, match=r"min_app_version 2\.0\.0 .*app version 1\.0\.0"):
        check_skin_version({**GOOD, "min_app_version": "2.0.0"}, "1.0.0", {1})


def test_valid_returns_parsed_versions():
    result = check_skin_version(GOOD, "1.0.0", {1})
    assert result.schema == 1
    assert result.version == parse_semver("1.2.0")
    assert result.min_app_version == parse_semver("0.9.0")
