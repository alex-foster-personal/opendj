// Semgrep positive control for the p/rust pack. Never compiled.
// scripts/security/scan_sast.sh fails as UNKNOWN when this file produces no finding.

fn main() {
    let value: i32 = 7;
    let pointer = &value as *const i32;
    let first_arg = std::env::args().nth(1);
    let scratch = std::env::temp_dir().join("control");
    unsafe {
        println!("{} {:?} {:?}", *pointer, first_arg, scratch);
    }
}
