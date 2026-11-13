"""Nearby browse/scan mixin (split from b2b_nearby)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import unquote

import hmac

from apps.ableton_link.link_transports import classify_interface
from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_pairing import PairingError, rank_addresses

from .b2b_nearby_protocol import (
    LABEL_MAX,
    NEARBY_VERSION,
    NearbyPeer,
    _first_per_id,
    _is_ipv4,
    _now_iso,
    _unb64,
    pairing_tag,
)

log = logging.getLogger(__name__)


def _facade_const(name: str) -> float:
    """Read tunables from the public module so tests can monkeypatch ``b2b_nearby``."""
    from apps.b2b_pairing import b2b_nearby as facade

    return float(getattr(facade, name))


class NearbyScanMixin:
    def snapshot(self, wait: bool = False) -> dict[str, Any]:
        self.start()
        thread = self._kick_scan()
        if wait and thread is not None:
            started = self._scan_started
            thread.join(
                timeout=max(
                    0.0, _facade_const("SCAN_DEADLINE_S") - (self._clock() - started if started is not None else 0.0)
                )
            )
        with self._lock:
            self._abandon_overdue_scan()
            return {
                "me": {
                    "device_id": self.pairing.device_id,
                    "label": self.pairing.label,
                    "accepting": self.accepting,
                    "listed": self.running(),
                    "door_port": self.pairing.open_door(),
                },
                "nearby": [peer.view() for peer in self._peers],
                "scanned_at": self._scanned_at,
                "scanning": self._scan_thread is not None and self._scan_thread.is_alive(),
                "scan_error": self._scan_error,
                "scan_note": self._scan_note,
                "outgoing": self._outgoing_view(),
                "incoming_count": len(self._live_incoming()),
            }

    def discovery(self, kick: bool) -> dict[str, Any]:
        """B2BMCAST-02: what the last finished scan says about the remembered partner, read
        without starting Nearby. ``kick`` starts a background scan (only while running) so the
        next call reads a fresh one; it never waits for it."""
        if kick and self.running():
            self._kick_scan()
        with self._lock:
            self._abandon_overdue_scan()
            scanned = self._scanned
            listed = any(peer.remembered for peer in self._peers)
            return {
                "running": self.running(),
                "scanned_at": scanned,
                "scan_error": self._scan_error,
                "scan_note": self._scan_note,
                # A partial scan that missed the partner cannot say it is absent (B2BNEAR-06).
                "partner_listed": None
                if scanned is None or self._scan_error is not None or (not listed and self._scan_note is not None)
                else listed,
            }

    def find_partner(self) -> list[NearbyPeer]:
        """B2BREADDR-02: a NEW scan's instances that carry this pairing's tag, the remembered
        partner among them if it is on this network. Blocks for the scan (inside its deadline).
        The tag is public on Bonjour, so this is where to ask, never who it is: ``prove_at`` decides."""
        self.start()
        thread = self._kick_scan(fresh=True)
        if thread is not None:
            started = self._scan_started
            thread.join(
                timeout=max(
                    0.0, _facade_const("SCAN_DEADLINE_S") - (self._clock() - started if started is not None else 0.0)
                )
            )
        with self._lock:
            self._abandon_overdue_scan()
            if self._scan_error is not None:
                raise PairingError("scan_failed", self._scan_error, 503)
            return list(self._partner_candidates)

    def _kick_scan(self, fresh: bool = False) -> threading.Thread | None:
        with self._lock:
            self._abandon_overdue_scan()
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return self._scan_thread
            if (
                not fresh
                and self._scanned is not None
                and self._clock() - self._scanned < _facade_const("SCAN_MAX_AGE_S")
            ):
                return None
            self._sync_advert()
            self._scan_generation += 1
            generation = self._scan_generation
            self._scan_started = self._clock()
            self._scan_thread = threading.Thread(
                target=self._scan, args=(generation,), name="b2b-nearby-scan", daemon=True
            )
            self._scan_thread.start()
            return self._scan_thread

    def _abandon_overdue_scan(self) -> None:
        """Caller holds ``self._lock``. A scan past ``SCAN_DEADLINE_S`` ends as a timeout now; its
        thread is left to finish on its own and whatever it finds is dropped."""
        thread, started = self._scan_thread, self._scan_started
        scan_deadline_s = _facade_const("SCAN_DEADLINE_S")
        if thread is None or not thread.is_alive() or started is None or self._clock() - started < scan_deadline_s:
            return
        log.warning("b2b nearby scan still running %.1f s after it started; reporting a timeout", scan_deadline_s)
        self._scan_generation += 1
        self._scan_thread = None
        self._scan_error = f"scan timed out after {scan_deadline_s:g} s"
        self._scan_note = None
        self._scanned = self._clock()
        self._scanned_at = _now_iso()

    def _scan(self, generation: int) -> None:
        try:
            result = b2b_mdns.scan()
            every = self._every_peer_from(result.resolved)
        except Exception as exc:  # the thread's only exit for an error is scan_error
            log.exception("b2b nearby scan failed")
            self._finish_scan(generation, error=f"{type(exc).__name__}: {exc}")
            return
        self._finish_scan(generation, peers=every, note="; ".join(result.notes) or None)

    def _finish_scan(
        self,
        generation: int,
        *,
        peers: list[NearbyPeer] | None = None,
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if generation != self._scan_generation:
                log.warning("b2b nearby scan finished after its deadline; result dropped")
                return
            if error is None:
                every = peers or []
                self._peers = sorted(_first_per_id(every), key=lambda p: (not p.remembered, p.label.lower()))
                self._partner_candidates = [peer for peer in every if peer.remembered]
            self._scan_error = error
            self._scan_note = note
            self._scanned = self._clock()
            self._scanned_at = _now_iso()

    def _peers_from(self, found: list[b2b_mdns.Resolved]) -> list[NearbyPeer]:
        """The listing: one entry per device id, the first instance seen."""
        return _first_per_id(self._every_peer_from(found))

    def _every_peer_from(self, found: list[b2b_mdns.Resolved]) -> list[NearbyPeer]:
        """Every OTHER nearby instance, in scan order, two instances under one id included."""
        record = self.pairing.record
        own_tag = pairing_tag(record.token) if record is not None else None
        hardware = self._hardware_ports()
        peers: list[NearbyPeer] = []
        for resolved in found:
            txt = resolved.txt
            peer_id = txt.get("id", "")
            if txt.get("nv") != NEARBY_VERSION or not peer_id or peer_id == self.pairing.device_id:
                continue
            # An advert is whatever its sender typed: only IPv4 literals are addresses of a door.
            addresses = [(ip, dev) for ip, dev in resolved.addresses if _is_ipv4(ip)]
            ranked = rank_addresses([(ip, classify_interface(dev, hardware.get(dev))) for ip, dev in addresses])
            devices = dict(addresses)
            peer_pub: bytes | None = None
            raw_pk = txt.get("pk")
            if raw_pk:
                try:
                    decoded = _unb64(raw_pk)
                except PairingError:
                    decoded = b""
                if len(decoded) == 32:
                    peer_pub = decoded
            peers.append(
                NearbyPeer(
                    id=peer_id,
                    label=unquote(txt.get("name", ""))[:LABEL_MAX] or "Open DJ",
                    accepting=txt.get("acc") == "1",
                    remembered=own_tag is not None and hmac.compare_digest(txt.get("pt", ""), own_tag),
                    port=resolved.port,
                    addresses=ranked,
                    instance=resolved.instance,
                    via=devices[ranked[0][0]] if ranked else None,
                    pub=peer_pub,
                )
            )
        return peers

    def _peer(self, peer_id: str) -> NearbyPeer:
        with self._lock:
            for peer in self._peers:
                if peer.id == peer_id:
                    return peer
        raise PairingError("not_nearby", "That Open DJ is not listed nearby (any more): refresh the list", 404)
