"""PARITY-02 serving-lane registration for the key producer.

Imported from ``apps.webui.server.analysis_serving_bootstrap``, not from
``apps.analysis_key.__init__``, to avoid an ``analysis`` <-> ``analysis_key``
import cycle.
"""
from __future__ import annotations

from apps.analysis.serving_lanes import register_serving_lane

register_serving_lane("key")
