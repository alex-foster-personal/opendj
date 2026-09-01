# Mach-O Test Fixtures

Four **real, loadable Mach-O binaries** emitted by Apple's linker, ~17KB each,
committed so the payload's signing and linked-library gates can be tested
without building a 174MB payload.

They are captured artifacts, not constructed bytes. An earlier revision of
these tests built 32-byte Mach-O headers by hand, which `AGENTS.md` forbids
(no stubs or fabricated application state) and which no production path can
load: `codesign` and `otool` both refuse a header with no load commands.

## Contents

Each file stands for one shape a staged payload carries:

| file | filetype | name | stands for |
| --- | --- | --- | --- |
| `opendj-probe-executable` | `MH_EXECUTE` | no extension | `runtime/bin/python3.N`, the bundled interpreter |
| `libopendj-probe.dylib` | `MH_DYLIB` | `.dylib` | `runtime/lib/libpython3.N.dylib` |
| `opendj-probe-bundle.so` | `MH_BUNDLE` | `.so` | a CPython extension module |
| `opendj-probe-dylib-named.so` | `MH_DYLIB` | `.so` | **suffix and filetype disagree** |

The last one is the case the fixtures exist for. `pydantic_core`, `rpds`,
`watchfiles` and `rbox` all ship a `.so` that is really a dylib, and
protobuf's `_message.abi3.so` is one whose install name is a `bazel-out`
build path. Classifying it by suffix counts that id line as a dependency and
reports a violation for a library nothing loads by that name.

## Deployment target

All four are built with `-mmacosx-version-min=11.0`, matching the app's own
`bundle.macOS.minimumSystemVersion`. The first capture defaulted to the host
SDK and encoded `minos 26.0`, which dyld on the `macos-14` packaging runner
could not have loaded, making "these are loadable Mach-O files" false in the
exact environment they stand for.

A test asserts `minos <= minimumSystemVersion` read from `tauri.conf.json`,
so it is an invariant rather than a number someone has to remember. Raising
or lowering the app's floor cannot silently invalidate the fixtures.

## Use them through `tests/scripts/macho_fixtures.py`

`verified(name)` checks the manifest `version` first and the file's sha256
second, and fails loudly on either. `hydrate(name, dest)` copies a verified
fixture into a disposable path the caller owns.

Never read these paths directly, and never mutate them. A test that needs a
mutated fixture calls `disposable_fixture_dir(dest)` for a writable copy of
the whole set and passes it to the real verifier through its `fixture_dir`
parameter. That helper runs `verify_all` on the SOURCE before it copies, so a
whole-set caller -- which names no fixture, and therefore cannot be covered by
a per-file check at the call site -- still gets provenance established on every
entry the manifest lists. That is a production-API argument, deliberately not a module
global to reassign: swapping globals is monkeypatching, which `AGENTS.md`
forbids, and it would mean the test never exercises the code that ships.

Three tests enforce this rather than relying on the prose: a tampered fixture
is refused, an unrecognized `manifest.version` fails closed before any hash is
compared, and a drifted SOURCE is refused before it can be copied -- that last
one plants its drift in a fixture no assertion names, so a per-file check would
not catch it.

## The `file_b` field is provenance, not an assertion target

`manifest.json` records what macOS `file(1)` printed at capture time. Do not
assert on it: GNU `file` words the same binary differently
(`ubuntu-latest` prints `Mach-O 64-bit arm64 executable, flags:<...>` where
macOS prints `Mach-O 64-bit executable arm64`), and it describes a bundle
with a phrase that *contains* the one it uses for a shared library.

That is why filetype is read from `otool -hv`, which prints a token
(`EXECUTE` / `DYLIB` / `BUNDLE`), and `file(1)` is used only for the portable
is-it-Mach-O question. Tests assert the invariant that the description
contains `Mach-O`, never the exact string.

## Regenerate

Only if a fixture is genuinely lost. Regenerating one to make a test pass is
forbidden by `AGENTS.md`; the sha256 in `manifest.json` must change in the
same commit, with the reason in the PR body.

```bash
# The deployment target is NOT optional and must not be hardcoded here: the
# first capture omitted it, cc defaulted to the host SDK, and the fixtures
# encoded minos 26.0 while the app declares 11.0 and the packaging lane runs
# macos-14. Read it from the app so this block cannot rot.
# Both paths below are anchored to the repo root rather than to $PWD. The
# config read only resolves from the root, while a bare -o only lands in the
# fixture directory from HERE, so any single working directory made one of the
# two silently wrong: run from the root and the rebuilt binaries went to the
# root, leaving the canonical fixtures untouched while the runbook appeared to
# succeed.
ROOT=$(git rev-parse --show-toplevel)
OUT="$ROOT/tests/fixtures/macho"
MIN=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['bundle']['macOS']['minimumSystemVersion'])" "$ROOT/apps/desktop/src-tauri/tauri.conf.json")
printf 'int main(void){return 0;}\n' > /tmp/m.c
printf 'int mdt_probe(void){return 7;}\n' > /tmp/l.c
cc -arch arm64 -mmacosx-version-min="$MIN" -o "$OUT/opendj-probe-executable" /tmp/m.c
cc -arch arm64 -mmacosx-version-min="$MIN" -dynamiclib -install_name @rpath/libopendj-probe.dylib -o "$OUT/libopendj-probe.dylib" /tmp/l.c
cc -arch arm64 -mmacosx-version-min="$MIN" -bundle -undefined dynamic_lookup -o "$OUT/opendj-probe-bundle.so" /tmp/l.c
cc -arch arm64 -mmacosx-version-min="$MIN" -dynamiclib -install_name @rpath/opendj-probe-dylib-named.so -o "$OUT/opendj-probe-dylib-named.so" /tmp/l.c

# Confirm rather than assume, then update manifest.json's sha256, bytes,
# minos, filetype and file_b for each rebuilt file.
for f in opendj-probe-executable libopendj-probe.dylib opendj-probe-bundle.so opendj-probe-dylib-named.so; do
  otool -l "$OUT/$f" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print FILENAME, $2; exit}' FILENAME="$f"
done
```

The test suite is the real check: `test_the_fixtures_load_on_the_oldest_macos_the_app_supports` fails if any `minos` exceeds the app's declared minimum, and
`test_the_manifest_filetypes_match_the_binaries` fails if a recorded filetype
does not match `otool -hv`.

They are arm64-only. `file(1)` reads the header on any host, so discovery is
covered everywhere; anything needing `otool` is gated on `which otool`.
