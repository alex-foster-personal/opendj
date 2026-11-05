"""Mutate each S1b booth guard (both ways where a boundary has two sides) and confirm its own tests go red.

    .venv/bin/python tests/b2b_booth/mutation_check.py [name filter ...]

Run from the repo root on a Linux dev host (it runs pytest and node per mutation). Each file is written
back from the exact bytes it read before the mutation. A GREEN line is a finding: a guard nothing tests.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LEASE, LOG, CORE = Path("apps/b2b_booth/lease.py"), Path("apps/b2b_booth/log.py"), Path("apps/b2b_booth/core.py")
BOOTH, LANES = Path("apps/b2b_booth/booth.py"), Path("apps/b2b_booth/lanes.py")
DOOR = Path("apps/b2b_pairing/b2b_door.py")
DGRAM_T, DGRAM_B = Path("apps/b2b_transport/datagram.py"), Path("apps/b2b_booth/dgram.py")
QUALITY, ROUTES = Path("apps/b2b_booth/quality.py"), Path("apps/webui/server/routes/b2b_booth.py")
STORE = Path("apps/webui/frontend/src/lib/rb/link/booth/booth.svelte.ts")


def _pytest(name: str) -> list[str]:
    return [".venv/bin/python", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", f"tests/b2b_booth/{name}"]


STORE_TEST = ["bash", "-c", "cd apps/webui/frontend && node --experimental-strip-types --test "
              "--test-timeout=120000 tests/unit/b2b-booth-store.test.mjs"]
FALLBACK = "elif now_us - ask.asked_us >= GRAB_FALLBACK_US:"
TOO_LATE = 'if now_us - ask.asked_us >= GRAB_FALLBACK_US:\n            return Decision([], "too_late")'
LAPSED = "return until is None or now_us > until"
LOCK = "return self.onair if self.settings.lock_half_on_turn[self.onair] else None"
RENEW = "out.extend(core.renew(now) if self.page_connected else _heartbeat(core, now))"
STALE_CTL = 'kept["cseq"] >= message["cseq"]'
NOT_HOLDER = 'if self.state.holders[message["unit"]] != message["side"]:\n            self.counters.ctl_not_holder += 1\n            return'
COALESCE = ("                if key in self._latest:\n                    self.replaced += 1\n"
            "                    del self._latest[key]\n                self._latest[key] = message")
DOOR_AUTH = ('if expected is None or not hmac.compare_digest((authorization or "").encode(), expected):\n'
             "        return 401, _NOT_PAIRED, _JSON\n    booth")


REPLAY = 'if not self._session.window.would_accept(counter):\n            return self._drop("replayed")'
REPUT = ('            self._ctl.put([(f"ctl:{key}", {"kind": "ctl", "epoch": core.epoch, **control})\n'
         '                           for key, control in core.controls.items() if control["side"] == core.side])')
STALE = "elif now - self.last_pong > DGRAM_STALE_S:"
NEWEST = "if kept is not None and counter <= kept:"
EVICT = "while self._bytes > self.max_bytes or len(self._posts) > self.max_posts:"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    cmd: list[str]


MUTATIONS = [
    Mutation("fallback late (>= -> >)", LEASE, FALLBACK, FALLBACK.replace(">=", ">"), _pytest("test_booth_lease.py")),
    Mutation("fallback early (0.9 s)", LEASE, FALLBACK, FALLBACK.replace("US:", "US - 100_000:"), _pytest("test_booth_lease.py")),
    Mutation("refuse at 1 s accepted", LEASE, TOO_LATE, TOO_LATE.replace(">=", ">"), _pytest("test_booth_lease.py")),
    Mutation("lease lapses at its end (> -> >=)", LEASE, LAPSED, LAPSED.replace("> until", ">= until"), _pytest("test_booth_lease.py")),
    Mutation("lease lapses 1 us late", LEASE, LAPSED, LAPSED + " + 1", _pytest("test_booth_lease.py")),
    Mutation("turn lock always on", LEASE, LOCK, "return self.onair", _pytest("test_booth_lease.py")),
    Mutation("turn lock never on", LEASE, LOCK, "return None", _pytest("test_booth_lease.py")),
    Mutation("lock hand-back off", LEASE, '            self.onair = op["side"]\n            self._enforce_lock()',
             '            self.onair = op["side"]', _pytest("test_booth_lease.py")),
    Mutation("partner may set my lock", LEASE, 'if name == "setting" and "lock_half_on_turn" in op and op["side"] != by:',
             "if False:", _pytest("test_booth_lease.py")),
    Mutation("stale epoch applied", LOG, "if epoch is not None and epoch == self.epoch:", "if True:", _pytest("test_booth_log.py")),
    Mutation("gap asks every commit", LOG, "if self._asked_from != start:", "if True:", _pytest("test_booth_log.py")),
    Mutation("gap never asks", LOG, "if self._asked_from != start:", "if False:", _pytest("test_booth_log.py")),
    Mutation("reducer applies out of order", LEASE, 'if committed["seq"] != self.seq + 1:', "if False:", _pytest("test_booth_log.py")),
    Mutation("older control kept", CORE, STALE_CTL, 'kept["cseq"] > message["cseq"] + 1', _pytest("test_booth_log.py")),
    Mutation("every control from that side dropped", CORE, STALE_CTL, "True", _pytest("test_booth_log.py")),
    Mutation("non-holder control applied", CORE, NOT_HOLDER, "if False:\n            return", _pytest("test_booth_log.py")),
    Mutation("clock converted the wrong way", LOG, "return round(peer_us - offset)", "return round(peer_us + offset)",
             _pytest("test_booth_log.py")),
    Mutation("least RTT ignored (newest wins)", LOG, "return None if not self._samples else min(self._samples)[1]",
             "return None if not self._samples else self._samples[-1][1]", _pytest("test_booth_log.py")),
    Mutation("negative time committed", CORE, "if at is None or at < 0:", "if at is None:", _pytest("test_booth_log.py")),
    Mutation("retried proposal committed twice", CORE, "if pid <= self._last_peer_pid:", "if False:", _pytest("test_booth_log.py")),
    Mutation("renew with no page", BOOTH, RENEW, "out.extend(core.renew(now))", _pytest("test_booth_engine.py")),
    Mutation("never renew", BOOTH, RENEW, "out.extend(_heartbeat(core, now))", _pytest("test_booth_engine.py")),
    Mutation("stuck ops lane reads live", BOOTH, "owed = self._ops is not None and self._ops.stuck_s() > CONTACT_STALE_S",
             "owed = False", _pytest("test_booth_engine.py")),
    Mutation("ctl retry overwrites newer", LANES, "self._latest.setdefault(key, message)  # a newer value put meanwhile wins",
             "self._latest[key] = message", _pytest("test_booth_lanes.py")),
    Mutation("ctl never coalesces (all in flight)", LANES, COALESCE, '                self._latest[f"{key}#{len(self._latest)}"] = message',
             _pytest("test_booth_lanes.py")),
    Mutation("ops retry appended at the tail", LANES, "self._queue.extendleft(m for _, m in reversed(batch))",
             "self._queue.extend(m for _, m in batch)", _pytest("test_booth_lanes.py")),
    Mutation("door serves booth without token", DOOR, DOOR_AUTH, DOOR_AUTH.replace(DOOR_AUTH.split(":\n", 1)[0], "if False"),
             _pytest("test_booth_routes.py")),
    # B2BBOOTH-97..99 (S1c): the datagram lane, the quality ring, the snapshot's decks.
    Mutation("datagram replay accepted", DGRAM_T, REPLAY, "pass", _pytest("test_booth_datagram.py")),
    Mutation("datagram of another epoch not named", DGRAM_T, "if epoch != bytes.fromhex(self.epoch):", "if False:",
             _pytest("test_booth_datagram.py")),
    Mutation("datagram key ignores direction", DGRAM_T, "info=LABEL + direction.encode()", "info=LABEL",
             _pytest("test_booth_datagram.py")),
    Mutation("newest-wins admits an equal counter", DGRAM_B, NEWEST, NEWEST.replace("<=", "<"), _pytest("test_booth_datagram.py")),
    Mutation("newest-wins drops the next counter", DGRAM_B, NEWEST, NEWEST.replace("<= kept", "<= kept + 1"),
             _pytest("test_booth_datagram.py")),
    Mutation("datagram lane before any pong", DGRAM_B, "elif self.last_pong is None:", "elif False:", _pytest("test_booth_datagram.py")),
    Mutation("fallback at the stale limit (> -> >=)", DGRAM_B, STALE, STALE.replace(">", ">="), _pytest("test_booth_datagram.py")),
    Mutation("fallback never (stale x100)", DGRAM_B, STALE, STALE.replace("DGRAM_STALE_S", "DGRAM_STALE_S * 100"),
             _pytest("test_booth_datagram.py")),
    Mutation("switch to door forgets the newest controls", BOOTH, REPUT, "            pass", _pytest("test_booth_datagram.py")),
    Mutation("quality merge keyed on the frame", QUALITY, "seen.setdefault(json.dumps(record, sort_keys=True), record)",
             'seen.setdefault(str(record.get(order)), record)', _pytest("test_booth_quality.py")),
    Mutation("quality ring never evicts", QUALITY, EVICT, "while False:", _pytest("test_booth_quality.py")),
    Mutation("quality post from any host", ROUTES, "return ipaddress.ip_address(host).is_loopback", "return True",
             _pytest("test_booth_quality.py")),
    Mutation("snapshot without resolution", BOOTH, '"resolution": self._resolutions.get(d["deck"]) if d["track"] is not None else None',
             '"resolution": None', _pytest("test_booth_snapshot.py")),
    Mutation("snapshot resolution on an empty deck", BOOTH, 'if d["track"] is not None else None', "",
             _pytest("test_booth_snapshot.py")),
    Mutation("store re-delivers ops on reconnect", STORE, "if (committed.seq <= this.view.seq) continue;", "", STORE_TEST),
    Mutation("store sends every control", STORE, "if (this.inFlight.has(key)) {", "if (false) {", STORE_TEST),
]


def run(only: list[str]) -> int:
    results: list[tuple[str, str]] = []
    for m in MUTATIONS:
        if only and not any(o in m.name for o in only):
            continue
        original = m.path.read_text()
        if original.count(m.old) != 1:
            results.append((m.name, f"NOT APPLIED ({original.count(m.old)} matches)"))
            continue
        m.path.write_text(original.replace(m.old, m.new))
        try:
            done = subprocess.run(m.cmd, capture_output=True, text=True, timeout=600, check=False)
            results.append((m.name, "RED (caught)" if done.returncode != 0 else "GREEN (SURVIVED)"))
        finally:
            m.path.write_text(original)
    for name, verdict in results:
        print(f"{verdict:18} {name}")
    caught = sum(v.startswith("RED") for _, v in results)
    print(f"[{'OK' if caught == len(results) else 'WARN'}] {caught}/{len(results)} mutations caught")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
