fn main() {
    // DEFAULT_ENGINE_ORIGIN is baked in with option_env!, which cargo does
    // not track on its own: without this line, changing the variable in
    // .env would leave a stale origin compiled into the binary and the
    // build would look successful while shipping the wrong port.
    println!("cargo:rerun-if-env-changed=OPENDJ_DEFAULT_ENGINE_ORIGIN");
    tauri_build::build()
}
