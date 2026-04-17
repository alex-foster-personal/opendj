"""FastAPI daemon package.

Modules:
  app        -- FastAPI() instance (import as ``apps.webui.server.app:app``).
  backend    -- StateBackend protocol + in-memory impl used by tests and
                as a v1 reference. Phase 5 can wire a sqlite-backed impl
                in apps.shared.state later.
  deps       -- FastAPI dependencies (get_read_state / get_write_state).
  etag       -- sha1(stable_id + ":" + modified_at) helper.
  errors     -- shared HTTP error models (409 ConflictError, 428, etc.).
  models     -- pydantic request/response schemas.
  routes/    -- per-surface routers.
"""
from __future__ import annotations
