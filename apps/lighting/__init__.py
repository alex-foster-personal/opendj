"""Lighting output for Open DJ (LIGHT-01): beat and phrase feed, protocol emitters, fixtures, shows.

Specs: (private path)/. Gated by feature flag ``lighting.output``
(default off). Outside code imports only ``apps.lighting.contract``; delivery layers may also
import ``apps.lighting.http`` once it exists.
"""
