import { runningAsPosixRoot } from './platform-identity';

/**
 * Platform seam for launching Chromium-family processes (Electron, Chrome) from the MCP
 * harnesses. Chromium on Linux and macOS refuses to start its sandbox as root, so a root
 * run needs `--no-sandbox`; Windows has no uid and no such check (platform-identity.ts).
 */
export function chromiumSandboxArgs(asPosixRoot: boolean = runningAsPosixRoot()): string[] {
	return asPosixRoot ? ['--no-sandbox'] : [];
}
