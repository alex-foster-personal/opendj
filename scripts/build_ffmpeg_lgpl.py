#!/usr/bin/env python3
"""Build the pinned, decode-only, LGPL ffmpeg (with libsoxr) the payload ships.

Mini-PRD
--------
R1 ok   Fetch PINNED ffmpeg + libsoxr source tarballs, refuse any whose sha256
        differs from the pin, build libsoxr STATIC then ffmpeg STATIC for
        macOS arm64 into a cache dir OUTSIDE the repo.
R2 ok   Idempotent: a cached artifact that already passes the licence guard is
        reused, never rebuilt.
R3 ok   Licence guard (fail-fast, the ffmpeg twin of the mutagen guard): the
        built binary's ``-buildconf`` must carry ``--enable-libsoxr`` and
        neither ``--enable-gpl`` nor ``--enable-nonfree``, and ``-L`` must say
        "Lesser General Public License". Anything else raises.
R4 ok   The artifact dir carries LICENSE-ffmpeg (LGPL-2.1 text from the
        source tarball), LICENSE-soxr, and NOTICE-ffmpeg (versions, configure
        line, source URLs + sha256, written source offer).

Acceptance
----------
[if] a tarball's sha256 differs from its pin        [then] FfmpegBuildError, nothing built
[if] -buildconf contains --enable-gpl                 [then] FfmpegBuildError names it
[if] -buildconf lacks --enable-libsoxr                [then] FfmpegBuildError names it
[if] -L does not report the LGPL                      [then] FfmpegBuildError
[if] the cached artifact exists and passes the guard  [then] no compiler runs

Why a separate executable and not a library: the app runs ffmpeg as a
subprocess and never links it, so the LGPL obligations are the licence text,
the notice and the source offer that travel next to it, nothing more. The
configuration is minimal on purpose: it enables exactly what the analysis
lanes' command lines use (apps/analysis_waveform/decode.py,
apps/analysis/pcm_fingerprint.py, apps/loudness/scan.py,
apps/shared/ffmpeg.probe_duration_s), so a feature that needs more fails
loudly here rather than shipping a bigger surface by accident.

Usage::

    python3 scripts/build_ffmpeg_lgpl.py            # build or reuse, print artifact dir
    python3 scripts/build_ffmpeg_lgpl.py --check    # guard the cached artifact only
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ----- pins -------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    name: str
    version: str
    url: str
    sha256: str
    tarball: str
    licence_file: str


FFMPEG = Source(
    name="ffmpeg",
    version="9.0.2",
    url="https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
    sha256="8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e",
    tarball="ffmpeg-9.0.2.tar.xz",
    licence_file="COPYING.LGPLv2.1",
)
SOXR = Source(
    name="soxr",
    version="0.1.3",
    url="https://downloads.sourceforge.net/project/soxr/soxr-0.1.3-Source.tar.xz",
    sha256="b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889",
    tarball="soxr-0.1.3-Source.tar.xz",
    licence_file="LICENCE",
)

MACOS_MIN = "12.0"
CACHE_ROOT = Path(
    os.environ.get("MDT_FFMPEG_BUILD_CACHE", Path.home() / "Library/Caches/opendj-ffmpeg-build")
)

DECODERS = (
    "mp3", "mp3float", "aac", "aac_latm", "alac", "flac", "vorbis", "opus",
    "wavpack", "pcm_*",
)
DEMUXERS = ("mp3", "mov", "flac", "wav", "aiff", "ogg", "matroska", "aac", "caf", "w64", "wv")
PARSERS = ("mpegaudio", "aac", "aac_latm", "flac", "vorbis", "opus")
# Exactly the filters the lane command lines name, plus what ffmpeg inserts on
# its own (aformat/anull/aresample/format/null) and the sine source the
# fingerprint's resampler positive control runs (pcm_fingerprint.require_resampler).
FILTERS = (
    "aresample", "aformat", "asplit", "lowpass", "highpass", "bandpass", "equalizer",
    "amerge", "pan", "ebur128", "astats", "volume", "anull", "atrim", "format", "null", "trim",
    "sine",
)
# Configure names, not -f names: the -f s16le muxer is pcm_s16le_muxer.
MUXERS = ("pcm_s16le", "pcm_f32le", "wav", "null")
ENCODERS = ("pcm_s16le", "pcm_f32le")
PROTOCOLS = ("file", "pipe")


def configure_args(prefix: Path) -> list[str]:
    """The ffmpeg configure line. Never --enable-gpl, never --enable-nonfree."""
    flags = f"-mmacosx-version-min={MACOS_MIN} -I{prefix}/include"
    return [
        f"--prefix={prefix}",
        "--arch=arm64",
        "--cc=clang",
        "--enable-static",
        "--disable-shared",
        "--disable-autodetect",
        "--disable-everything",
        "--disable-doc",
        "--disable-debug",
        "--disable-network",
        "--disable-ffplay",
        "--enable-ffmpeg",
        "--enable-ffprobe",
        "--enable-libsoxr",
        "--enable-indev=lavfi",
        f"--enable-decoder={','.join(DECODERS)}",
        f"--enable-demuxer={','.join(DEMUXERS)}",
        f"--enable-parser={','.join(PARSERS)}",
        f"--enable-filter={','.join(FILTERS)}",
        f"--enable-muxer={','.join(MUXERS)}",
        f"--enable-encoder={','.join(ENCODERS)}",
        f"--enable-protocol={','.join(PROTOCOLS)}",
        f"--extra-cflags={flags}",
        f"--extra-ldflags=-mmacosx-version-min={MACOS_MIN} -L{prefix}/lib",
    ]


#: Stands in for the real prefix when hashing, so the name never depends on the cache path.
_PREFIX_PLACEHOLDER = Path("/opendj-ffmpeg-prefix")


def artifact_name(args: list[str]) -> str:
    """Cache name keyed on the whole configure line, so ANY flag change rebuilds.

    A hand-bumped revision number let an added filter (astats) ship without a
    recompile: the old artifact passed the licence guard and was reused.
    """
    digest = hashlib.sha256("\0".join(args).encode()).hexdigest()[:12]
    return f"ffmpeg-{FFMPEG.version}-soxr-{SOXR.version}-lgpl-{digest}"


ARTIFACT_NAME = artifact_name(configure_args(_PREFIX_PLACEHOLDER))


# ----- guard ------------------------------------------------------------------


class FfmpegBuildError(RuntimeError):
    """The pinned LGPL ffmpeg could not be produced or failed its guard."""


FORBIDDEN_FLAGS = ("--enable-gpl", "--enable-nonfree", "--enable-version3")
REQUIRED_FLAGS = ("--enable-libsoxr",)
LGPL_MARKER = "Lesser General Public License"


def buildconf_violations(buildconf: str) -> list[str]:
    """Why ``buildconf`` (``ffmpeg -buildconf`` stdout) is not shippable; [] when it is.

    Token match, not substring: ``--enable-gpl`` must not hide inside a longer
    flag name and a longer flag must not be mistaken for it.
    """
    tokens = set(buildconf.split())
    problems = [f"buildconf contains {flag}" for flag in FORBIDDEN_FLAGS if flag in tokens]
    problems += [f"buildconf lacks {flag}" for flag in REQUIRED_FLAGS if flag not in tokens]
    return problems


def licence_violations(licence_text: str) -> list[str]:
    """Why ``ffmpeg -L`` output is not LGPL; [] when it is.

    Whitespace is normalised first: ffmpeg wraps "Lesser General Public" and
    "License" onto two lines.
    """
    if LGPL_MARKER not in " ".join(licence_text.split()):
        return [f"ffmpeg -L does not report the {LGPL_MARKER}"]
    return []


def _run_text(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise FfmpegBuildError(f"{' '.join(argv)} exited {result.returncode}: {result.stderr[-500:]}")
    return result.stdout + result.stderr


def verify_lgpl_binary(binary: Path) -> dict[str, str]:
    """Run the licence guard against a real binary; raise on any violation."""
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        raise FfmpegBuildError(f"{binary} is not an executable file")
    buildconf = _run_text([str(binary), "-hide_banner", "-buildconf"])
    licence = _run_text([str(binary), "-hide_banner", "-L"])
    problems = buildconf_violations(buildconf) + licence_violations(licence)
    if problems:
        raise FfmpegBuildError(f"{binary} is not a shippable LGPL ffmpeg: {'; '.join(problems)}")
    _require_canonical_decode(binary)
    version = _run_text([str(binary), "-hide_banner", "-version"]).splitlines()[0]
    return {"binary": str(binary), "version": version}


#: The fingerprint's resampler positive control (pcm_fingerprint.require_resampler):
#: lavfi sine -> soxr at precision 28 -> raw s16le on stdout.
_SOXR_SMOKE = [
    "-nostdin", "-v", "error",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1:sample_rate=48000",
    "-ac", "1", "-af", "aresample=44100:resampler=soxr:precision=28",
    "-f", "s16le", "-acodec", "pcm_s16le", "-",
]


def _require_canonical_decode(binary: Path) -> None:
    """A guard-clean buildconf is not enough: the canonical chain must actually run."""
    result = subprocess.run([str(binary), *_SOXR_SMOKE], capture_output=True, check=False, timeout=30)
    if result.returncode != 0 or not result.stdout:
        raise FfmpegBuildError(
            f"{binary} cannot run the canonical soxr -> s16le chain: "
            f"{result.stderr.decode(errors='replace')[-500:]}"
        )


# ----- build ------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_verified(source: Source, src_dir: Path) -> Path:
    """The pinned tarball, downloaded if absent, refused if its sha256 differs."""
    src_dir.mkdir(parents=True, exist_ok=True)
    tarball = src_dir / source.tarball
    if not tarball.exists():
        partial = tarball.with_suffix(".part")
        with urllib.request.urlopen(source.url, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.rename(tarball)
    actual = _sha256(tarball)
    if actual != source.sha256:
        raise FfmpegBuildError(
            f"{tarball} sha256 {actual} != pinned {source.sha256}; refusing to build"
        )
    return tarball


def _extract(tarball: Path, into: Path) -> Path:
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    with tarfile.open(tarball) as archive:
        archive.extractall(into, filter="data")
    (top,) = [p for p in into.iterdir() if p.is_dir()]
    return top


def _sh(argv: list[str], cwd: Path, log: Path) -> None:
    with log.open("a") as out:
        out.write(f"\n$ {' '.join(argv)}\n")
        out.flush()
        result = subprocess.run(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        raise FfmpegBuildError(f"{argv[0]} failed in {cwd} (exit {result.returncode}); see {log}")


def _jobs() -> str:
    return str(max(1, (os.cpu_count() or 2) - 2))


def build_soxr(tarball: Path, work: Path, prefix: Path, log: Path) -> None:
    source = _extract(tarball, work / "soxr")
    build = source / "build"
    build.mkdir()
    _sh(
        [
            "cmake", "..",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MACOS_MIN}",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DBUILD_TESTS=OFF",
            "-DBUILD_EXAMPLES=OFF",
            "-DWITH_OPENMP=OFF",
            "-DWITH_LSR_BINDINGS=OFF",
        ],
        build, log,
    )
    _sh(["make", f"-j{_jobs()}"], build, log)
    _sh(["make", "install"], build, log)


def build_ffmpeg(tarball: Path, work: Path, prefix: Path, log: Path) -> list[str]:
    source = _extract(tarball, work / "ffmpeg")
    args = configure_args(prefix)
    _sh(["./configure", *args], source, log)
    _sh(["make", f"-j{_jobs()}"], source, log)
    _sh(["make", "install"], source, log)
    shutil.copy2(source / FFMPEG.licence_file, prefix / "LICENSE-ffmpeg")
    return args


def notice_text(args: list[str]) -> str:
    """NOTICE-ffmpeg: what was built, from what, and the written source offer."""
    return (
        "Open DJ bundles ffmpeg and ffprobe as separate executables, run as\n"
        "subprocesses and never linked into Open DJ.\n\n"
        f"ffmpeg {FFMPEG.version} is licensed under the GNU Lesser General Public\n"
        "License version 2.1 or later (see LICENSE-ffmpeg). It is statically built\n"
        f"with libsoxr {SOXR.version}, also LGPL-2.1 (see LICENSE-soxr). It was\n"
        "configured WITHOUT --enable-gpl and WITHOUT --enable-nonfree.\n\n"
        "Configure line:\n  ./configure " + " ".join(args) + "\n\n"
        "Corresponding source, unmodified:\n"
        f"  {FFMPEG.url}\n  sha256 {FFMPEG.sha256}\n"
        f"  {SOXR.url}\n  sha256 {SOXR.sha256}\n"
        "Build script: scripts/build_ffmpeg_lgpl.py in the Open DJ source tree.\n\n"
        "Written offer: for at least three years from the date you received this\n"
        "build, the Open DJ maintainers will provide the complete corresponding\n"
        "source code of the ffmpeg and libsoxr builds above, and the script used\n"
        "to build them, to anyone who asks, at no more than the cost of\n"
        "distribution. Request it through the Open DJ project's issue tracker.\n"
    )


def artifact_dir() -> Path:
    return CACHE_ROOT / ARTIFACT_NAME


ARTIFACT_FILES = ("bin/ffmpeg", "bin/ffprobe", "LICENSE-ffmpeg", "LICENSE-soxr", "NOTICE-ffmpeg")


def cached_artifact() -> Path | None:
    """The cached artifact if complete and guard-clean, else None."""
    prefix = artifact_dir()
    if not all((prefix / name).is_file() for name in ARTIFACT_FILES):
        return None
    verify_lgpl_binary(prefix / "bin/ffmpeg")
    return prefix


def build() -> Path:
    """Build (or reuse) the artifact and return its directory."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise FfmpegBuildError("the payload ffmpeg is built for macOS arm64, on macOS arm64")
    cached = cached_artifact()
    if cached is not None:
        return cached
    src_dir = CACHE_ROOT / "src"
    ffmpeg_tar = fetch_verified(FFMPEG, src_dir)
    soxr_tar = fetch_verified(SOXR, src_dir)
    # Built straight into the FINAL prefix: configure bakes --prefix into
    # -buildconf, so the notice and the binary must name the same path.
    # NOTICE-ffmpeg is written last, so a half-built prefix is never "cached".
    prefix = artifact_dir()
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True)
    work = CACHE_ROOT / "work"
    log = CACHE_ROOT / f"{ARTIFACT_NAME}.build.log"
    log.write_text("")
    build_soxr(soxr_tar, work, prefix, log)
    shutil.copy2(work / "soxr" / f"soxr-{SOXR.version}-Source" / SOXR.licence_file, prefix / "LICENSE-soxr")
    args = build_ffmpeg(ffmpeg_tar, work, prefix, log)
    verify_lgpl_binary(prefix / "bin/ffmpeg")
    (prefix / "NOTICE-ffmpeg").write_text(notice_text(args))
    shutil.rmtree(work)
    return prefix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="guard the cached artifact only")
    opts = parser.parse_args(argv)
    try:
        if opts.check:
            found = cached_artifact()
            if found is None:
                raise FfmpegBuildError(f"no complete artifact at {artifact_dir()}")
        else:
            found = build()
        print(verify_lgpl_binary(found / "bin/ffmpeg"))
        print(found)
    except FfmpegBuildError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
