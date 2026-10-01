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

/** Home roots on macOS, Linux and Windows: `/Users/<name>`, `/home/<name>`,
 * `C:\Users\<name>`. The segment after the root is the account name and is
 * never shown. */
const HOME_ROOT = /^(?:\/(?:Users|home)\/[^/]+|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/]+)(?=$|[\\/])/;
/** Removable and network volumes: the volume name is what a DJ recognizes
 * ("the USB stick called MUSIC"), the mount root is not. */
const VOLUME_ROOT = /^(?:\/Volumes|\/media\/[^/]+|\/mnt)\/([^/]+)(?=$|\/)/;

/** True for anything that reads as an absolute filesystem path. */
export function isAbsolutePath(text: string): boolean {
	return /^(?:\/|[A-Za-z]:[\\/]|\\\\)/.test(text.trim());
}

/**
 * Shorten a path to what a user recognizes. EVERY absolute path is shortened:
 * home paths read `~/Music/...`, a volume reads by its name (`MUSIC/DJ`), and
 * anything else keeps only its last two folders behind an ellipsis, so a
 * system or temp root never reaches visible copy.
 */
export function shortenPath(path: string): string {
	const trimmed = path.trim();
	if (trimmed === '' || !isAbsolutePath(trimmed)) return trimmed;
	const home = HOME_ROOT.exec(trimmed);
	if (home !== null) {
		const rest = trimmed.slice(home[0].length).replace(/\\/g, '/');
		return truncateDeepPath(rest === '' ? '~' : `~${rest}`);
	}
	const volume = VOLUME_ROOT.exec(trimmed);
	if (volume !== null) {
		return truncateDeepPath(`${volume[1]}${trimmed.slice(volume[0].length)}`);
	}
	const parts = trimmed.split(/[\\/]+/).filter((part) => part !== '' && !/^[A-Za-z]:$/.test(part));
	if (parts.length === 0) return '...';
	return `.../${parts.slice(-2).join('/')}`;
}

/** Keep the head (`~` or a volume name) and the last few folders; ellipsize
 * the middle of a very deep path. */
export function truncateDeepPath(path: string, maxTailSegments = 3): string {
	const parts = path.split('/').filter(Boolean);
	if (parts.length <= maxTailSegments + 1) return path;
	return `${parts[0]}/.../${parts.slice(-maxTailSegments).join('/')}`;
}

/** True when visible copy must not echo this token. */
export function containsForbiddenHumanToken(text: string): boolean {
	if (/\/api\/v1\//.test(text)) return true;
	if (/\b[A-Z][A-Z0-9_]{2,}\b/.test(text) && /_/.test(text)) return true;
	if (/(?:^|[\s'"(])(?:\/(?:Users|home|data|var|tmp|private|opt|Volumes|mnt|Library|Applications)\/|[A-Za-z]:\\)/.test(text)) return true;
	if (/\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/.test(text)) return true;
	if (/\b[a-z]+(?:_[a-z0-9]+)+\b/.test(text)) return true;
	if (/\b(?:HTTP|GET|POST|PUT|PATCH|DELETE) \/|\b[1-5]\d\d\b.*\b(?:error|status)\b|Traceback|Exception|Errno/.test(text)) return true;
	if (/cannot import:/i.test(text)) return true;
	if (/pyrekordbox|sqlcipher|sqlite|demucs|torch|fastapi|uvicorn|openrouter|state\.db|master\.db/i.test(text)) return true;
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

/** Where the library lives, in words. The packaged app keeps it under the
 * OS's per-app support folder, which no DJ has ever opened on purpose, so it
 * is named rather than spelled out; a data dir the user chose is shortened. */
export function humanDataDirLabel(dataDir: string): string {
	if (/Application Support|AppData|\.local[\\/]share|com\.opendj\./i.test(dataDir)) {
		return "Open DJ's library folder";
	}
	return shortenPath(dataDir);
}

export function humanAdvanceRefusal(
	step: string,
	ctx: { source: 'rekordbox' | 'folder' | null },
	rawRefusal: string | null
): string | null {
	if (rawRefusal === null) return null;
	if (step === 'detect' && ctx.source === null) {
		return 'Choose where your music comes from first.';
	}
	if (step === 'detect' && ctx.source === 'folder') {
		if (rawRefusal === 'remove duplicate folder paths before importing') {
			return 'The same folder is listed twice. Remove one before importing.';
		}
		if (rawRefusal === 'no folder has been checked yet') {
			return 'Choose a folder and press Check this folder first.';
		}
		if (rawRefusal === 'macOS is blocking that folder; grant access and check again') {
			return 'macOS is blocking that folder. Grant access and check it again.';
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
		return 'This is the last step.';
	}
	return 'This step is not available yet.';
}

/**
 * Sentences this client writes itself, mapped to the copy an operator sees.
 */
const CLIENT_SENTENCES: Readonly<Record<string, string>> = {
	'type a folder path first': 'Type a folder path first.',
	'check at least one folder with audio files in it':
		'Check at least one folder with audio files in it first.',
	'check a folder with audio files in it first': 'Check a folder with audio files in it first.',
	'choose rekordbox import before starting': 'Choose to import your DJ collection first.'
};

/** Shown for any failure whose own words are not operator-safe. */
export const GENERIC_SETUP_ERROR = 'Something went wrong talking to the app. Try again in a moment.';

/** An absolute path inside a sentence, up to whitespace; trailing sentence
 * punctuation is not part of it. */
const PATH_IN_TEXT = /(?:\/(?:Users|home|Volumes|media|mnt)\/|[A-Za-z]:\\)[^\s'"]*[^\s'".,;:)]/g;

/**
 * Turn a thrown client message into operator-safe copy.
 *
 * The client's own sentences map through CLIENT_SENTENCES. A server sentence
 * is shown only when, after its paths are shortened, nothing internal is left
 * in it (macOS grant instructions are the case this exists for); a job id, an
 * error code, an endpoint or a library name anywhere sends the whole message
 * to the generic sentence, and the original always rides on errorDiagnostic.
 */
export function humanApiError(message: string): string {
	if (message.includes('setup API not offered')) return humanSetupMissing();
	if (message.includes('daemon not identified yet')) return humanSetupProbePending();
	const trimmed = message.trim();
	const known = CLIENT_SENTENCES[trimmed];
	if (known !== undefined) return known;
	if (/already running/i.test(trimmed)) {
		return 'An import is already running. Wait for it to finish, then try again.';
	}
	if (trimmed === '' || /\bjob-|\b[0-9a-f]{8,}\b/i.test(trimmed)) return GENERIC_SETUP_ERROR;
	const shortened = trimmed.replace(PATH_IN_TEXT, (path) => shortenPath(path));
	if (containsForbiddenHumanToken(shortened)) return GENERIC_SETUP_ERROR;
	return shortened;
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

export function humanStemsFailed(): string {
	return 'Stem separation did not run. You can finish setup and run it later from the library.';
}

/** Human label for an import stage. The stage id itself is a wire name and
 * only ever rides on data-agent-stage. */
export function humanStageLabel(stage: string, labels: Readonly<Record<string, string>>): string {
	const label = labels[stage];
	return label === undefined || label.trim() === '' ? 'Another import step' : label;
}
