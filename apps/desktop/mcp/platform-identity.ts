/**
 * Platform seam for "who is this process": the one place the MCP harnesses and their
 * tests read a POSIX uid. Windows has no uid, so it is never root here.
 */
export function runningAsPosixRoot(
	platform: NodeJS.Platform = process.platform,
	uid: number | null = process.getuid?.() ?? null
): boolean {
	return platform !== 'win32' && uid === 0;
}
