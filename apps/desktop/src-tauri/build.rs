fn main() {
    // Everything read with option_env! is baked in at compile time, and cargo
    // does not track those on its own: without these lines a changed value
    // leaves the previous one compiled into the binary and the build looks
    // successful while shipping a stale answer. That is precisely the failure
    // the build stamp exists to make visible, so it must not happen to the
    // stamp itself.
    //
    // OPENDJ_DEFAULT_ENGINE_ORIGIN is deliberately gone: a shell that starts
    // its own engine on an ephemeral port has no default address to bake, and
    // a baked one would point a lane at whatever answered on that port first.
    for variable in [
        "OPENDJ_BUILD_GIT_SHA",
        "OPENDJ_BUILD_GIT_SHA_FULL",
        "OPENDJ_BUILD_GIT_BRANCH",
        "OPENDJ_BUILD_GIT_DIRTY",
        "OPENDJ_BUILD_AT_UTC",
        "OPENDJ_BUILD_LANE_LABEL",
        "OPENDJ_BUILD_CHANNEL",
        "OPENDJ_BUILD_EVIDENCE_AT_UTC",
    ] {
        println!("cargo:rerun-if-env-changed={variable}");
    }
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new()
                .commands(&["native_midi_snapshot", "native_midi_send"]),
        ),
    )
    .expect("failed to build Tauri application metadata")
}
