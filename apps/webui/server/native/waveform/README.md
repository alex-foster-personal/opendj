# Rekordbox waveform native extension

`_rb_waveform_native` is the optional-at-runtime PyO3 acceleration embedded in
the normal `music-dj-tools` server wheel. Its entire Python API is the one
fused business operation `bands_payload`; decoding and the unchanged explicit
NumPy oracle/fallback remain in `apps.webui.server.rb_vendor`.

From the repository root (Python 3.11+ and Rust required):

```sh
uv run --no-project --with 'maturin>=1.8,<2' maturin develop --release \
  --manifest-path apps/webui/server/native/waveform/Cargo.toml
cargo test --manifest-path apps/webui/server/native/waveform/Cargo.toml
MDT_WAVEFORM_BACKEND=native MDT_REQUIRE_WAVEFORM_NATIVE=1 \
  uv run pytest -q tests/webui/test_waveform_native.py
make waveform-native-release-check
```

Backend policy is fail closed when `MDT_WAVEFORM_BACKEND=native`; `auto`
uses the extension when installed and otherwise uses the exact NumPy path,
while `python` always selects that oracle.

## Packaging contract

The root setuptools build compiles this crate into the normal platform wheel;
an ordinary `pip install music-dj-tools-*.whl` activates it automatically.
The release gate installs that root wheel in an isolated environment outside
the source tree, then checks the selected backend and real-fixture parity. The
separate Tauri launcher does not bundle the Python web server, so this makes no
claim about a desktop-app Python runtime that does not exist in the repository.

The crate enables PyO3 `abi3-py311`: one platform wheel supports CPython 3.11
and newer. The release gate rejects any wheel not tagged `cp311-abi3`, imports
it under the build interpreter, and CI separately imports that same wheel under
Python 3.14. This was chosen from built-wheel evidence rather than metadata
alone; the extension was smoke-tested with real NumPy arrays on both versions.
The core/native runtime accepts NumPy 1.26 and newer, so fresh Python 3.14
installs resolve a published NumPy 2 wheel. The `analysis` extra and development
requirements retain `numpy<2` separately for madmom compatibility.

The pure numeric bucket/rounding work releases the GIL. NumPy extraction and
Python-list construction retain it, as required by the C API. The benchmark
reports both sequential latency and overlapping same-process thread throughput,
matching FastAPI's worker-pool execution of this synchronous route.
