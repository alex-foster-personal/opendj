"""Pydantic schemas for PREFLIGHT-01's boot gate (``GET /api/v1/preflight``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PreflightCheckOut(BaseModel):
    """One row of PREFLIGHT-01's boot gate (issue #771).

    ``status`` is never a two-way pass/fail: ``pending`` covers a check that
    genuinely could not be exercised (e.g. audio-access with no resolvable
    track anywhere in a small sample), which is an honest denominator, never
    a fabricated pass. ``remediation`` is null on a pass or a pending row and
    a real sentence on a fail.

    ``user_*`` fields carry plain-language copy for the boot gate (issue
    #2722). Admin/diagnostics views keep the technical ``label``/``detail``.
    """

    id: str
    label: str
    status: Literal["pass", "fail", "pending"]
    detail: str
    remediation: str | None = None
    user_label: str | None = None
    user_detail: str | None = None
    user_remediation: str | None = None
    #: How much this check MATTERS, which is a different axis from whether it
    #: passed (the maintainer, Wed 16 Sep 2026, after a fresh-Mac first run: "some
    #: checks aren't so important"). ``blocking`` means the app cannot
    #: usefully run until it passes, so the boot gate holds. ``advisory``
    #: means the app runs fine and the user is told, so the gate does not
    #: hold. The UI paints red for a failed blocking check and orange for a
    #: failed or unexercised advisory one, rather than red for everything.
    severity: Literal["blocking", "advisory"] = "blocking"
    #: One sentence answering "what do I do about this?", shown on hover.
    #: Distinct from ``remediation``: that is the fix for a FAILURE, this is
    #: present on every row including passes, so a user can ask what a row
    #: means without having to break it first.
    explainer: str | None = None


class PreflightOut(BaseModel):
    """``GET /api/v1/preflight`` -- the ONE source of truth for the boot
    gate. ``status`` is ``fail`` iff a check that is ``severity: blocking``
    is ``fail``; a ``pending`` check never blocks it, because a check that
    could not be exercised is not a defect on its own, and an ``advisory``
    check never blocks it either, because the app runs without it.

    ``advisories`` counts the non-blocking rows the user should still see,
    so a caller can distinguish "everything is fine" from "running, with
    things worth telling you" without recomputing severity for itself.
    """

    status: Literal["pass", "fail"]
    advisories: int = 0
    checks: list[PreflightCheckOut]


__all__ = ["PreflightCheckOut", "PreflightOut"]
