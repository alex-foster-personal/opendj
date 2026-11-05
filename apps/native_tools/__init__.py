"""Re-export macOS ``sysctl`` helpers from ``apps.shared.macos_sysctl``.

``apps.stems.live_capability`` and ``tests/shared/test_native_tool_paths.py``
import from here. The implementation lives in shared so lower layers (for
example ``apps.shared.machine_metrics``) can call ``try_sysctl_n`` without
reaching into ``native_tools``.
"""

from __future__ import annotations

from apps.shared.macos_sysctl import SYSCTL, sysctl_n, try_sysctl_n

__all__ = ["SYSCTL", "sysctl_n", "try_sysctl_n"]
