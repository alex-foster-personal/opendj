/** LIBM-129 v1: syntax + server existence checks for watcher folder prefs (no daemon). */

export function formatWatcherFolderLines(paths: readonly string[]): string {
	return paths.join('\n');
}

export function parseWatcherFolderLines(text: string): string[] {
	const lines = text
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line.length > 0);
	const seen = new Set<string>();
	const out: string[] = [];
	for (const line of lines) {
		validateWatcherFolderPathSyntax(line);
		if (seen.has(line)) continue;
		seen.add(line);
		out.push(line);
	}
	return out;
}

// Absolute on the platform the daemon runs on: POSIX (/Users/dev/Music), a
// Windows drive letter (C:\Music or C:/Music) or a UNC share
// (\\server\share\Music). Drive-relative (C:Music) and rooted-but-driveless
// (\Music) forms stay rejected (PR #4014, Sol P2).
const WINDOWS_DRIVE_ABSOLUTE = /^[A-Za-z]:[\\/]/;
const WINDOWS_UNC_ABSOLUTE = /^\\\\[^\\/]+[\\/][^\\/]+/;

function isAbsoluteWatcherFolderPath(path: string): boolean {
	return (
		path.startsWith('/') || WINDOWS_DRIVE_ABSOLUTE.test(path) || WINDOWS_UNC_ABSOLUTE.test(path)
	);
}

export function validateWatcherFolderPathSyntax(path: string): void {
	if (!isAbsoluteWatcherFolderPath(path)) {
		throw new Error(`watcher folder must be an absolute path: ${path}`);
	}
	if (path.includes('..')) {
		throw new Error(`watcher folder must not contain ..: ${path}`);
	}
}

export async function validateWatcherFoldersExist(paths: readonly string[]): Promise<void> {
	if (paths.length === 0) return;
	const res = await fetch('/api/v1/ui-prefs/watcher-folders:validate', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ paths })
	});
	if (res.ok) return;
	let detail = `HTTP ${res.status}`;
	try {
		const body = (await res.json()) as { detail?: unknown };
		if (typeof body.detail === 'string') detail = body.detail;
		else if (body.detail && typeof body.detail === 'object') {
			const missing = (body.detail as { missing?: string[] }).missing;
			if (Array.isArray(missing) && missing.length > 0) {
				detail = `folder does not exist: ${missing.join(', ')}`;
			}
		}
	} catch {
		// keep generic detail
	}
	throw new Error(detail);
}
