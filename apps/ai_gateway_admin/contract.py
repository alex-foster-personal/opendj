"""The AI gateway admin contract: a CLI with no in-process consumers.

``python -m apps.ai_gateway_admin`` talks to the Worker's ``/admin`` endpoints
over HTTP. No package imports it, so this surface declares a version and no
types; the wire it speaks is the Worker's (``apps/ai_gateway_worker``).
"""

from __future__ import annotations

CONTRACT_VERSION = "ai_gateway_admin/1"
