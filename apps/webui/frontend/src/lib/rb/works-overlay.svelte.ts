/**
 * Live works state for the table, and every works write the UI makes.
 *
 * All of it goes through the same routes as `opendj work`, so nothing here can
 * do what an agent cannot. Writes carry this app session's undo stack.
 */
import { API_BASE } from '$lib/api';
import { subscribe } from '$lib/api/events-bus';
import { pushToast } from '$lib/stores.svelte';
import {
	EMPTY_OVERLAY,
	normalizeOptionalColumns,
	tracksViaWork,
	type ClassicalValues,
	type OptionalColumn,
	type WorkRole,
	type WorksOverlay
} from './library-works';

const UNDO_SESSION = `webui-${crypto.randomUUID()}`;
const FLAGS_DEBOUNCE_MS = 400;
const OVERLAY_KINDS = new Set(['works', 'classical', 'undo']);

/** A typed refusal from the library organization routes. */
export class WorksError extends Error {
	constructor(
		readonly code: string,
		message: string,
		readonly details: Record<string, unknown>
	) {
		super(message);
	}
}

export type WorksModal =
	| { kind: 'editor'; stableIds: string[]; title: string }
	| { kind: 'link'; stableId: string }
	| { kind: 'merge'; sourceId: string; intoId: string; onmerged: () => void }
	| {
			kind: 'copy-unstack';
			member: StackMember;
			workId: string;
			playlistId: string;
			position: number | undefined;
	  };

export interface StackMember {
	member_kind: 'track' | 'edit';
	member_id: string;
	title: string | null;
	artist: string;
	role: WorkRole;
	role_label: string;
	is_primary: boolean;
	this_row: boolean;
	place: number | null;
}

export interface Suggestion {
	member_id: string | null;
	title: string | null;
	artist: string | null;
	work_id: string | null;
	work_title: string | null;
	reason_code: string;
	reason: string;
	confidence: 'high' | 'medium' | 'low';
}

export interface WorkRow {
	id: string;
	title: string;
	kind: 'classical' | 'song';
	composer: string | null;
	catalogue: string | null;
	movement: string | null;
	aliases: string;
}

export interface StackView {
	work: WorkRow | null;
	members: StackMember[];
	suggestions: Suggestion[];
}

export interface WriteOut {
	row: Record<string, unknown>;
	undo_id: string | null;
}

class WorksView {
	overlay = $state<WorksOverlay>(EMPTY_OVERLAY);
	suggestionIds = $state<ReadonlySet<string>>(new Set());
	optionalColumns = $state<OptionalColumn[]>([]);
	classicalLayout = $state(false);
	modal = $state<WorksModal | null>(null);
	/** The member being dragged out of a stack, while a drag is live. */
	stackDrag = $state<{ member: StackMember; workId: string } | null>(null);
	/** The newest undoable action in this session, named for the Undo control. */
	lastAction = $state<string | null>(null);
}

export const worksView = new WorksView();

let _viaWorkQuery = '';
let _viaWorkOverlay: WorksOverlay | null = null;
let _viaWorkHits = new Set<string>();

/** Tracks a query reaches through a work. Cached: this runs once per row searched. */
export function viaWorkHits(query: string): ReadonlySet<string> {
	const overlay = worksView.overlay;
	if (query !== _viaWorkQuery || overlay !== _viaWorkOverlay) {
		_viaWorkQuery = query;
		_viaWorkOverlay = overlay;
		_viaWorkHits = tracksViaWork(overlay, query);
	}
	return _viaWorkHits;
}

async function _send<T>(method: string, path: string, body?: unknown): Promise<T> {
	const write = method !== 'GET';
	const response = await fetch(`${API_BASE}/api/v1${path}`, {
		method,
		headers: {
			...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
			...(write ? { 'X-Undo-Session': UNDO_SESSION } : {})
		},
		...(body === undefined ? {} : { body: JSON.stringify(body) })
	});
	if (response.ok) return (await response.json()) as T;
	const detail = ((await response.json().catch(() => ({}))) as { detail?: unknown }).detail;
	if (typeof detail === 'object' && detail !== null && 'error' in detail) {
		const { error, message, ...details } = detail as Record<string, unknown>;
		throw new WorksError(String(error), String(message), details);
	}
	throw new WorksError('HTTP', `${method} ${path} failed (${response.status})`, {});
}

/** One write the UI performs. Refreshes the table and arms Undo with its name. */
async function _write<T extends { undo_id: string | null }>(
	action: string,
	method: string,
	path: string,
	body?: unknown
): Promise<T> {
	const result = await _send<T>(method, path, body);
	if (result.undo_id !== null) worksView.lastAction = action;
	await loadOverlay();
	return result;
}

export async function loadOverlay(): Promise<void> {
	worksView.overlay = await _send<WorksOverlay>('GET', '/works/overlay');
}

export async function loadWorkPrefs(): Promise<void> {
	const prefs = await _send<{
		library_optional_columns: unknown;
		library_classical_layout: boolean;
	}>('GET', '/ui-prefs');
	worksView.optionalColumns = normalizeOptionalColumns(prefs.library_optional_columns);
	worksView.classicalLayout = prefs.library_classical_layout;
}

