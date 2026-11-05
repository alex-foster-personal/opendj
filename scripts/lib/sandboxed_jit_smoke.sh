# Prove a numba JIT runs under the App Store signing, inside the App Sandbox.
#
# WHY (INSTALL-3287). Open DJ's own analysis runs librosa, whose numba/llvmlite
# JIT makes ordinary memory executable. Under the hardened runtime that is a
# CODESIGNING SIGKILL unless the process's OWN signature carries
# com.apple.security.cs.allow-unsigned-executable-memory (INSTALL-26, the
# Developer ID dmg). Measured Wed 16 Sep 2026 on this repo's payload with a
# sandboxed parent and an inherit-signed interpreter:
#
#   payload signed as it was (inherit, --options runtime)       SIGKILL
#   the key added to the PARENT's entitlements instead          SIGKILL
#   inherit plus the key on the interpreter                     JIT runs
#   inherit only, payload signed WITHOUT --options runtime      JIT runs
#
# Hardened-runtime keys are read per process and are not inherited, so the
# parent cannot grant it. ship_appstore.sh takes the last row: the Mac App
# Store does not require the hardened runtime (notarization does), and it
# keeps the helper at the inherit-only entitlements App Review expects
# instead of adding a key to justify.
#
# WHY A HARNESS. A binary carrying com.apple.security.inherit only runs as
# the child of a sandboxed process, so this compiles a small parent
# (sandboxed_jit_probe.c), bundles it with a CLONE of the signed payload, signs
# the parent with the app's own sandbox entitlements, and reads how the child
# ended. The payload copy keeps its signatures byte for byte; only the parent
# is new.
#
# WHICH IDENTITY. The payload is signed with the store's Apple Distribution
# certificate, and Apple says distribution-signed code may refuse to launch
# locally (it is meant to be re-signed by the store). A probe signed that way
# never runs, and the verdict below reports UNKNOWN and aborts every --build.
# So sandboxed_jit_probe_identity picks an identity that RUNS: the payload's
# own when it is not a store distribution identity (the Developer ID
# measurements above), else an Apple Development identity from the keychain,
# else ad-hoc. When that differs from the payload's identity, the CLONE is
# re-signed with it, with the same inherit entitlements and the same absence
# of the hardened runtime, so the probe measures the signing SHAPE the store
# artifact carries; the shipped payload's own signatures are never touched
# (the static runtime-flag check in ship_appstore.sh reads those directly).
# MDT_MAS_PROBE_CERT overrides the choice.
#
# Source it, do not execute it. No `set` here: see scripts/lib/macho.sh.

SANDBOXED_JIT_PROBE_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sandboxed_jit_probe.c"
# macho_files, for re-signing the payload clone with the same file set the
# store signing used.
# shellcheck source=scripts/lib/macho.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/macho.sh"
# Keys a provisioning profile must authorize. A locally built probe carries no
# profile, so macOS would refuse to launch it with these, which would measure
# the probe rather than the payload. They do not bear on executable memory.
SANDBOXED_JIT_PROFILE_KEYS=(com.apple.application-identifier com.apple.developer.team-identifier)

# sandboxed_jit_probe_identity <payload-identity>
# Prints one line: the identity the probe is signed with, a tab, and why.
# Never fails: ad-hoc always signs, and whether the result RUNS is what the
# verdict measures (an UNKNOWN there is still fatal, so this cannot turn a
# probe that never ran into a pass).
sandboxed_jit_probe_identity() {
    local payload_identity="$1" identities dev=""
    if [ -n "${MDT_MAS_PROBE_CERT:-}" ]; then
        printf '%s\t%s\n' "$MDT_MAS_PROBE_CERT" "MDT_MAS_PROBE_CERT is set"
        return 0
    fi
    case "$payload_identity" in
        "Apple Distribution:"* | "3rd Party Mac Developer Application:"* | "Mac App Distribution:"*)
            identities="$(security find-identity -v -p codesigning 2>/dev/null)" || identities=""
            # -m1 rather than `| head -1`: under pipefail an early head exit can
            # SIGPIPE grep and turn a match into a failed pipeline.
            dev="$(grep -m1 -oE '"(Apple Development|Mac Developer): [^"]*"' <<<"$identities" | tr -d '"')" || dev=""
            if [ -n "$dev" ]; then
                printf '%s\t%s\n' "$dev" "an Apple Development identity from the keychain, because '$payload_identity' is a store distribution identity and distribution-signed code can refuse to launch locally"
            else
                printf -- '-\t%s\n' "ad-hoc, because '$payload_identity' is a store distribution identity and the keychain holds no Apple Development identity"
            fi
            ;;
        *)
            printf '%s\t%s\n' "$payload_identity" "the payload's own identity, which is not a store distribution identity and runs locally"
            ;;
    esac
}

