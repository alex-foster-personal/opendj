"""Paths, JSON config and OpenRouter key file for the title-fix store (split from title_fix_store)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from apps.shared.private_files import is_owner_only, restrict_to_owner

from .title_fix_store_types import CFG, POLICIES, ConfigError

# ----------------------------------------------------------------- paths


@dataclass(frozen=True)
class StorePaths:
    state_db: Path

    @property
    def store_db(self) -> Path:
        return self.state_db.parent / CFG.STORE_FILENAME

    @property
    def config(self) -> Path:
        return self.state_db.parent / CFG.CONFIG_FILENAME

    @property
    def key(self) -> Path:
        # the library DATA dir, by the same layout rule as shell_output_health.data_dir_from_state_db (apps/tags may
        # not import the webui): data_dir/state/state.db is nested, anything else is flat beside the database
        data_dir = self.state_db.parent.parent if self.state_db.parent.name == "state" else self.state_db.parent
        return data_dir / CFG.KEY_FILENAME


# ----------------------------------------------------------------- config


@dataclass(frozen=True)
class TitleFixConfig:
    enabled: bool = CFG.DEFAULT_ENABLED
    auto_apply_policy: str = CFG.DEFAULT_POLICY
    spend_cap_usd: float = CFG.DEFAULT_SPEND_CAP_USD
    tag_families: tuple[str, ...] = CFG.TAG_FAMILIES

    def validated(self) -> TitleFixConfig:
        if not isinstance(self.enabled, bool):
            raise ConfigError("enabled must be a boolean")
        if self.auto_apply_policy not in POLICIES:
            raise ConfigError(f"auto_apply_policy must be one of {POLICIES}, got {self.auto_apply_policy!r}")
        cap = self.spend_cap_usd
        if isinstance(cap, bool) or not isinstance(cap, int | float):
            raise ConfigError("spend_cap_usd must be a number")
        if not CFG.SPEND_CAP_MIN_USD <= cap <= CFG.SPEND_CAP_MAX_USD:
            raise ConfigError(f"spend_cap_usd must be between {CFG.SPEND_CAP_MIN_USD} and {CFG.SPEND_CAP_MAX_USD}")
        unknown = [f for f in self.tag_families if f not in CFG.TAG_FAMILIES]
        if unknown:
            raise ConfigError(f"unknown tag families {unknown}; known: {CFG.TAG_FAMILIES}")
        return TitleFixConfig(self.enabled, self.auto_apply_policy, float(cap), tuple(dict.fromkeys(self.tag_families)))


def load_config(paths: StorePaths) -> TitleFixConfig:
    if not paths.config.exists():
        return TitleFixConfig()
    try:
        raw = json.loads(paths.config.read_text(encoding="utf-8"))
    except ValueError as exc:  # truncated or otherwise invalid JSON (also a bad UTF-8 byte): name the file to repair
        raise ConfigError(f"{paths.config} is not valid JSON ({exc}); repair or delete it") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{paths.config} is not a JSON object")
    known = {"enabled", "auto_apply_policy", "spend_cap_usd", "tag_families"}
    if unknown := set(raw) - known:
        raise ConfigError(f"{paths.config} has unknown keys {sorted(unknown)}")
    if missing := known - set(raw):  # save_config always writes every key: a partial file is damage, not "use defaults"
        raise ConfigError(
            f"{paths.config} is missing keys {sorted(missing)}; repair or delete it to restore the defaults"
        )
    base = TitleFixConfig()
    families = raw.get("tag_families", base.tag_families)
    if not isinstance(families, (list, tuple)):
        raise ConfigError(f"{paths.config}: tag_families must be a list, got {type(families).__name__}")
    return TitleFixConfig(
        enabled=raw.get("enabled", base.enabled),
        auto_apply_policy=raw.get("auto_apply_policy", base.auto_apply_policy),
        spend_cap_usd=raw.get("spend_cap_usd", base.spend_cap_usd),
        tag_families=tuple(families),
    ).validated()


def save_config(paths: StorePaths, config: TitleFixConfig) -> TitleFixConfig:
    valid = config.validated()
    payload = {**asdict(valid), "tag_families": list(valid.tag_families)}
    _atomic_write(paths.config, json.dumps(payload, indent=2) + "\n", owner_only=False)
    return valid


# ----------------------------------------------------------------- key


@dataclass(frozen=True)
class KeyStatus:
    configured: bool
    source: Literal["user", "env", "none"]
    hint: str | None  # last CFG.KEY_HINT_CHARS chars only; never the key


def _hint(key: str) -> str:
    """The last KEY_HINT_CHARS characters of a key long enough that they are a small part of it; a short key is fully
    opaque, so the hint can never reveal the secret itself."""
    if len(key) < CFG.KEY_HINT_MIN_LEN:
        return "..."
    return "..." + key[-CFG.KEY_HINT_CHARS :]


def _read_user_key(paths: StorePaths) -> str | None:
    if not paths.key.exists():
        return None
    if not is_owner_only(paths.key):
        raise ConfigError(
            f"the API key file {paths.key} is readable by other accounts; restrict it to this account (chmod 600 on "
            "POSIX, owner-only ACL on Windows) or save the key again from the Title Fix tab"
        )
    key = paths.key.read_text(encoding="utf-8").strip()
    if not key:  # a saved-but-empty key is a broken save, never a silent fall back to the environment key
        raise ConfigError(
            f"the API key file {paths.key} is empty; save the key again or clear it from the Title Fix tab"
        )
    return key


def resolve_key(paths: StorePaths) -> tuple[str | None, KeyStatus]:
    """The effective key (user file > environment) and its masked status."""
    user = _read_user_key(paths)
    if user:
        return user, KeyStatus(True, "user", _hint(user))
    env = os.environ.get(CFG.OPENROUTER_KEY_ENV, "").strip()
    if env:
        return env, KeyStatus(True, "env", _hint(env))
    return None, KeyStatus(False, "none", None)


def save_user_key(paths: StorePaths, key: str) -> KeyStatus:
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise ConfigError("the API key must be a single non-empty token")
    _atomic_write(paths.key, key, owner_only=True)
    return resolve_key(paths)[1]


def clear_user_key(paths: StorePaths) -> KeyStatus:
    paths.key.unlink(missing_ok=True)
    return resolve_key(paths)[1]


def _atomic_write(target: Path, text: str, *, owner_only: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if owner_only:
            restrict_to_owner(Path(tmp_name))
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
