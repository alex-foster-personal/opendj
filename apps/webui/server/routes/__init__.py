"""FastAPI route modules under ``apps.webui.server.routes``.

A module is mounted under ``/api/v1`` when it sets ``MOUNT_UNDER_API_PREFIX = True``
and defines a module-level ``router`` (``APIRouter``). Discovery imports modules in
name order via ``pkgutil.iter_modules``.

Conditional routers, factory-built routers, and modules that only add routes to a
parent's router stay explicit in ``app_wiring._mount_api_routers`` and must not set
the flag (child modules would mount the same router object twice).
"""
