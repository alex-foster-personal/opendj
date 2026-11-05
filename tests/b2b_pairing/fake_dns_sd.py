"""A real stand-in process for ``/usr/bin/dns-sd`` (B2BNEAR-06 tests).

It prints dns-sd's own line formats for ``-B`` (browse), ``-L`` (resolve), ``-G v4``
(address) and ``-R`` (register), then sleeps like the real client, which never exits on its
own. Environment knobs (all optional):

- ``FAKE_DNSSD_START_DELAY`` seconds before the first byte (a loaded machine starting slowly).
- ``FAKE_DNSSD_INSTANCES`` comma-separated instance names ``-B`` reports.
- ``FAKE_DNSSD_HANG`` comma-separated instance names whose ``-L`` never answers.
- ``FAKE_DNSSD_RESOLVE_DELAY`` seconds every ``-L`` waits before answering.
- ``FAKE_DNSSD_IGNORE_TERM`` = 1 ignores SIGTERM, so only SIGKILL stops it.
- ``FAKE_DNSSD_PIDS`` a directory; each run writes ``<pid>`` there.
"""

from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path


def _names(env: str) -> list[str]:
    return [name for name in os.environ.get(env, "").split(",") if name]


def _stamp() -> str:
    return time.strftime("%H:%M:%S.000")


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> None:
    if os.environ.get("FAKE_DNSSD_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if pids := os.environ.get("FAKE_DNSSD_PIDS"):
        (Path(pids) / str(os.getpid())).write_text(" ".join(argv))
    time.sleep(float(os.environ.get("FAKE_DNSSD_START_DELAY", "0")))
    mode, args = argv[0], argv[1:]
    if mode == "-B":
        stype = args[0]
        _say(f"Browsing for {stype}.local")
        _say("DATE: ---Mon 12 Oct 2026---")
        _say(f"{_stamp()}  ...STARTING...")
        _say("Timestamp     A/R    Flags  if Domain               Service Type         Instance Name")
        for name in _names("FAKE_DNSSD_INSTANCES"):
            _say(f"{_stamp()}  Add        3   1 local.               {stype}.     {name}")
    elif mode == "-L":
        name, stype = args[0], args[1]
        _say(f"Lookup {name}.{stype}.local")
        _say("DATE: ---Mon 12 Oct 2026---")
        _say(f"{_stamp()}  ...STARTING...")
        if name not in _names("FAKE_DNSSD_HANG"):
            time.sleep(float(os.environ.get("FAKE_DNSSD_RESOLVE_DELAY", "0")))
            peer = hashlib.sha256(name.encode()).hexdigest()[:32]
            _say(f"{_stamp()}  {name}.{stype}.local. can be reached at {peer[:8]}.local.:4500 (interface 1)")
            _say(f" v=1 id={peer} nv=1 name={name.replace(' ', '%20')} acc=1")
    elif mode == "-G":
        host = args[1]
        _say("DATE: ---Mon 12 Oct 2026---")
        _say(f"{_stamp()}  ...STARTING...")
        _say("Timestamp     A/R  Flags         IF  Hostname                  Address                                      TTL")
        _say(f"{_stamp()}  Add  40000002       1  {host}                10.9.0.{int(host[:2], 16)}                                 120")
    time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1:])
