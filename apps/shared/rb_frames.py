"""Rekordbox frame arithmetic -- one home for the 44.1 kHz heuristic.

T3b defect D3 (``.planning/t3b-decomposition-map.md`` section 3): the
millisecond-to-frame conversion existed twice, byte-identical, in
``apps/sync/rb_writer.py`` and in ``rb_vendor.py``'s hot-cue writer, whose
docstring named the other copy without either ever being consolidated.

The map nominates ``adapters/rekordbox/cues.py`` as the single home. That
works at the package's final root, but not at its interim one: the split is
staged under ``apps/webui/server/rb_vendor_pkg/`` for the duration of T3b, and
``.importlinter``'s ``webui-is-the-top-layer`` contract forbids ``apps.sync``
(or any other domain package) from importing ``apps.webui`` -- a hard-fail
contract whose own comment reads "Never add a line here: add the inversion
instead". ``apps.shared`` is the layer both writers may legally import, and
``rb_color_palette.py`` is the standing precedent for a small rekordbox
conversion primitive living here. ``rb_vendor_pkg.cues`` re-exports it, so the
adapter surface is exactly what the map specifies and there is still one
definition. When the package leaves ``apps/webui``, this module folds into
``cues.py`` and the indirection disappears.
"""
from __future__ import annotations

#: Frames per millisecond at 44.1 kHz.
FRAMES_PER_MSEC: float = 0.441


def msec_to_frame(msec: int) -> int:
    """Approximate audio-frame offset for non-MPEG containers at 44.1 kHz.

    Rekordbox stores ``InFrame`` alongside ``InMsec``. Our read path never
    reads it back (``fetch_cues`` works from ``InMsec``), so this exists for
    on-disk-row authenticity: a row we write should look like a row rekordbox
    wrote. MPEG containers use ``InMpegFrame``/``InMpegAbs`` instead, which
    both writers leave NULL for rekordbox to backfill.
    """
    return round(msec * FRAMES_PER_MSEC)


__all__ = ["FRAMES_PER_MSEC", "msec_to_frame"]
