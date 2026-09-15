// Semgrep positive control for the p/typescript pack. Never imported or built.
// scripts/security/scan_sast.sh fails as UNKNOWN when this file produces no finding
// for javascript.browser.security.wildcard-postmessage-configuration.

export function broadcastToAnyOrigin(data: unknown): void {
  window.parent.postMessage(data, "*");
}

export function runFragmentAsCode(): unknown {
  return eval(window.location.hash.slice(1));
}
