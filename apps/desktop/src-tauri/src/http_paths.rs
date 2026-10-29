//! HTTP paths the shell probes or serves on loopback.

/// Engine health probe used during boot and supervision.
pub const HEALTH_PATH: &str = "/api/v1/health";