# prove_sandboxed_engine_can_jit <signed-payload> <payload-identity> <app-entitlements> <inherit-entitlements> <workdir>
# Prints the verdict and returns 0 only on a JIT that ran inside the sandbox
# and a child that then exited 0.
prove_sandboxed_engine_can_jit() {
    local payload="$1" payload_identity="$2" entitlements="$3" inherit_entitlements="$4" workdir="$5"
    local probe="$workdir/JitProbe.app" out rc=0 key identity why resolved
    [ -x "$payload/runtime/bin/python3" ] || {
        echo "[ERROR] no interpreter at $payload/runtime/bin/python3" >&2
        return 1
    }
    [ -f "$entitlements" ] || {
        echo "[ERROR] no app entitlements at $entitlements" >&2
        return 1
    }
    [ -f "$inherit_entitlements" ] || {
        echo "[ERROR] no inherit entitlements at $inherit_entitlements" >&2
        return 1
    }
    resolved="$(sandboxed_jit_probe_identity "$payload_identity")"
    identity="${resolved%%$'\t'*}"
    why="${resolved#*$'\t'}"
    [ -n "$identity" ] || {
        echo "[ERROR] no identity resolved for the JIT probe (payload signed as '$payload_identity')" >&2
        return 1
    }
    echo "[OK]      JIT probe signs as '$identity': $why"
    rm -rf "$probe"
    mkdir -p "$probe/Contents/MacOS" "$probe/Contents/Resources"
    clang -O2 -o "$probe/Contents/MacOS/jitprobe" "$SANDBOXED_JIT_PROBE_SOURCE" || {
        echo "[ERROR] could not compile $SANDBOXED_JIT_PROBE_SOURCE" >&2
        return 1
    }
    /usr/bin/plutil -create xml1 "$probe/Contents/Info.plist"
    /usr/bin/plutil -insert CFBundleIdentifier -string com.opendj.jitprobe "$probe/Contents/Info.plist"
    /usr/bin/plutil -insert CFBundleExecutable -string jitprobe "$probe/Contents/Info.plist"
    /usr/bin/plutil -insert CFBundlePackageType -string APPL "$probe/Contents/Info.plist"
    # -c clones on APFS: seconds, not a 450 MB copy, and the signatures are
    # the payload's own until a re-sign below writes new ones into the clone.
    cp -Rc "$payload" "$probe/Contents/Resources/payload"
    if [ "$identity" != "$payload_identity" ]; then
        # The same file set, the same inherit entitlements, and the same
        # absence of --options runtime as the store signing, so the clone
        # carries the store payload's signing shape under an identity that
        # launches. Copy-on-write: the shipped payload is untouched.
        macho_files "$probe/Contents/Resources/payload" | xargs -0 -n 1 \
            codesign --force --sign "$identity" --entitlements "$inherit_entitlements" ||
            {
                echo "[ERROR] could not re-sign the payload clone as '$identity'" >&2
                return 1
            }
        echo "[OK]      re-signed the payload clone as '$identity' (the shipped payload keeps its '$payload_identity' signatures)"
    fi
    cat >"$probe/Contents/Resources/jitprobe.sh" <<'PROBE'
#!/bin/sh
set -eu
payload="$(cd "$(dirname "$0")" && pwd)/payload"
export PYTHONPATH="$payload/app:$payload/pylib" PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$payload/runtime/bin/python3" -c 'import os, numba; f = numba.njit(lambda n: n + 1); assert f(41) == 42; print("JIT_OK", numba.__version__, "sandboxed=" + str("APP_SANDBOX_CONTAINER_ID" in os.environ))'
PROBE
    # PlistBuddy, not plutil: plutil reads the dots in an entitlement key as a
    # keypath, so every one of these keys would silently be "absent".
    local probe_entitlements="$workdir/JitProbe.entitlements.plist"
    cp "$entitlements" "$probe_entitlements"
    for key in "${SANDBOXED_JIT_PROFILE_KEYS[@]}"; do
        if /usr/libexec/PlistBuddy -c "Print :$key" "$probe_entitlements" >/dev/null 2>&1; then
            /usr/libexec/PlistBuddy -c "Delete :$key" "$probe_entitlements" >/dev/null || {
                echo "[ERROR] could not drop $key from the probe entitlements" >&2
                return 1
            }
        fi
    done
    local sandbox_value
    sandbox_value=$(/usr/libexec/PlistBuddy -c "Print :com.apple.security.app-sandbox" "$probe_entitlements" 2>/dev/null) || sandbox_value=""
    [ "$sandbox_value" = true ] || {
        echo "[ERROR] $entitlements does not enable com.apple.security.app-sandbox, so the probe would not measure the App Store case" >&2
        return 1
    }
    codesign --force --options runtime --sign "$identity" \
        --entitlements "$probe_entitlements" "$probe" || {
        echo "[ERROR] could not sign the probe parent" >&2
        return 1
    }
    out=$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$probe/Contents/MacOS/jitprobe" 2>&1) || rc=$?
    rm -rf "$probe"
    sandboxed_jit_verdict "$out" "$rc"
}

