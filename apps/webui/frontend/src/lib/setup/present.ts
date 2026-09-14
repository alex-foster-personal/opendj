/**
 * Human vs agent presentation for first-run setup.
 *
 * Sanitizes only what is RENDERED to a nontechnical operator. Request payloads,
 * API calls and stored paths stay canonical; agent diagnostics live in
 * data-agent-* attributes or a closed "Details for agents" disclosure.
 */

import type { components } from '../api-types';

type RekordboxDetection = components['schemas']['RekordboxDetectionOut'];
type FolderScan = components['schemas']['FolderScanOut'];
type Permissions = components['schemas']['PermissionsOut'];

/** Label for the collapsed agent diagnostics block. Spelled once for tests. */
export const AGENT_DETAILS_LABEL = 'Details for agents';

const HOME_PREFIXES = ['/Users/', '/home/'];

/** Shorten an absolute path to a home-relative form such as ~/Music/... */
export function shortenPath(path: string): string {
	const trimmed = path.trim();
	if (trimmed === '') return trimmed;
	for (const prefix of HOME_PREFIXES) {
		if (!trimmed.startsWith(prefix)) continue;
		const rest = trimmed.slice(prefix.length);
		const slash = rest.indexOf('/');
		if (slash === -1) return '~';
		return `~${rest.slice(slash)}`;
	}
	return truncateDeepPath(trimmed);
}

/** Keep recognizable folder names; ellipsize very deep paths. */
export function truncateDeepPath(path: string, maxTailSegments = 3): string {
	const parts = path.split('/').filter(Boolean);
	if (parts.length <= maxTailSegments + 1) return path;
	const tail = parts.slice(-maxTailSegments).join('/');
	return `.../${tail}`;
}

