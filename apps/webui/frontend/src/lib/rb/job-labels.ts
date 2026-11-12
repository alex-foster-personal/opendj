/**
 * Plain-word names for engine job kinds, shown as the jobs drawer row title.
 *
 * The raw kind stays an agent contract (row tooltip and data-agent-kind);
 * operators read the words below. A kind with no row falls back to its raw
 * name and logs once, so a missing label shows up in review.
 */

export type JobLabelInput = { kind: string; payload?: { [key: string]: unknown } | null };

/** Every kind the engine registers via register_worker today. The unit test
 *  cross-checks this list against the Python registrations. */
export const ENGINE_JOB_KINDS = [
	'anlz.cache_rebuild',
	'cloud.hydrate',
	'setup.import-rekordbox',
	'stems.separate'
] as const;
export type EngineJobKind = (typeof ENGINE_JOB_KINDS)[number];

type Payload = JobLabelInput['payload'];

//-----------------------------------------------------------------------------
function setupImportLabel(kind: string, payload: Payload): string {
	const mode = payload?.mode;
	if (mode === 'folder') {
		return 'Import music folder';
	} else if (mode === 'rekordbox' || mode === undefined) {
		return 'Import rekordbox library';
	} else {
		console.error('[job-labels] unknown setup import mode', mode);
		return kind;
	}
}

/** One row per kind. */
export const JOB_LABELS: Record<EngineJobKind, (payload: Payload) => string> = {
	'setup.import-rekordbox': (payload) => setupImportLabel('setup.import-rekordbox', payload),
	'stems.separate': () => 'Separate stems',
	'anlz.cache_rebuild': () => 'Rebuild analysis cache',
	'cloud.hydrate': () => 'Download track from cloud'
};

const reportedUnknownKinds = new Set<string>();

//-----------------------------------------------------------------------------
export function jobDisplayLabel(job: JobLabelInput): string {
	if (Object.hasOwn(JOB_LABELS, job.kind)) {
		return JOB_LABELS[job.kind as EngineJobKind](job.payload);
	}
	if (!reportedUnknownKinds.has(job.kind)) {
		reportedUnknownKinds.add(job.kind);
		console.error('[job-labels] no plain-word label for job kind', job.kind);
	}
	return job.kind;
}
