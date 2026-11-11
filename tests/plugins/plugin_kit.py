"""Build REAL plugins for tests: files on disk, a zip, a signed index.

Nothing here is a stand-in for the code under test. A test plugin is a real
first-party module with a real FastAPI router and register function; the
index is signed with a real Ed25519 key generated per test; and
:meth:`LocalStore.fetch` reads every byte through the production
:func:`apps.plugins.catalog.http_fetch`, from a real HTTPS server on
127.0.0.1. The one substitution is name resolution: the store's public origin
(``https://store.example.test``) is sent to that local server, and "offline"
is a local port nothing listens on.
"""

from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import io
import ipaddress
import json
import socket
import ssl
import struct
import sys
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

import apps
from apps.plugins.catalog import http_fetch
from apps.plugins.signing import public_key_b64, sign

KEY_ID = "test-store-key"


def symlink_or_skip(link: Path, target: Path, target_is_directory: bool = False) -> None:
    """Create ``link`` -> ``target``; skip on Windows when this account may not create symlinks.

    Windows needs Developer Mode or SeCreateSymbolicLinkPrivilege for a symlink;
    without either, the test cannot set up the link it is about, so it skips
    with that reason. Anywhere else a failure is a real error and raises.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if sys.platform != "win32":
            raise
        pytest.skip(
            f"this Windows account cannot create symlinks (Developer Mode or "
            f"SeCreateSymbolicLinkPrivilege needed): {exc}"
        )


@pytest.fixture(autouse=True)
def restore_apps_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader appends plugin dirs to ``apps.__path__``; undo it per test.

    Also points the real HTTPS client at the local store server: it trusts that
    server's certificate and reaches 127.0.0.1 directly, not through a proxy.
    Autouse in every module that imports it. Not a conftest, because the
    engine-level plugin tests live in tests/engine_core and share it.
    """
    monkeypatch.setenv("SSL_CERT_FILE", str(_store_server().cert))
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    before = list(apps.__path__)
    yield
    apps.__path__[:] = before
    for name in [n for n in sys.modules if n.startswith("apps.plugtest_")]:
        del sys.modules[name]


BASE = "https://store.example.test/index"

_ROUTER_SRC = """\
from fastapi import APIRouter

router = APIRouter(prefix="{prefix}")


@router.get("{route}")
def ping() -> dict[str, str]:
    return {{"pong": "{plugin_id}"}}
{extra}"""

_EXTRA_ROUTE_SRC = """

@router.{method}("{path}")
def extra_{n}() -> dict[str, str]:
    return {{"extra": "{n}"}}
"""

