// Semgrep positive control for the p/typescript pack. Never imported or built.
// scripts/security/scan_sast.sh fails as UNKNOWN when this file produces no finding.

export function runUserCode(userInput: string): unknown {
  return eval(userInput);
}

export function renderUnsafe(target: HTMLElement, html: string): void {
  target.innerHTML = html;
}