export async function setOptionalColumn(col: OptionalColumn, on: boolean): Promise<void> {
	const next = new Set(worksView.optionalColumns);
	if (on) next.add(col);
	else next.delete(col);
	const list = normalizeOptionalColumns([...next]);
	await _send('PUT', '/ui-prefs', { library_optional_columns: list });
	worksView.optionalColumns = list;
}

export async function setClassicalLayout(on: boolean): Promise<void> {
	await _send('PUT', '/ui-prefs', { library_classical_layout: on });
	worksView.classicalLayout = on;
}

let _flagsTimer: ReturnType<typeof setTimeout> | null = null;

/** Debounced `▸ ?` lookup for the rows of the open list. */
export function refreshSuggestionFlags(stableIds: readonly string[]): void {
	if (_flagsTimer !== null) clearTimeout(_flagsTimer);
	_flagsTimer = setTimeout(() => {
		_flagsTimer = null;
		void _send<{ member_ids: string[] }>('POST', '/works/suggestion-flags', {
			member_ids: stableIds
		})
			.then((flags) => {
				worksView.suggestionIds = new Set(flags.member_ids);
			})
			.catch((exc: unknown) => pushToast(`version suggestions failed: ${String(exc)}`, 'error'));
	}, FLAGS_DEBOUNCE_MS);
}

/** Start following the library. Returns the unsubscribe. */
export function startWorksOverlay(): () => void {
	const fail = (exc: unknown): void => {
		pushToast(`works did not load: ${String(exc)}`, 'error');
	};
	void loadOverlay().catch(fail);
	void loadWorkPrefs().catch(fail);
	return subscribe('library.changed', (envelope) => {
		const kind = (envelope.payload as { kind?: string } | undefined)?.kind;
		if (kind !== undefined && OVERLAY_KINDS.has(kind)) void loadOverlay().catch(fail);
	});
}

export const fetchStack = (stableId: string, playlistId: string | null): Promise<StackView> =>
	_send(
		'GET',
		`/works/stack?${new URLSearchParams({
			member: stableId,
			...(playlistId === null ? {} : { playlist_id: playlistId })
		})}`
	);

export const fetchCandidates = (
	stableId: string,
	query: string
): Promise<{ rows: { member_id: string; title: string; artist: string; same_stem: boolean }[] }> =>
	_send('GET', `/works/link-candidates?${new URLSearchParams({ member: stableId, q: query })}`);

export const fetchWorks = (): Promise<{ rows: WorkRow[] }> => _send('GET', '/works');

export const fetchClassical = (stableId: string): Promise<{ values: ClassicalValues }> =>
	_send('GET', `/tracks/${encodeURIComponent(stableId)}/classical`);

export const fetchFileTags = (stableId: string): Promise<{ proposed: Record<string, unknown> }> =>
	_send('GET', `/tracks/${encodeURIComponent(stableId)}/classical/file-tags`);

export const linkTracks = (
	memberIds: string[],
	options: { workId?: string; title?: string; kind?: 'classical' | 'song'; role?: WorkRole } = {}
): Promise<WriteOut> =>
	_write('link versions', 'POST', '/works:link', {
		member_ids: memberIds,
		...(options.workId === undefined ? {} : { work_id: options.workId }),
		...(options.title === undefined ? {} : { title: options.title }),
		...(options.kind === undefined ? {} : { kind: options.kind }),
		...(options.role === undefined ? {} : { role: options.role })
	});

export const createWork = (fields: Record<string, unknown>): Promise<WriteOut> =>
	_write('create work', 'POST', '/works', fields);

export const updateWork = (workId: string, fields: Record<string, unknown>): Promise<WriteOut> =>
	_write('edit work', 'PATCH', `/works/${encodeURIComponent(workId)}`, fields);

export const mergeWorks = (sourceId: string, intoId: string): Promise<WriteOut> =>
	_write('merge works', 'POST', `/works/${encodeURIComponent(sourceId)}:merge`, { into: intoId });

export const moveMembers = (workId: string, memberIds: string[]): Promise<WriteOut> =>
	_write('reorder stack', 'POST', `/works/${encodeURIComponent(workId)}/members:move`, {
		member_ids: memberIds
	});

const _memberPath = (workId: string, memberId: string): string =>
	`/works/${encodeURIComponent(workId)}/members/track/${encodeURIComponent(memberId)}`;

export const unlinkMember = (workId: string, memberId: string): Promise<{ undo_id: string | null }> =>
	_write('unlink version', 'DELETE', _memberPath(workId, memberId));

export const setMemberRole = (workId: string, memberId: string, role: WorkRole): Promise<WriteOut> =>
	_write('change role', 'PATCH', _memberPath(workId, memberId), { role });

export const dismissSuggestion = (a: string, b: string): Promise<WriteOut> =>
	_write('dismiss suggestion', 'POST', '/works/dismissals', { a_member: a, b_member: b });

export const saveClassical = (stableId: string, values: ClassicalValues): Promise<WriteOut> =>
	_write('edit recording', 'PATCH', `/tracks/${encodeURIComponent(stableId)}/classical`, values);

export async function undoLast(): Promise<void> {
	const undone = await _send<{ op: string }>('POST', '/undo');
	worksView.lastAction = null;
	await loadOverlay();
	pushToast(`Undid ${undone.op}`, 'info');
}

/** Report a refused write in the words the server gave. */
export function reportWorksFailure(what: string, exc: unknown): void {
	pushToast(`${what}: ${exc instanceof Error ? exc.message : String(exc)}`, 'error');
}