/** True when visible copy must not echo this token. */
export function containsForbiddenHumanToken(text: string): boolean {
	if (/\/api\/v1\//.test(text)) return true;
	if (/\b[A-Z][A-Z0-9_]{2,}\b/.test(text) && /_/.test(text)) return true;
	if (/^\/(?:Users|home|data|var|tmp)\//.test(text.trim())) return true;
	if (/cannot import:/i.test(text)) return true;
	if (/rekordbox_not_found|rekordbox_key|pyrekordbox|sqlcipher/i.test(text)) return true;
	return false;
}

export function humanSetupMissing(): string {
	return 'Setup is not available in this version of the app.';
}

export function humanSetupProbePending(): string {
	return 'Still connecting to the app...';
}

export function humanLegacySetupRefusal(): string {
	return humanSetupMissing();
}

export function humanBlockerSentence(code: string, detection: RekordboxDetection): string {
	if (code === 'rekordbox_not_found') {
		return 'No DJ collection was found on this machine.';
	}
	if (code === 'rekordbox_key_unavailable') {
		return 'The collection is locked and cannot be read yet.';
	}
	if (code === 'rekordbox_share_missing') {
		return (
			'Waveform data was not found. Tracks can still be imported, ' +
			'but waveforms and beatgrids may be missing until that data is available.'
		);
	}
	if (code === 'rekordbox_decrypt_failed') {
		return 'The collection could not be unlocked for import.';
	}
	if (code === 'rekordbox_ingest_failed') {
		return 'The import could not be prepared.';
	}
	return 'This import cannot start yet.';
}

export function agentBlockerDetail(code: string, detection: RekordboxDetection): string {
	if (code === 'rekordbox_not_found') {
		return `code=${code} live_db=${detection.live_db.path}`;
	}
	if (code === 'rekordbox_key_unavailable') {
		return `code=${code} key_detail=${detection.key_detail}`;
	}
	if (code === 'rekordbox_share_missing') {
		return `code=${code} share_dir=${detection.share_dir.path}`;
	}
	return code;
}

export function humanFolderVerdict(scan: FolderScan): string {
	if (scan.denied) {
		return `This folder cannot be read yet. ${scan.how_to_grant}`;
	}
	const label = shortenPath(scan.path);
	if (!scan.exists) return `Nothing was found at ${label}.`;
	if (!scan.readable) return `${label} could not be read. Check folder permissions and try again.`;
	if (scan.audio_files === 0) return `${label} is readable but holds no audio files.`;
	const placeholders =
		scan.icloud_placeholders > 0
			? ` ${scan.icloud_placeholders} iCloud-only files were skipped.`
			: '';
	return `${scan.audio_files} audio file${scan.audio_files === 1 ? '' : 's'} found.${placeholders}`;
}

export function humanAccessCaveat(permissions: Permissions | null): string | null {
	const denied = permissions?.denied ?? [];
	if (denied.length === 0) return null;
	return (
		`Some music folders could not be read (${denied.length}), so any count below ` +
		'covers only what was accessible, not your whole library.'
	);
}

export function agentAccessDetail(permissions: Permissions | null): string | null {
	const denied = permissions?.denied ?? [];
	if (denied.length === 0) return null;
	return denied.join(', ');
}

export function humanProbeLabel(label: string, exists: boolean): string {
	return exists ? `${label}: found` : `${label}: not found`;
}

export function humanKeyLine(keyAvailable: boolean): string {
	return keyAvailable ? 'Collection unlock: ready' : 'Collection unlock: not available';
}

export function humanImportSourceLabel(encrypted: boolean | null | undefined): string {
	if (encrypted === true) return 'A saved copy of your collection is ready to import (encrypted).';
	if (encrypted === false) return 'A saved copy of your collection is ready to import.';
	return 'Your collection is ready to import.';
}

export function humanDataDirLabel(dataDir: string): string {
	return shortenPath(dataDir);
}

export function humanAdvanceRefusal(
	step: string,
	ctx: {
		source: 'rekordbox' | 'folder';
		folderScan: FolderScan | null;
		detection: RekordboxDetection | null;
		job: { status: string } | null;
	},
	rawRefusal: string | null
): string | null {
	if (rawRefusal === null) return null;
	if (step === 'detect' && ctx.source === 'folder') {
		if (rawRefusal === 'no folder has been checked yet') {
			return 'Choose a folder and press Check this folder first.';
		}
		if (rawRefusal === 'macOS is blocking that folder; grant access and check again') {
			return rawRefusal;
		}
		if (rawRefusal.startsWith('nothing importable in ')) {
			return 'That folder has nothing we can import yet.';
		}
	}
	if (step === 'detect' && ctx.source === 'rekordbox') {
		if (rawRefusal === 'detection has not answered yet') {
			return 'Still looking for your music...';
		}
		if (rawRefusal.startsWith('cannot import:')) {
			return 'This import cannot start yet.';
		}
	}
	if (step === 'progress') {
		if (rawRefusal === 'no import has been started yet') {
			return 'The import has not started yet.';
		}
		if (rawRefusal.startsWith('import is ')) {
			return 'The import is still running.';
		}
		if (rawRefusal.startsWith('import ')) {
			return 'The import did not finish successfully. Try again before continuing.';
		}
	}
	if (step === 'done' && rawRefusal === 'this is the last step') {
		return rawRefusal;
	}
	return 'This step is not available yet.';
}

/** Turn a thrown client message into operator-safe copy. */
export function humanApiError(message: string): string {
	if (message.includes('setup API not offered')) return humanSetupMissing();
	if (message.includes('daemon not identified yet')) return humanSetupProbePending();
	if (containsForbiddenHumanToken(message)) {
		return 'Something went wrong talking to the app. Try again in a moment.';
	}
	return message;
}

export function agentApiError(message: string): string {
	return message;
}

export function humanEscapeTitle(id: 'redetect' | 'folder' | 'dismiss'): string {
	if (id === 'redetect') return 'Look for your music again on this machine.';
	if (id === 'folder') return 'Import a folder of audio files instead.';
	return 'Close setup and use the app with whatever is already in the library.';
}

export function agentEscapeEndpoint(id: 'redetect' | 'folder' | 'dismiss'): string {
	if (id === 'redetect') return 'GET /api/v1/setup/detect/rekordbox';
	if (id === 'folder') return 'GET /api/v1/setup/detect/folder';
	return 'POST /api/v1/setup/dismiss';
}

export function humanScanningSentence(): string {
	return 'Looking for your music on this machine...';
}

export function humanImportJobStatus(status: string): string {
	if (status === 'running') return 'Import in progress';
	if (status === 'queued') return 'Import queued';
	if (status === 'succeeded') return 'Import finished';
	if (status === 'failed') return 'Import failed';
	if (status === 'cancelled') return 'Import cancelled';
	if (status === 'cancelling') return 'Import stopping';
	return 'Import status updating';
}

export function humanImportJobMessage(message: string | null | undefined): string | null {
	if (message === null || message === undefined || message.trim() === '') return null;
	const trimmed = message.trim();
	if (containsForbiddenHumanToken(trimmed)) return 'Working through your library...';
	if (/\/(?:Users|home)\//.test(trimmed)) return 'Working through your library...';
	if (/^(setup import|ingest:|detect:|snapshot:|decrypt:|analysis:|scan:)/i.test(trimmed)) {
		return 'Working through your library...';
	}
	if (/job-/i.test(trimmed)) return 'Working through your library...';
	return trimmed;
}

export function humanStemsJobsUnavailable(): string {
	return 'Stem separation is not available in this version of the app.';
}

export function humanStemsPlanLoadError(): string {
	return (
		'Could not work out what stem separation would involve, so nothing is being offered yet.'
	);
}

export function humanStemsPlanTimeout(): string {
	return 'Working out stem separation is taking longer than expected. Try again in a moment.';
}

export function humanStemsBlocked(localExecutor: boolean): string {
	return localExecutor
		? 'Local stem separation is not available on this machine.'
		: 'Stem separation is not available in this build.';
}

export function humanStemsEnqueueError(): string {
	return 'Could not start stem separation. Try again in a moment.';
}

export function humanAssistantStatusError(): string {
	return 'The assistant is not available right now. Setup can continue without it.';
}
