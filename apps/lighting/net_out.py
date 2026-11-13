"""UDP output loop that sends universes over sACN or Art-Net (L24, issue #9878).

Each tick reads every configured universe from a frame source, asks one ``SendPolicy`` per
universe how many packets are due and sends them. sACN goes multicast (loopback on) or unicast
to the configured address; on stop it sends 3 Stream_Terminated packets per universe. Art-Net
polls ``artnet_poll_address``, keeps a ``SubscriberTable`` from the replies and unicasts ArtDmx
to each subscriber, plus one ArtSync to each when more than one universe changed in a tick.

Ticks run on a scheduled clock (``start + n * period``): a late wakeup sends the due tick
instead of dropping it, so the frame count over a run does not depend on machine load.
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from collections.abc import Callable

from apps.lighting import artnet, artnet_poll, sacn
from apps.lighting.settings import SACN_MULTICAST, NetOutSettings
from apps.lighting.universe import MAX_FRAMES_PER_SECOND, SendPolicy

FrameSource = Callable[[int, int], bytes]
"""Called as ``source(universe, tick)``; returns that universe's slots for the tick."""

TERMINATE_PACKETS = 3
# A hair over 1/44 s so float rounding never makes SendPolicy see a tick as too early.
TICK_PERIOD_S = (1.0 / MAX_FRAMES_PER_SECOND) * (1 + 1e-9)


class NetOut:
    """Async sender for one ``NetOutSettings``; ``sent_to`` records every destination address."""

    def __init__(self, settings: NetOutSettings, source: FrameSource, *, cid: uuid.UUID | None = None) -> None:
        settings.validate()
        self.settings = settings
        self._source = source
        self._cid = cid or uuid.uuid4()
        self._policies = {u: SendPolicy() for u in settings.universes}
        self._sacn_seq = dict.fromkeys(settings.universes, 0)
        self._artnet_seq = dict.fromkeys(settings.universes, 0)
        self._last_frame: dict[int, bytes] = {}
        self.subscribers = artnet_poll.SubscriberTable()
        self.sent_to: list[tuple[str, int]] = []
        self._sock = self._open_socket()

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.settings.protocol == "sacn":
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        return sock

    def _send(self, packet: bytes, address: tuple[str, int]) -> None:
        self.sent_to.append(address)
        self._sock.sendto(packet, address)

    # ----- sACN

    def _sacn_address(self, universe: int) -> tuple[str, int]:
        dest = self.settings.sacn_destination or ""
        host = sacn.multicast_address(universe) if dest == SACN_MULTICAST else dest
        return host, self.settings.sacn_port

    def _sacn_packet(self, universe: int, slots: bytes, *, terminate: bool = False) -> bytes:
        frame = sacn.SacnFrame(
            cid=self._cid,
            source_name=self.settings.source_name,
            universe=universe,
            slots=slots,
            sequence=self._sacn_seq[universe],
            terminate=terminate,
        )
        self._sacn_seq[universe] = (self._sacn_seq[universe] + 1) % 256
        return sacn.encode_data_packet(frame)

    # ----- Art-Net

    def _poll(self) -> None:
        self._send(artnet_poll.encode_artpoll(), (self.settings.artnet_poll_address or "", self.settings.artnet_port))

    def _read_replies(self, now: float) -> None:
        while True:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            try:
                self.subscribers.record(artnet_poll.parse_artpollreply(data), now)
            except ValueError:
                continue

    # ----- loop

    def tick(self, tick: int, now: float) -> int:
        """Send what is due for tick ``tick`` at scheduled time ``now``; returns universes sent."""
        if self.settings.protocol == "artnet":
            self._read_replies(now)
        changed = 0
        targets: set[tuple[str, int]] = set()
        for universe in self.settings.universes:
            slots = self._source(universe, tick)
            due = self._policies[universe].packets_due(now, slots)
            if not due:
                continue
            if slots != self._last_frame.get(universe):
                changed += 1
            self._last_frame[universe] = slots
            if self.settings.protocol == "sacn":
                # The first-send repeats share one sequence number: they are one frame.
                packet = self._sacn_packet(universe, slots)
                for _ in range(due):
                    self._send(packet, self._sacn_address(universe))
            else:
                self._artnet_seq[universe] = artnet.next_sequence(self._artnet_seq[universe])
                packet = artnet.encode_artdmx(universe, slots, self._artnet_seq[universe])
                for dest in self.subscribers.destinations(universe, now):
                    targets.add(dest)
                    for _ in range(due):
                        self._send(packet, dest)
        if changed > 1:
            for dest in sorted(targets):
                self._send(artnet.encode_artsync(), dest)
        return changed

    def terminate(self) -> None:
        """Send 3 Stream_Terminated packets per universe (sACN only)."""
        if self.settings.protocol != "sacn":
            return
        for universe in self.settings.universes:
            slots = self._last_frame.get(universe, bytes(512))
            for _ in range(TERMINATE_PACKETS):
                self._send(self._sacn_packet(universe, slots, terminate=True), self._sacn_address(universe))

    async def run(self, stop: asyncio.Event, *, duration_s: float | None = None) -> int:
        """Tick on the scheduled clock until ``stop`` is set or ``duration_s`` of ticks ran; returns ticks."""
        start = time.monotonic()
        next_poll = 0.0
        tick = 0
        try:
            while not stop.is_set():
                scheduled = tick * TICK_PERIOD_S
                if duration_s is not None and scheduled >= duration_s:
                    break
                delay = start + scheduled - time.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), delay)
                        break
                    except TimeoutError:
                        pass
                if self.settings.protocol == "artnet" and scheduled >= next_poll:
                    self._poll()
                    next_poll = scheduled + self.subscribers.poll_interval_s
                    await asyncio.sleep(0)
                self.tick(tick, scheduled)
                tick += 1
        finally:
            self.terminate()
            self._sock.close()
        return tick


__all__ = ["TERMINATE_PACKETS", "TICK_PERIOD_S", "FrameSource", "NetOut"]
