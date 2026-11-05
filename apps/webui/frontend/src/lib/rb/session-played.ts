/**
 * SESSION-PLAYED-01: the session played set behind the green library rows.
 *
 * The engine owns the set on disk (`/api/v1/session/played`), so a crash or
 * relaunch brings it back. This tracker mirrors it in the page:
 * - boot reads it; a live session paints at once,
 * - an expired one (idle over 12 h) paints nothing and HOLDS new loads until
 *   the DJ picks Restore or New session, so nothing is revived or dropped
 *   silently,
 * - every deck load is posted once.
 *
 * Pure: no `$lib` imports, so node unit tests load it directly. The rune
 * wrapper lives in `session-played.svelte.ts`.
 */

export interface SessionPlayedDoc {
	session_id: string | null;
	started_at_ms: number | null;
	last_activity_ms: number | null;
	expired: boolean;
	count: number;
	played_ids: string[];
}

export interface SessionPlayedApi {
	get: () => Promise<SessionPlayedDoc>;
	add: (stableId: string) => Promise<SessionPlayedDoc>;
	resume: () => Promise<SessionPlayedDoc>;
	startNew: () => Promise<SessionPlayedDoc>;
}

export type SessionPlayedPhase = 'loading' | 'live' | 'expired' | 'error';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** "Wed 20:14" in local time: the weekday is the checksum on the prompt. */
export function formatSessionWhen(ms: number): string {
	const at = new Date(ms);
	const hh = String(at.getHours()).padStart(2, '0');
	const mm = String(at.getMinutes()).padStart(2, '0');
	return `${WEEKDAYS[at.getDay()]} ${hh}:${mm}`;
}

/** Ids a deck may hold that are not library tracks. */
export function isRecordableStableId(id: string): boolean {
	return id.trim() !== '' && !id.startsWith('spotify-pending:');
}

export class SessionPlayedTracker {
	phase: SessionPlayedPhase = 'loading';
	ids = new Set<string>();
	doc: SessionPlayedDoc | null = null;
	error: string | null = null;
	private held: string[] = [];
	private chain: Promise<void> = Promise.resolve();
	private readonly api: SessionPlayedApi;
	private readonly onChange: () => void;

	constructor(api: SessionPlayedApi, onChange: () => void = () => {}) {
		this.api = api;
		this.onChange = onChange;
	}

	/** What the library paints green: nothing while the restore prompt is open. */
	get visibleIds(): ReadonlySet<string> {
		return this.phase === 'live' ? this.ids : new Set<string>();
	}

	async boot(): Promise<void> {
		try {
			this.apply(await this.api.get());
		} catch (exc) {
			this.phase = 'error';
			this.error = exc instanceof Error ? exc.message : String(exc);
			this.onChange();
			return;
		}
		if (this.phase === 'live') this.flushHeld();
	}

	record(stableId: string): void {
		if (!isRecordableStableId(stableId) || this.ids.has(stableId) || this.held.includes(stableId)) return;
		if (this.phase !== 'live') {
			this.held.push(stableId);
			return;
		}
		this.post(stableId);
	}

	async resume(): Promise<void> {
		this.apply(await this.api.resume());
		this.flushHeld();
		await this.settled();
	}

	async startNew(): Promise<void> {
		this.apply(await this.api.startNew());
		this.flushHeld();
		await this.settled();
	}

	/** Resolves once every posted load has been answered. */
	settled(): Promise<void> {
		return this.chain;
	}

	private apply(doc: SessionPlayedDoc): void {
		this.doc = doc;
		this.ids = new Set(doc.played_ids);
		this.phase = doc.expired ? 'expired' : 'live';
		this.error = null;
		this.onChange();
	}

	private flushHeld(): void {
		const held = this.held;
		this.held = [];
		for (const id of held) if (!this.ids.has(id)) this.post(id);
	}

	private post(stableId: string): void {
		this.ids = new Set([...this.ids, stableId]);
		this.onChange();
		this.chain = this.chain.then(async () => {
			try {
				this.apply(await this.api.add(stableId));
			} catch (exc) {
				this.error = `session played: could not record ${stableId}: ${exc instanceof Error ? exc.message : String(exc)}`;
				this.onChange();
			}
		});
	}
}
