"""DJ Copilot: PLAY IT solver + AI-01 next-track suggester (Phase 13).

Public surface:

* :class:`set_goal.SetGoal` -- structured goal for PLAY IT / AI-02.
* :func:`energy_curve.target_energy_at` -- piecewise-linear target curve.
* :func:`solver.suggest_order` -- greedy beam PLAY IT solver.
* :func:`play_it.play_it` -- solve + persist a named play-order.
* :func:`overrides.set_override` / :func:`overrides.clear_override`
  -- PLAY-03 per-track override write-path.
* :func:`suggester.suggest_next` -- AI-01 ranked next-track candidates.

All modules are pure-stdlib; no ML, no network, no subprocess.
"""
from __future__ import annotations