# sandboxed_jit_verdict <probe-output> <probe-exit-code>
# The verdict alone, separated so it can be tested against real probe output
# without a signing identity. Returns 0 only for a JIT that ran sandboxed AND
# a child that exited 0 with the parent passing that through (exit 0);
# 2 when the probe never measured anything; 1 for every other outcome.
sandboxed_jit_verdict() {
    local out="$1" rc="$2"
    # Here-strings, not `printf | grep -q`: under pipefail an early grep exit
    # can SIGPIPE the producer and turn a match into a failed pipeline.
    if ! grep -qE '^(CHILD_|SPAWN_)' <<<"$out"; then
        echo "[ERROR] UNKNOWN: the sandboxed probe parent itself did not run (exit $rc), so the payload's JIT was never measured. Code signed for App Store distribution may refuse to launch locally; the probe signs itself with the identity named above (set MDT_MAS_PROBE_CERT to one that launches on this Mac). Output: $(tail -3 <<<"$out")" >&2
        return 2
    fi
    if ! grep -q '^JIT_OK .* sandboxed=True$' <<<"$out"; then
        echo "[ERROR] a numba JIT under the App Store signed payload did not run inside the sandbox (exit $rc). CHILD_SIGNAL 9 is the hardened runtime killing it: the interpreter was signed with the hardened runtime and no com.apple.security.cs.allow-unsigned-executable-memory of its own, and a parent cannot grant that key. sandboxed=False means the probe did not measure the App Store case. Output: $(tail -3 <<<"$out")" >&2
        return 1
    fi
    # JIT_OK is printed before the interpreter exits, so a runtime that
    # crashes on the way out still printed it. The child's exit is reported
    # by the parent (CHILD_EXIT n / CHILD_SIGNAL n) and passed through as the
    # parent's own exit code; both must read 0, or the smoke has recorded a
    # crashing runtime as release evidence.
    if ! grep -q '^CHILD_EXIT 0$' <<<"$out" || [ "$rc" != 0 ]; then
        echo "[ERROR] the JIT ran inside the sandbox, but the probe child did not exit 0 (probe exit $rc): a signed runtime that dies after its first JIT is not release evidence. Output: $(tail -3 <<<"$out")" >&2
        return 1
    fi
    echo "[OK]      App Store signed engine runs a numba JIT inside the sandbox ($(grep '^JIT_OK ' <<<"$out" | cut -d' ' -f2))"
}