_PLUGIN_SRC = """\
STARTED = []


def _start(host):
    if {startup_fails}:
        raise RuntimeError("could not open its socket")
    STARTED.append(host.plugin_id)


{register_def} register(host):
    from apps.{package}.routes import router

    host.include_router(router)
    host.on_startup(lambda: _start(host))
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class BuiltPlugin:
    manifest: dict[str, Any]
    files: dict[str, bytes]
    #: The signed index's listing for this plugin, and the media it pins by path.
    listing: dict[str, Any] = field(default_factory=dict)
    media: dict[str, bytes] = field(default_factory=dict)

    @property
    def manifest_bytes(self) -> bytes:
        return (json.dumps(self.manifest, indent=2) + "\n").encode()

    def write_dir(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "opendj-plugin.json").write_bytes(self.manifest_bytes)
        for rel, data in self.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return root

    def archive(self, extra: dict[str, bytes] | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("opendj-plugin.json", self.manifest_bytes)
            for rel, data in {**self.files, **(extra or {})}.items():
                zf.writestr(rel, data)
        return buf.getvalue()


def png_bytes(width: int = 4, height: int = 3, shade: int = 40) -> bytes:
    """A real, decodable PNG (8-bit gray), so media checks see a true image."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes([shade]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def listing_for(
    plugin_id: str,
    *,
    status: str = "stable",
    shots: int = 1,
    capability_kind: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A complete problem-first listing plus the screenshots it pins."""
    media: dict[str, bytes] = {}
    screenshots = []
    for n in range(shots):
        data = png_bytes(shade=40 + n)
        path = f"media/{plugin_id}/{sha(data)}.png"
        media[path] = data
        screenshots.append(
            {
                "path": path,
                "sha256": sha(data),
                "size": len(data),
                "width": 4,
                "height": 3,
                "alt": f"Screenshot {n + 1}",
                "theme": "dark" if n % 2 == 0 else "light",
            }
        )
    listing: dict[str, Any] = {
        "tagline": "Does one useful thing.",
        "problem": "Without it you cannot do the useful thing.",
        "you_can": ["Do the useful thing"],
        "how_it_works": "It does it.",
        "limits": [],
        "status": status,
        "screenshots": screenshots,
    }
    if capability_kind is not None:
        listing["capability_kind"] = capability_kind
    return listing, media


def module_plugin(
    plugin_id: str = "opendj.test-mod",
    *,
    version: str = "1.0.0",
    package: str = "plugtest_mod",
    prefix: str = "/plugtest",
    route_prefix: str | None = None,
    channel: str = "stable",
    tier: int = 0,
    notice: str | None = None,
    requires: list[str] | None = None,
    ui: bool = True,
    route: str = "/ping",
    min_app_version: str | None = None,
    startup_fails: bool = False,
    async_register: bool = False,
    extra_routes: list[tuple[str, str]] | None = None,
    sys_exit: str | None = None,
    starlette_router: bool = False,
    late_raw_route: bool = False,
    ui_entry: str = "ui/index.js",
    permissions: list[str] | None = None,
    bad_schema: bool = False,
) -> BuiltPlugin:
    """``sys_exit``: call ``sys.exit`` at "import", in "register" or in its "startup" hook.

    ``starlette_router``: hand the host a plain Starlette ``Router`` holding the same routes.
    ``late_raw_route``: add a raw Starlette route to the router after handing it to the host.
    ``bad_schema``: add a route whose response model has no JSON schema, so OpenAPI generation raises.
    """
    plugin_src = _PLUGIN_SRC.format(
        package=package, startup_fails=startup_fails, register_def="async def" if async_register else "def"
    )
    if sys_exit == "import":
        plugin_src = "raise SystemExit(3)\n" + plugin_src
    elif sys_exit == "register":
        plugin_src = plugin_src.replace("    from apps.", "    raise SystemExit(3)\n    from apps.", 1)
    elif sys_exit == "startup":
        plugin_src = plugin_src.replace("STARTED.append(host.plugin_id)", "raise SystemExit(3)", 1)
    if late_raw_route:
        plugin_src = plugin_src.replace(
            "    host.include_router(router)\n",
            (
                "    host.include_router(router)\n"
                "    from starlette.responses import PlainTextResponse\n"
                f"    router.add_route('{prefix}/late', lambda _request: PlainTextResponse('x'))\n"
            ),
            1,
        )
    files = {
        f"python/apps/{package}/__init__.py": b"",
        f"python/apps/{package}/plugin.py": plugin_src.encode(),
        f"python/apps/{package}/routes.py": _ROUTER_SRC.format(
            prefix=route_prefix or prefix,
            plugin_id=plugin_id,
            route=route,
            extra="".join(
                _EXTRA_ROUTE_SRC.format(method=m, path=pth, n=i) for i, (m, pth) in enumerate(extra_routes or [])
            ),
        ).encode(),
    }
    if bad_schema:
        files[f"python/apps/{package}/routes.py"] += (
            "\nfrom pydantic import BaseModel, ConfigDict\n\n\nclass _Opaque:\n    pass\n\n\n"
            "class _Bad(BaseModel):\n    model_config = ConfigDict(arbitrary_types_allowed=True)\n    x: _Opaque\n\n\n"
            f"@router.get('{route_prefix or prefix}/bad', response_model=_Bad)\n"
            "def _bad() -> None:\n    return None\n"
        ).encode()
    if starlette_router:
        files[f"python/apps/{package}/routes.py"] += (
            b"\nfrom starlette.routing import Router as _Router\n\nrouter = _Router(routes=list(router.routes))\n"
        )
    if ui:
        files[ui_entry] = b"export function mount(el) { el.textContent = 'hi'; }\n"
    manifest: dict[str, Any] = {
        "schema": 1,
        "id": plugin_id,
        "name": "Test Module",
        "version": version,
        "summary": "A real module used by the plugin store tests.",
        "publisher": "Open DJ",
        "trust": "first-party",
        "kind": "module",
        "tier": tier,
        "channel": channel,
        "license": "Apache-2.0",
        "permissions": permissions if permissions is not None else ["net:lan"],
        "python": {
            "packages": [package],
            "register": f"apps.{package}.plugin:register",
            "requires": requires or [],
            "api_prefixes": [prefix],
        },
        "files": {rel: sha(data) for rel, data in files.items()},
    }
    if ui:
        manifest["ui"] = {"entry": ui_entry, "slots": ["topbar.end"]}
    if notice is not None:
        manifest["notice"] = notice
    if min_app_version is not None:
        manifest["min_app_version"] = min_app_version
    listing, media = listing_for(plugin_id)
    return BuiltPlugin(manifest, files, listing, media)


def provider_plugin(
    plugin_id: str = "opendj.test-provider",
    *,
    capability_kind: str = "API",
    credential_env: str = "OPENDJ_TEST_PROVIDER_KEY",
    version: str = "1.0.0",
    enabled_permissions: list[str] | None = None,
) -> BuiltPlugin:
    """A first-party music-intelligence provider fixture for store/API tests."""
    built = module_plugin(
        plugin_id,
        version=version,
        package="plugtest_provider",
        prefix="/plugtest-provider",
        permissions=enabled_permissions if enabled_permissions is not None else ["net:internet"],
    )
    built.manifest["provider"] = {
        "capability_kind": capability_kind,
        "credential_env": credential_env,
        "terms_url": "https://example.test/terms",
        "provenance_fields": ["provider", "model_version", "observed_at"],
    }
    if capability_kind in ("public browse", "unavailable"):
        built.manifest["provider"].pop("credential_env", None)
    built.listing, built.media = listing_for(plugin_id, capability_kind=capability_kind)
    return built


def data_plugin(plugin_id: str = "opendj.test-data") -> BuiltPlugin:
    """A data-only plugin: files, no code."""
    files = {"(private path)": b'{"controls": []}\n'}
    manifest: dict[str, Any] = {
        "schema": 1,
        "id": plugin_id,
        "name": "Test Data",
        "version": "1.0.0",
        "summary": "A data plugin used by the plugin store tests.",
        "publisher": "Open DJ",
        "trust": "first-party",
        "kind": "data",
        "tier": 0,
        "channel": "stable",
        "license": "Apache-2.0",
        "permissions": [],
        "files": {rel: sha(data) for rel, data in files.items()},
    }
    listing, media = listing_for(plugin_id)
    return BuiltPlugin(manifest, files, listing, media)


def self_signed(directory: Path) -> tuple[Path, Path]:
    """A certificate and key for 127.0.0.1, written to ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(hours=12))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


