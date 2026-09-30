// Test-only link flag. `tests/rt_malloc.rs` replaces the C allocator to count
// allocations made by C++ code in the render path; for libstdc++'s `new` to
// reach that replacement, the test binary must export its `malloc`. Linux
// only; the shipped binary is linked as before.
fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux") {
        println!("cargo:rustc-link-arg-tests=-Wl,--export-dynamic");
    }
    println!("cargo:rerun-if-changed=build.rs");
}
