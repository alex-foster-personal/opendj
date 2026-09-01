# Mach-O discovery, shared by the two signing paths.
#
# WHY SHARED. scripts/sign_macos_developer_id.sh (Developer ID dmg) and
# scripts/ship_appstore.sh (Mac App Store pkg) sign the same staged CPython
# payload with different certificates and different entitlements, but they
# have to find exactly the same set of files. Two copies of this predicate
# drifted once already: the App Store copy narrowed to '*.so' and '*.dylib'
# and therefore never signed the bundled interpreter, which carries no
# extension. One definition, sourced by both, is what stops that recurring.
#
# Source it, do not execute it:
#   . "$(dirname "${BASH_SOURCE[0]}")/lib/macho.sh"
#
# NO `set -euo pipefail` IN THIS FILE, DELIBERATELY, and this is not an
# oversight for a later sweep to "fix". `set` in a sourced file mutates the
# CALLER's shell, so the options would silently leak into whatever sourced it
# and could not be handed back. Both callers already set -euo pipefail BEFORE
# the source line (sign_macos_developer_id.sh:52 before :57,
# ship_appstore.sh:36 before :44), so macho_files and macho_count already run
# under pipefail: the find|while and the tr|wc|tr below cannot report a
# downstream success over an upstream failure. A signing script that acquired
# these options by accident, rather than by declaring them, is the drift this
# shared file exists to prevent.

# Emit every Mach-O file under a directory, NUL separated.
#
# Candidates are narrowed by extension or executable bit first so `file` is
# not run over several thousand .py files, then confirmed with `file` so that
# static archives (.a, reported as "current ar archive") and shell scripts
# with the executable bit set are not handed to codesign, which would reject
# them. Detection is by CONTENT: an extension-only predicate misses
# runtime/bin/python3.N, the interpreter that actually runs the engine.
#
# -type f excludes symlinks deliberately. runtime/bin/python3 is a symlink to
# the versioned interpreter (stage_runtime copies the tree with symlinks=True),
# so signing through it would sign the same file twice under a name codesign
# does not record.
macho_files() {
    local root="$1" f
    find "$root" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 |
        while IFS= read -r -d '' f; do
            case "$(file -b "$f" 2>/dev/null)" in
            Mach-O*) printf '%s\0' "$f" ;;
            esac
        done
}

# How many files macho_files would emit. Counts NUL terminators rather than
# lines so a path containing a newline cannot inflate the number.
macho_count() {
    macho_files "$1" | tr -dc '\0' | wc -c | tr -d ' '
}