#: The origin signed indexes in these tests name; :class:`LocalStore` serves it.
PUBLIC_ORIGIN = "https://store.example.test"


@dataclass
class _StoreServer:
    """One HTTPS server for the test process, serving whichever store is fetching."""

    origin: str
    dead_origin: str
    cert: Path
    active: LocalStore | None = None


@functools.cache
def _store_server() -> _StoreServer:
    """Started on first use, from a test's autouse fixture, and kept for the process."""
    cert, key = self_signed(Path(tempfile.mkdtemp(prefix="plugin-store-tls-")))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    holder: dict[str, _StoreServer] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # keep test output clean
            return

        def do_GET(self) -> None:
            store = holder["server"].active
            body = None if store is None else store.blobs.get(f"{PUBLIC_ORIGIN}{self.path}")
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as probe:  # a port that was free a moment ago and has no listener
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    started = _StoreServer(
        origin=f"https://127.0.0.1:{server.server_address[1]}",
        dead_origin=f"https://127.0.0.1:{dead_port}",
        cert=cert,
    )
    holder["server"] = started
    return started


#: ``publish(generated_at=NEXT_STAMP)``: sign one second after the previous publication.
NEXT_STAMP = "next"


@dataclass
class LocalStore:
    """A signed store whose bytes are fetched over real HTTPS. ``fetch`` matches the Fetcher protocol."""

    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    blobs: dict[str, bytes] = field(default_factory=dict)
    offline: bool = False
    #: Where the signed index says archives and media live. The default is served
    #: by the local HTTPS server; (private path)
    #: runs its own server (redirects, endless bodies) and points this at it.
    origin: str = PUBLIC_ORIGIN
    #: Publications so far; each one is signed a second after the last, as a
    #: real store's are, so a refresh never reads it as a replay.
    published: int = 0

    def _stamp(self, generated_at: str | None) -> str | None:
        self.published += 1
        if generated_at != NEXT_STAMP:
            return generated_at
        return (datetime(2026, 10, 13, tzinfo=UTC) + timedelta(seconds=self.published)).isoformat()

    @property
    def base(self) -> str:
        return f"{self.origin}/index"

    @property
    def trusted(self) -> dict[str, str]:
        return {KEY_ID: public_key_b64(self.key)}

    def fetch(self, url: str, *, max_bytes: int = 1 << 30) -> bytes:
        """The production fetcher, with this store's public origin resolved to the local server."""
        server = _store_server()
        if url.startswith(PUBLIC_ORIGIN + "/"):
            url = (server.dead_origin if self.offline else server.origin) + url[len(PUBLIC_ORIGIN) :]
        server.active = self
        return http_fetch(url, max_bytes=max_bytes)

    def publish(
        self,
        channel: str,
        plugins: list[BuiltPlugin],
        blocklist: list[dict] | None = None,
        *,
        sign_with: Ed25519PrivateKey | None = None,
        generated_at: str | None = NEXT_STAMP,
    ) -> bytes:
        rows = []
        for plugin in plugins:
            archive = plugin.archive()
            url = f"{self.origin}/archives/{plugin.manifest['id']}-{plugin.manifest['version']}.zip"
            self.blobs[url] = archive
            for path, data in plugin.media.items():
                self.blobs[f"{self.base}/{path}"] = data
            rows.append(
                {
                    "manifest": plugin.manifest,
                    "archive_url": url,
                    "archive_sha256": sha(archive),
                    "archive_size": len(archive),
                    "listing": plugin.listing,
                }
            )
        index = json.dumps(
            {
                "schema": 1,
                "channel": channel,
                "generated_at": self._stamp(generated_at),
                "plugins": rows,
                "blocklist": blocklist or [],
            }
        ).encode()
        self.blobs[f"{self.base}/{channel}.json"] = index
        self.blobs[f"{self.base}/{channel}.json.sig"] = sign(index, sign_with or self.key, KEY_ID)
        return index
