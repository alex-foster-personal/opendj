"""Local feature flags: a file on disk, read once at startup.

A SEPARATE CONCEPT FROM ENTITLEMENTS, and a separate store (FLAG-01).  A flag
answers "is this code path enabled": engineering-owned, deploy-time,
temporary, the same answer for every user of this daemon.  An entitlement
answers "is this account allowed": billing-owned, persistent, per account.
They share no file, no module and no function here, because conflating them
scatters pricing logic through deploy toggles and makes a billing change need
a code release.  Entitlements live in ``apps.entitlements``.

NO FLAG PLATFORM (FLAG-02, FLAG-03).  openDJ is local-first and the daemon is
loopback by design, so a flag MUST resolve with no network and no server.
That rules out LaunchDarkly, PostHog, Statsig and the hosted modes of Unleash
and Flagsmith by construction, and it makes OpenFeature / Flipt / flagd
answers to a question this project does not ask (provider portability between
hosted vendors).  What is left is the thing that actually fits: one JSON file
in the data dir, read at boot.  Revisit only if a hosted companion service
ever exists.
"""

from __future__ import annotations

from apps.feature_flags.store import (
    APP_MODE_FEATURE_FLAGS,
    APP_MODE_IDS,
    FLAGS,
    FLAGS_FILE_ENV,
    FLAGS_FILENAME,
    FlagDef,
    FlagFileError,
    FlagRefusal,
    FlagState,
    FlagStore,
    flags_path,
    load_flags,
    store_build_refusal,
    store_profile_is_source,
)

__all__ = [
    "APP_MODE_FEATURE_FLAGS",
    "APP_MODE_IDS",
    "FLAGS",
    "FLAGS_FILENAME",
    "FLAGS_FILE_ENV",
    "FlagDef",
    "FlagFileError",
    "FlagRefusal",
    "FlagState",
    "FlagStore",
    "flags_path",
    "load_flags",
    "store_build_refusal",
    "store_profile_is_source",
]
