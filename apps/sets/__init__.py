"""Phase 12 -- set recording, transition classification, replay.

Package layout::

  paths        -- constants for the data/sets/ tree
  state        -- sqlite shim (per-set sessions + event timeline)
  manifest     -- manifest.json read/write helpers
  capture      -- ffmpeg AVFoundation subprocess orchestration (MP3 rolling)
  record       -- top-level orchestrator (start/stop/resume/status/list)
  watermark    -- ID3 COMM 'personal-review-only' stamping (stdlib impl)
  retention    -- prune old MP3 segments, keep timeline.jsonl
  sources/     -- deck-state pollers (djay_source, rb_source)

Safety posture (CONTEXT D6): share_state defaults to 'private'; every
recorded MP3 carries an ID3 COMM watermark tagging it for personal review
only. No live-DB writes anywhere in this phase.
"""
from __future__ import annotations

__all__: list[str] = []
