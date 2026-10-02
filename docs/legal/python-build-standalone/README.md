# python-build-standalone bundled native library licenses

Our CPython runtime (`runtime/`) comes from
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)'s
`install_only` release tarball. That tarball ships CPython's own PSF license
(`runtime/lib/python3*/LICENSE.txt`) but NOT license texts for the native C
libraries it statically links (OpenSSL, zlib, SQLite, etc.) -- their notices
are published separately, in the project's own repository, as one
`LICENSE.<name>.txt` file per library.

These files are mirrored here verbatim, fetched from
`astral-sh/python-build-standalone` at commit `63249f9a31f23542d5a58754aed0b93cc432e761`
(`main` branch, Fri 2 Oct 2026), scoped to the libraries this build's macOS
aarch64 `install_only` tarball actually links (`libX11`, `libXau`, `libxcb`
and `bdb` are omitted: they are Linux/X11-only and Berkeley DB respectively,
none of which this runtime bundles). All are permissive (BSD/MIT/zlib-style,
public domain, or Apache-2.0 for OpenSSL 3.x) -- none are copyleft.

`scripts/third_party_licenses.py`'s `supplement_components` reads every file
in this directory and attaches it to the "CPython (python-build-standalone)"
component, replacing a prior note that only said the notices were "published
with the release" without actually shipping them.

To refresh for a new CPython/python-build-standalone pin: re-download the
`LICENSE.*.txt` files relevant to the target platform from that repo at its
current `main` SHA, update the commit SHA and date above, and re-run
`just dmg` (verify_bundled_licenses will fail if this directory is empty).
