"""LINKROLE-01/02: the engine owns who is ON AIR, and the one verdict on whether the peer is live.

Before this, each laptop's PAGE ran the take-over clash rule on its own copy of the other's claim,
with its own 2 s freshness limit, while the engine only stored whatever role it was POSTed
(docs/b2b-link/FE-LOGIC-AUDIT.md, moves 1 and 2). Two laptops could reach different verdicts:
- a tie with an unreadable device id threw, and both stayed ON AIR;
- one side saw the peer as stale and kept ON AIR while the other did not;
- an HTTP client could POST ``on_air`` past the rule, and anyone could write the session tempo.

Now the engine decides and the page renders it:
- ``RoleAuthority.take_over`` stamps the press with a Lamport number, one past the highest the
  peer has claimed (two Macs share no clock to timestamp a press with);
- ``RoleAuthority.observe_peer`` applies ``resolve_clash`` to the two claims and yields with the
  reason when this side lost. The verdict depends on the two claims ONLY, never on how fresh one
  looks from here, so two engines that saw the same two claims agree;
- each engine sends its own claim on the read it already makes of the other (``link_claim`` on
  ``GET /b2b/now-playing``), so a claim crosses even when only one direction works. A received
  claim can only make this side yield, never take ON AIR;
- ``peer_liveness`` is the one live verdict, with its age and its limit on the wire.

Pure (no FastAPI); ``routes/link_tempo.py`` and ``routes/b2b_peer.py`` hold the instance.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Final, Literal

from apps.shared.link_constants import CLAIM_PARAM_RE

LinkRole = Literal["on_air", "following"]
#: LINKLANE-01: whose grid the shared lane draws, from this engine's side.
LaneLead = Literal["self", "peer"]
ClashOutcome = Literal["keep", "yield"]

_ROLES: Final[frozenset[str]] = frozenset({"on_air", "following"})
#: A peer answer (its mirror age plus the time since this engine received it) older than this is
#: not live. One limit for the engine and the page: the page reads it from the wire.
PEER_LIVE_MAX_MS: Final[float] = 3000.0
#: An ON AIR claim with no take-over number (a page reload or an older build) ranks below every
#: press, so any later Take over outranks it (live, Mon 12 Oct 2026, after a Reconnect).
UNNUMBERED_TAKE_OVER: Final[int] = 0
_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class RoleClaim:
    role: LinkRole
    #: Lamport take-over number; None while following, or from a page that published none.
    took_seq: int | None
    #: The claiming engine's id (the tie-break key); None from a build that sent none.
    peer_id: str | None


@dataclass(frozen=True)
class PeerLiveness:
    live: bool
    #: Age of the peer's state when judged, ms; None when it never answered.
    age_ms: float | None
    max_age_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {"live": self.live, "age_ms": self.age_ms, "max_age_ms": self.max_age_ms}


NEVER_ANSWERED: Final[PeerLiveness] = PeerLiveness(live=False, age_ms=None, max_age_ms=PEER_LIVE_MAX_MS)


# ----------------------------------------------------------------------------- pure rules


def resolve_clash(mine: RoleClaim, theirs: RoleClaim) -> ClashOutcome:
    """What THIS side does when both claim ON AIR. The higher take-over number keeps it; a tie
    goes to the lower engine id; a tie with an unknown id yields (no side may guess, and yielding
    is the one move that can never leave two laptops ON AIR). Its own id is not a clash."""
    if mine.peer_id is not None and mine.peer_id == theirs.peer_id:
        return "keep"
    mine_seq = UNNUMBERED_TAKE_OVER if mine.took_seq is None else mine.took_seq
    theirs_seq = UNNUMBERED_TAKE_OVER if theirs.took_seq is None else theirs.took_seq
    if mine_seq != theirs_seq:
        return "keep" if mine_seq > theirs_seq else "yield"
    if mine.peer_id is None or theirs.peer_id is None:
        return "yield"
    return "keep" if mine.peer_id < theirs.peer_id else "yield"


def peer_liveness(*, mirror_age_ms: float, since_answer_ms: float) -> PeerLiveness:
    """The one live verdict: the peer's mirror age (its own page's publish age) plus the time
    since this engine received the answer, against ``PEER_LIVE_MAX_MS``."""
    for name, value in (("mirror_age_ms", mirror_age_ms), ("since_answer_ms", since_answer_ms)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"peer_liveness: {name} must be finite and >= 0, got {value}")
    age = mirror_age_ms + since_answer_ms
    return PeerLiveness(live=age <= PEER_LIVE_MAX_MS, age_ms=age, max_age_ms=PEER_LIVE_MAX_MS)


def parse_claim(link: Any) -> RoleClaim | None:
    """A claim from a now-playing ``link`` object; None for a missing or malformed one (never a
    default role)."""
    if not isinstance(link, dict) or link.get("role") not in _ROLES:
        return None
    seq, peer_id = link.get("took_seq"), link.get("peer_id")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return None
    if peer_id is not None and not (isinstance(peer_id, str) and _ID_RE.fullmatch(peer_id)):
        return None
    return RoleClaim(role=link["role"], took_seq=seq, peer_id=peer_id)


def claim_param(claim: RoleClaim) -> str:
    if claim.peer_id is None:
        raise ValueError("claim_param: an engine always sends its own id")
    return f"{claim.role}.{'' if claim.took_seq is None else claim.took_seq}.{claim.peer_id}"


def parse_claim_param(raw: str) -> RoleClaim | None:
    match = re.fullmatch(CLAIM_PARAM_RE, raw)
    if match is None:
        return None
    role, seq, peer_id = raw.split(".", 2)
    return RoleClaim(role=role, took_seq=None if seq == "" else int(seq), peer_id=peer_id)  # type: ignore[arg-type]


def _capitalized(name: str) -> str:
    return name[:1].upper() + name[1:]


def take_over_lost_text(peer_name: str, mine: RoleClaim, theirs: RoleClaim) -> str:
    """The reason the page toasts when this side yielded."""
    name = _capitalized(peer_name)
    if (mine.took_seq or UNNUMBERED_TAKE_OVER) != (theirs.took_seq or UNNUMBERED_TAKE_OVER):
        return f"{name} took over just after you, so {name} leads now. Take over again to lead."
    if mine.peer_id is None or theirs.peer_id is None:
        return (
            f"You and {name} took over at the same moment and one side sent no id to break the tie, "
            "so neither leads. Take over again to lead."
        )
    return f"You and {name} took over at the same moment; {name} leads (tie goes to the lower id). Take over again to lead."


# ----------------------------------------------------------------------------- the authority


class RoleAuthority:
    """This engine's role. Thread-safe: routes call it from FastAPI's worker threads."""

    def __init__(self, engine_id: str) -> None:
        if not _ID_RE.fullmatch(engine_id):
            raise ValueError(f"RoleAuthority: engine id must match {_ID_RE.pattern}, got {engine_id!r}")
        self.engine_id = engine_id
        self._lock = threading.Lock()
        self.role: LinkRole = "following"
        self.took_seq: int | None = None
        self.reason: str | None = None
        self.peer_seq_seen = 0
        self.peer_claim: RoleClaim | None = None
        #: The peer's last good answer: (its mirror age ms, when this engine received it, s).
        self._peer_answer: tuple[float, float] | None = None
        #: The peer's display name from its last answer, for the yield reason.
        self.peer_name = "the other laptop"
        #: Whether the peer's listener accepts this engine's claim (an older build refuses the
        #: parameter); None until the first try.
        self.peer_takes_claims: bool | None = None
        #: Bumped on every role change, so a reader can tell two changes apart.
        self.epoch = 0

    def claim(self) -> RoleClaim:
        with self._lock:
            return RoleClaim(role=self.role, took_seq=self.took_seq, peer_id=self.engine_id)

    def take_over(self) -> RoleClaim:
        """ON AIR, numbered one past the highest the peer has claimed: a press made after seeing
        the peer's claim always outranks it; presses that cannot have seen each other tie."""
        with self._lock:
            self.role, self.took_seq, self.reason = "on_air", self.peer_seq_seen + 1, None
            self.epoch += 1
            return RoleClaim(role=self.role, took_seq=self.took_seq, peer_id=self.engine_id)

    def follow(self, reason: str | None) -> None:
        with self._lock:
            self.role, self.took_seq, self.reason = "following", None, reason
            self.epoch += 1

    def timeline_jumped(self, *, reason: str, now_s: float) -> bool:
        """LINKROLE-03: the page saw the session timeline jump while ON AIR (a force it did not make).
        A live Open DJ partner that claims FOLLOWING did not take over (the jump was its stale
        re-anchor, or a late read), so this side keeps ON AIR; demoting here left both laptops
        FOLLOWING (rig, Thu 15 Oct 2026). With no live partner claim (a commercial peer, a partner
        gone quiet) or a partner claiming ON AIR, the jump demotes, as before. True when it did."""
        live = self.liveness(now_s=now_s).live
        with self._lock:
            if self.role != "on_air":
                return False
            partner = self.peer_claim
            if live and partner is not None and partner.role == "following" and partner.peer_id != self.engine_id:
                return False
            self.role, self.took_seq, self.reason = "following", None, reason
            self.epoch += 1
            return True

    def note_peer_answer(self, *, mirror_age_ms: float, received_at_s: float, peer_name: str | None) -> None:
        """A good peer answer arrived: the input to the one liveness verdict."""
        peer_liveness(mirror_age_ms=mirror_age_ms, since_answer_ms=0.0)  # validates
        with self._lock:
            self._peer_answer = (mirror_age_ms, received_at_s)
            if peer_name:
                self.peer_name = peer_name

    def forget_peer_link(self) -> None:
        """The peer address changed: its answers and its claim capability are unknown again. The
        highest take-over number seen stays (it only ever raises this side's next press)."""
        with self._lock:
            self._peer_answer, self.peer_claim, self.peer_takes_claims = None, None, None

    def liveness(self, *, now_s: float) -> PeerLiveness:
        with self._lock:
            answer = self._peer_answer
        if answer is None:
            return NEVER_ANSWERED
        mirror_age_ms, received_at_s = answer
        return peer_liveness(mirror_age_ms=mirror_age_ms, since_answer_ms=max(0.0, (now_s - received_at_s) * 1000.0))

    def observe_peer(self, claim: RoleClaim | None, peer_name: str | None = None) -> str | None:
        """Note the peer's claim and resolve a clash. Returns the reason when this side yielded.
        How fresh the claim looks from here is deliberately not an input."""
        if claim is None:
            return None
        with self._lock:
            self.peer_claim = claim
            if claim.took_seq is not None and claim.took_seq > self.peer_seq_seen and claim.peer_id != self.engine_id:
                self.peer_seq_seen = claim.took_seq
            if self.role != "on_air" or claim.role != "on_air":
                return None
            mine = RoleClaim(role=self.role, took_seq=self.took_seq, peer_id=self.engine_id)
            if resolve_clash(mine, claim) == "keep":
                return None
            self.role, self.took_seq = "following", None
            self.reason = take_over_lost_text(peer_name or self.peer_name, mine, claim)
            self.epoch += 1
            return self.reason

    def peer_holds_turn(self, *, now_s: float) -> bool:
        """The peer is live and ON AIR while this side follows: only it may write the session tempo."""
        peer_live = self.liveness(now_s=now_s).live
        with self._lock:
            return (
                self.role == "following"
                and peer_live
                and self.peer_claim is not None
                and self.peer_claim.role == "on_air"
                and self.peer_claim.peer_id != self.engine_id
            )

    def lane_lead(self, *, now_s: float) -> tuple[LaneLead, str]:
        """LINKLANE-01: who owns the shared lane grid, and why. ON AIR leads a FOLLOWING partner;
        with both on the same role the lower engine id leads. Both engines see the same two claims,
        so they name the same leader; with no live partner this laptop draws its own grid."""
        peer_live = self.liveness(now_s=now_s).live
        with self._lock:
            peer = self.peer_claim
            if not peer_live or peer is None or peer.peer_id == self.engine_id:
                return "self", "no live partner: this laptop draws its own grid"
            if self.role != peer.role:
                return ("self", "this laptop is ON AIR") if self.role == "on_air" else ("peer", "the partner is ON AIR")
            if peer.peer_id is None:
                return "self", "the partner's build sends no engine id, so each laptop draws its own grid"
            if self.engine_id < peer.peer_id:
                return "self", "neither side alone is ON AIR: the lower engine id (this one) leads"
            return "peer", "neither side alone is ON AIR: the lower engine id (the partner's) leads"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            peer = self.peer_claim
            return {
                "role": self.role,
                "took_seq": self.took_seq,
                "reason": self.reason,
                "engine_id": self.engine_id,
                "epoch": self.epoch,
                "peer_seq_seen": self.peer_seq_seen,
                "peer_claim": None
                if peer is None
                else {"role": peer.role, "took_seq": peer.took_seq, "peer_id": peer.peer_id},
                "peer_name": self.peer_name,
                "peer_takes_claims": self.peer_takes_claims,
            }

    def snapshot_with_liveness(self, *, now_s: float) -> dict[str, Any]:
        lead, lead_reason = self.lane_lead(now_s=now_s)
        return {**self.snapshot(), "peer_liveness": self.liveness(now_s=now_s).as_dict(),
                "lane_lead": lead, "lane_lead_reason": lead_reason}
