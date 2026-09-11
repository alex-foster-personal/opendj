"""Own key analysis helpers (nav1-key-r0 round 0).

Record writing and `/anlz` projection live in later lanes; this package
registers the lane as served once imported so PARITY-02 can allow `own`.
"""

from apps.analysis.selection import register_serving_lane

register_serving_lane("key")
