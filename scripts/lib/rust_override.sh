# shellcheck shell=bash
# Sourced by scripts/cloud_agent_setup.sh. Pure text in, text out, so it is
# testable on the exact `rustup show active-toolchain` wording.

# rust_override_hint <active-toolchain output>: print how to clear the rustup
# override that selected the active toolchain, or nothing when none did.
rust_override_hint() {
  case "$1" in
    *"RUSTUP_TOOLCHAIN"*)
      printf '%s\n' "unset RUSTUP_TOOLCHAIN" ;;
    *"directory override"* | *"overridden by"*)
      printf '%s\n' "run 'rustup override unset' in this checkout (or edit its rust-toolchain file)" ;;
  esac
}
