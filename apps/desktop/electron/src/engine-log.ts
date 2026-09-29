// Day-based engine log rotation shared by the shell and tests.
//
// Port of apps/desktop/src-tauri/src/engine_log.rs. Same limits, same archive
// names, same low-disk latch, so a log directory written by either shell reads
// the same to a tester and to the pruning below.

import * as fs from 'node:fs';
import * as path from 'node:path';

export const ENGINE_LOG_MAX_BYTES = 5 * 1024 * 1024;
export const ENGINE_LOG_RETENTION_DAYS = 7;
export const ENGINE_LOG_MAX_TOTAL_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024;
export const ENGINE_LOG_MAX_ARCHIVE_COUNT = 20;
export const ENGINE_LOG_MIN_FREE_BYTES = 1024 * 1024 * 1024;

const lowDisk = { rotationDisabled: false, lowDiskLogged: false };
let diskFreeOverride: number | null = null;

/** Test seam: pin the free-space figure so the runner's real disk never decides. */
export function setDiskFreeOverride(free: number | null): void {
	diskFreeOverride = free;
}

/** Test seam: clear the process-wide low-disk latch and the override. */
export function resetLowDiskState(): void {
	diskFreeOverride = null;
	lowDisk.rotationDisabled = false;
	lowDisk.lowDiskLogged = false;
}

function mountDir(target: string): string {
	try {
		if (fs.statSync(target).isDirectory()) return target;
	} catch {
		// Not a directory (or not there yet): measure its parent.
	}
	return path.dirname(target);
}

/** Free bytes on the mount containing `target`. Throws when it cannot measure. */
export function diskFreeBytes(target: string): number {
	if (diskFreeOverride !== null) return diskFreeOverride;
	const stats = fs.statfsSync(mountDir(target));
	return Number(stats.bavail) * Number(stats.bsize);
}

function rotationAllowed(target: string): boolean {
	if (lowDisk.rotationDisabled) return false;
	const mount = mountDir(target);
	const free = diskFreeBytes(mount);
	if (free < ENGINE_LOG_MIN_FREE_BYTES) {
		lowDisk.rotationDisabled = true;
		if (!lowDisk.lowDiskLogged) {
			lowDisk.lowDiskLogged = true;
			process.stderr.write(
				`[ERROR] engine log rotation disabled: ${mount} has ${free} bytes free ` +
					`(minimum ${ENGINE_LOG_MIN_FREE_BYTES})\n`
			);
		}
		return false;
	}
	return true;
}

/** `20260924T011236Z`, derived from the UTC ISO form rather than typed by hand. */
export function archiveTimestamp(now: Date = new Date()): string {
	const iso = now.toISOString(); // 2026-09-24T01:12:36.906Z, always UTC
	return `${iso.slice(0, 4)}${iso.slice(5, 7)}${iso.slice(8, 10)}T${iso.slice(11, 13)}${iso.slice(14, 16)}${iso.slice(17, 19)}Z`;
}

export function archivePaths(logPath: string): string[] {
	const parent = path.dirname(logPath);
	const base = path.basename(logPath);
	let names: string[];
	try {
		names = fs.readdirSync(parent);
	} catch {
		return [];
	}
	return names
		.filter((name) => name !== base && name.startsWith(`${base}.`))
		.map((name) => path.join(parent, name));
}

function mtimeMs(file: string): number {
	try {
		return fs.statSync(file).mtimeMs;
	} catch {
		return 0;
	}
}

function oldest(files: string[]): string {
	let pick = files[0] as string;
	for (const file of files) {
		if (mtimeMs(file) < mtimeMs(pick)) pick = file;
	}
	return pick;
}

export function pruneArchives(logPath: string): void {
	const cutoff = Date.now() - ENGINE_LOG_RETENTION_DAYS * 24 * 60 * 60 * 1000;
	for (const archive of archivePaths(logPath)) {
		if (fs.statSync(archive).mtimeMs < cutoff) fs.rmSync(archive);
	}
	let archives = archivePaths(logPath);
	while (archives.length > ENGINE_LOG_MAX_ARCHIVE_COUNT) {
		fs.rmSync(oldest(archives));
		archives = archivePaths(logPath);
	}
	let total = archives.reduce((sum, file) => sum + fs.statSync(file).size, 0);
	while (total > ENGINE_LOG_MAX_TOTAL_ARCHIVE_BYTES && archives.length > 0) {
		const victim = oldest(archives);
		total -= fs.statSync(victim).size;
		fs.rmSync(victim);
		archives = archivePaths(logPath);
	}
}

/** Rotate the live log when it reaches `maxBytes`, or unconditionally with 0. */
export function rotateLogIfNeeded(logPath: string, maxBytes: number): void {
	let size: number;
	try {
		size = fs.statSync(logPath).size;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
		throw error;
	}
	if (size < maxBytes) return;
	if (!rotationAllowed(path.dirname(logPath))) return;
	fs.renameSync(logPath, `${logPath}.${archiveTimestamp()}`);
	pruneArchives(logPath);
}

/** Append bytes, rotating first when the live file would exceed the cap. */
export function appendRotated(logPath: string, bytes: Buffer | string): void {
	let existing = 0;
	try {
		existing = fs.statSync(logPath).size;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
	}
	const length = typeof bytes === 'string' ? Buffer.byteLength(bytes) : bytes.length;
	if (existing > 0 && existing + length > ENGINE_LOG_MAX_BYTES) {
		rotateLogIfNeeded(logPath, 0);
	}
	fs.appendFileSync(logPath, bytes);
}
