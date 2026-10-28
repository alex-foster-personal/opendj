"""AutoDJ abstractions: the planner / executor / critic split the canon doc sets.

``(private path)`` (CANON, the maintainer Tue 6 Oct
2026) is the law for this package. In one line: an agent (or AutoPlay, with no
model at all) reads compact musical facts, picks a named transition style, and
submits a :class:`~apps.autodj.plan.TransitionPlan` in absolute bars; the page
executes it on the deck's own grid; nothing here turns a knob in real time.

The modules, in the order a transition is decided:

* :mod:`sections` -- rekordbox PSSI kinds and own (All-In-One) labels mapped
  onto one DJ vocabulary (intro, build, drop, breakdown, outro, ...).
* :mod:`profile` -- :class:`TrackProfile`, one track's bars, sections, energy,
  bass, vocals and key, built from the ``/anlz`` payload the app already serves.
* :mod:`windows` -- ranked mix-in / mix-out windows (``get_mix_windows``).
* :mod:`scoring` -- window-level pair scoring (``score_pair``).
* :mod:`styles` -- the transition style catalog (``list_transition_styles``).
* :mod:`plan` -- the plan type and ``validate_plan``.
* :mod:`propose` -- the deterministic chooser (``propose_transition``).
* :mod:`compile` -- a plan turned into the page's ``plan`` order (AGENT-24).
* :mod:`service` -- the HTTP-backed facade the MCP server and CLI share.

Everything except :mod:`service` is pure: no I/O, no clock, so it is tested
with fixtures and its answers are reproducible.
"""
