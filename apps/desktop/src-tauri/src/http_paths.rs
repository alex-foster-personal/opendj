//! HTTP paths the shell probes or serves on loopback.

/// Engine health probe used during boot and supervision.
pub const HEALTH_PATH: &str = "/api/v1/health";
/// The engine's own build identity (`apps/engine_core/build_info.py`).
pub const BUILD_INFO_PATH: &str = "/api/v1/build-info";
