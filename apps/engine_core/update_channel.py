"""``GET /api/v1/update/check`` -- is there a newer Open DJ than this one?

WHY THIS EXISTS SEPARATELY FROM THE TAURI UPDATER

The desktop shell carries ``tauri-plugin-updater``, and that plugin is the
only component that may ever APPLY an update: it re-fetches the manifest,
verifies its minisign signature against the pubkey compiled into the shell,
and swaps the .app bundle. Signature verification and installation must not
be separable, so nothing here downloads or installs anything.

What the plugin CANNOT do is answer the question anywhere except inside the
shell's own webview. An agent driving this app over HTTP has no IPC bridge,
and neither does a browser tab pointed at the engine. Agent-native parity is
a project requirement, so the CHECK is answered here too, over plain HTTP,
from the SAME feed the shell installs from. The two read one directory and
cannot drift: ``tests/engine_core/test_update_channel.py`` asserts this
module's ``UPDATE_FEED_URL`` equals ``UPDATE_FEED_URL`` in the Electron
shell's ``src/updater.ts``. Every release writes both ``latest-mac.yml`` and
``latest.json`` (the latter carries the Electron app to alpha Tauri installs)
there, and this module parses either; ``CHECKED_FEED_FILE`` says which it
reads. (private path) section (d).

TWO VERSIONS, AND WHY BOTH ARE ON SCREEN

The updater keys on SEMVER (``tauri.conf.json`` ``version``), because that is
what the plugin compares. Today every build Open DJ ships is ``0.1.0``: the
number changes when someone cuts a release, not when someone merges. The
identity that actually distinguishes two builds is the git sha and build time
already stamped into the payload manifest, which this engine serves at
``/build-info``.

So this route reports BOTH, and names the awkward case rather than hiding it:
a remote release carrying the same semver as the running build but a different
sha is ``up-to-date`` by the rule the updater will apply, with
``same_version_different_build`` set. A user staring at "you are up to date"
while running a different commit is exactly the wasted afternoon
``build_info`` was written to end; it is not repeated here.

NO SILENT NO-OP

Every failure to reach or parse the manifest is a named fault carrying the
endpoint and the reason. An update channel that quietly answers "up to date"
when its endpoint is unreachable is worse than no channel: it converts an
outage into a false reassurance. There is no branch in this module that can
produce ``up-to-date`` without having read a manifest.

Requirements (mini-PRD):

- ✔︎ ✅ 🎯 A reachable manifest with a higher semver reports
  ``update-available`` naming both versions. -> :func:`compare_versions`
- ✔︎ ✅ 🎯 An unreachable, refused, or malformed endpoint reports a named
  fault, never ``up-to-date``. -> :func:`resolve_update_check`
- ✔︎ ✅ 🎯 A manifest with no entry for this platform reports
  ``platform-unsupported`` naming the key it looked for.
  -> :func:`_platform_entry`
- ✔︎ ✅ 🎯 Equal semver with a different git sha reports ``up-to-date`` AND
  ``same_version_different_build``. -> :func:`resolve_update_check`
- ✔︎ ✅ 🎯 The endpoint this module reads equals the one compiled into the
  shell. -> ``tests/engine_core/test_update_channel.py``

Acceptance tests:

- [if] the endpoint returns a manifest with version 9.9.9 [then] status is
  ``update-available`` with available_version 9.9.9, [else ⛔️].
- [if] the endpoint refuses the connection [then] status is
  ``endpoint-unreachable`` and the body names the URL, [else ⛔️].
- [if] the endpoint answers 404 [then] status is ``endpoint-refused`` carrying
  404, and NOT ``up-to-date``, [else ⛔️].
- [if] the manifest omits this platform key [then] status is
  ``platform-unsupported`` naming darwin-aarch64, [else ⛔️].
- [if] the manifest's version equals the running one but the notes name a
  different sha [then] status is ``up-to-date`` with
  same_version_different_build true, [else ⛔️].
"""

from __future__ import annotations

import time

import httpx

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    BUILD_INFO_PATH,
    BuildIdentity,
    BuildInfoUnavailable,
    resolve_build_info,
)
from apps.engine_core.origin import EngineNotRunning, resolve_origin

# the command line
from apps.engine_core.update_channel_cli import (
    _apply_via_engine,
    _check_locally,
    _check_via_engine,
    _emit,
    _emit_apply,
    _stderr_progress,
    main,
)

#: UPDATE_FEED_URL is THE update feed directory,
#: https://github.com/alex-foster-personal/issue-assets/releases/latest/download,
#: defined with its rationale in update_channel_contract.py.
# constants, status vocabulary, wire models, facade lookup
from apps.engine_core.update_channel_contract import (
    _ARCH_KEYS,
    _OS_KEYS,
    ACTIONABLE_STATUSES,
    ANSWERED,
    APPLY_STATUS_POLL_INTERVAL_S,
    APPLY_TIMEOUT_S,
    CHECKED_FEED_FILE,
    CODE_UPDATE_CHECK_FAILED,
    ELECTRON_FEED_FILE,
    EXIT_APPLIED,
    EXIT_APPLY_FAILED,
    EXIT_NOT_APPLIED,
    FACADE_MODULE,
    FEED_URL_ENV,
    RELAUNCH_BUILD_INFO_TIMEOUT_S,
    REQUEST_TIMEOUT_S,
    STATUS_ENGINE_MALFORMED,
    STATUS_ENGINE_UNREACHABLE,
    TAURI_FEED_FILE,
    UPDATE_APPLY_PATH,
    UPDATE_APPLY_STATUS_PATH,
    UPDATE_CHECK_PATH,
    UPDATE_ENDPOINT,
    UPDATE_FEED_URL,
    UpdateApplyAccepted,
    UpdateApplyRefused,
    UpdateApplyStatusOut,
    UpdateCheckError,
    UpdateCheckOut,
    UpdateStatus,
    facade,
)

# check and apply through a running engine
from apps.engine_core.update_channel_engine_client import (
    ApplyOutcome,
    CheckOutcome,
    _announced_version,
    _apply_failed,
    _apply_with,
    _await_install,
    _await_relaunch,
    _build_identity_slice,
    _check_shortfall,
    _check_with,
    _contextualise,
    _fault,
    _InstallWait,
    _json_object,
    _not_applied,
    _read_identity,
    _refusal_body,
    _shortfall,
    _start_install,
    _verdict,
    _version_higher,
    apply_via_engine,
    check_via_engine,
)

# platform keys, semver, manifest fetch, check resolution
from apps.engine_core.update_channel_feed import (
    _ELECTRON_ZIP_SUFFIX,
    _electron_entry,
    _fetch_manifest,
    _is_electron_feed,
    _manifest_version,
    _mentions_other_sha,
    _platform_entry,
    compare_versions,
    parse_version,
    platform_key,
    resolve_update_check,
    update_endpoint,
)

# the FastAPI check and apply routes
from apps.engine_core.update_channel_routes import (
    _update_check_http_response,
    add_update_apply_route,
    add_update_check_route,
)
from apps.shared import platform_paths
from apps.shared.semver import Semver as _Version
from apps.shared.semver import parse_semver as _parse_semver
from apps.webui.server.shell_commands import (
    ShellBrokerInvalidError,
    ShellBrokerUnavailableError,
    ShellCommandConflictError,
    install_shell_command_broker,
    shell_broker,
)

#: Every name this module bound before the split, private ones included: the
#: facade's job is to keep each of them importable from here.
__all__ = [
    "ACTIONABLE_STATUSES",
    "ANSWERED",
    "APPLY_STATUS_POLL_INTERVAL_S",
    "APPLY_TIMEOUT_S",
    "BUILD_IDENTITY_STATE_ATTR",
    "BUILD_INFO_PATH",
    "CHECKED_FEED_FILE",
    "CODE_UPDATE_CHECK_FAILED",
    "ELECTRON_FEED_FILE",
    "EXIT_APPLIED",
    "EXIT_APPLY_FAILED",
    "EXIT_NOT_APPLIED",
    "FACADE_MODULE",
    "FEED_URL_ENV",
    "RELAUNCH_BUILD_INFO_TIMEOUT_S",
    "REQUEST_TIMEOUT_S",
    "STATUS_ENGINE_MALFORMED",
    "STATUS_ENGINE_UNREACHABLE",
    "TAURI_FEED_FILE",
    "UPDATE_APPLY_PATH",
    "UPDATE_APPLY_STATUS_PATH",
    "UPDATE_CHECK_PATH",
    "UPDATE_ENDPOINT",
    "UPDATE_FEED_URL",
    "_ARCH_KEYS",
    "_ELECTRON_ZIP_SUFFIX",
    "_OS_KEYS",
    "ApplyOutcome",
    "BuildIdentity",
    "BuildInfoUnavailable",
    "CheckOutcome",
    "EngineNotRunning",
    "ShellBrokerInvalidError",
    "ShellBrokerUnavailableError",
    "ShellCommandConflictError",
    "UpdateApplyAccepted",
    "UpdateApplyRefused",
    "UpdateApplyStatusOut",
    "UpdateCheckError",
    "UpdateCheckOut",
    "UpdateStatus",
    "_InstallWait",
    "_Version",
    "_announced_version",
    "_apply_failed",
    "_apply_via_engine",
    "_apply_with",
    "_await_install",
    "_await_relaunch",
    "_build_identity_slice",
    "_check_locally",
    "_check_shortfall",
    "_check_via_engine",
    "_check_with",
    "_contextualise",
    "_electron_entry",
    "_emit",
    "_emit_apply",
    "_fault",
    "_fetch_manifest",
    "_is_electron_feed",
    "_json_object",
    "_manifest_version",
    "_mentions_other_sha",
    "_not_applied",
    "_parse_semver",
    "_platform_entry",
    "_read_identity",
    "_refusal_body",
    "_shortfall",
    "_start_install",
    "_stderr_progress",
    "_update_check_http_response",
    "_verdict",
    "_version_higher",
    "add_update_apply_route",
    "add_update_check_route",
    "apply_via_engine",
    "check_via_engine",
    "compare_versions",
    "facade",
    "httpx",
    "install_shell_command_broker",
    "main",
    "parse_version",
    "platform_key",
    "platform_paths",
    "resolve_build_info",
    "resolve_origin",
    "resolve_update_check",
    "shell_broker",
    "time",
    "update_endpoint",
]


if __name__ == "__main__":
    raise SystemExit(main())
