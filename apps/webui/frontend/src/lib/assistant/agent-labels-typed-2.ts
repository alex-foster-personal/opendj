/**
 * Card sentences for OJ's second batch of typed tools (library care, deck
 * sync, stems, AutoPlay, browser and view). Same rules as agent-labels-typed.ts:
 * reads say what OJ is looking up, writes name their real arguments, a
 * missing argument reads as plain words. Pure: no DOM.
 */
import {
	between,
	called,
	clockText,
	deck,
	intent,
	playlist,
	quoted,
	track,
	tracks,
	valueText,
	type ToolArgs
} from './agent-labels-typed';

//----- argument words

const MAX_PATH_CHARS = 60;

/** 0.8 reads "80%"; a missing level reads "a new level". */
function percent(value: unknown): string {
	return typeof value === 'number' ? `${Math.round(value * 100)}%` : 'a new level';
}

/** 2 reads "+2", -1.5 reads "-1.5". */
function signed(value: number): string {
	const rounded = Math.round(value * 10) / 10;
	return rounded > 0 ? `+${rounded}` : `${rounded}`;
}

function plural(value: number, one: string, many: string): string {
	return `${value} ${Math.abs(value) === 1 ? one : many}`;
}

/** One id as "1 <one>", many as "3 <many>", none as "<many>". */
function some(value: unknown, one: string, many: string): string {
	return Array.isArray(value) && value.length ? plural(value.length, one, many) : many;
}

/** `Turn on X`, `Turn off X`, or `Switch X` when on/off is missing. */
function switched(enabled: unknown, what: string): string {
	if (enabled === true) return `Turn on ${what}`;
	if (enabled === false) return `Turn off ${what}`;
	return `Switch ${what}`;
}

/** A path keeps its end (the file name), not its start. */
function pathText(value: unknown): string {
	if (typeof value !== 'string' || !value) return 'a new path';
	return value.length > MAX_PATH_CHARS ? `"...${value.slice(3 - MAX_PATH_CHARS)}"` : `"${value}"`;
}

function stem(value: unknown): string {
	return typeof value === 'string' ? `the ${value} stem` : 'a stem';
}

function group(value: unknown): string {
	return called('duplicate group', value);
}

function pair(out: unknown, into: unknown): string {
	return `from ${track(out)} into ${track(into)}`;
}

function planTrack(plan: unknown, side: 'out' | 'in'): unknown {
	const end = (plan as Record<string, unknown> | null)?.[side];
	return typeof end === 'object' && end !== null
		? (end as Record<string, unknown>).track
		: undefined;
}

function editFields(a: ToolArgs): string {
	const parts: string[] = [];
	if (typeof a.rating === 'number') parts.push(`rating ${plural(a.rating, 'star', 'stars')}`);
	if (typeof a.genre === 'string') parts.push(`genre ${quoted(a.genre, '""')}`);
	if (typeof a.comments === 'string') parts.push(`comment ${quoted(a.comments, '""')}`);
	if (typeof a.notes === 'string') parts.push(`notes ${quoted(a.notes, '""')}`);
	if (Array.isArray(a.tags_add))
		parts.push(`add ${some(a.tags_add, 'tag', 'tags')} ${quotedList(a.tags_add)}`);
	if (Array.isArray(a.tags_remove))
		parts.push(`remove ${some(a.tags_remove, 'tag', 'tags')} ${quotedList(a.tags_remove)}`);
	return parts.length ? `: ${parts.join(', ')}` : '';
}

function quotedList(values: unknown[]): string {
	return values.map((value) => quoted(value, valueText(value))).join(', ');
}

const RELATED_CRITERIA = ['key', 'genre', 'color', 'rating', 'mytag', 'artist'] as const;

function relatedCriteria(a: ToolArgs): string {
	const parts: string[] = [];
	if (typeof a.bpm_percent === 'number') parts.push(`BPM within ${a.bpm_percent}%`);
	for (const name of RELATED_CRITERIA) {
		if (typeof a[name] === 'boolean') parts.push(`${name} ${a[name] ? 'on' : 'off'}`);
	}
	return parts.length ? `: ${parts.join(', ')}` : '';
}

function keySyncTarget(a: ToolArgs): string {
	const target = typeof a.target === 'number' ? deck(a.target) : 'the master deck';
	return `${target}${typeof a.mode === 'string' ? ` (${a.mode})` : ''}`;
}

/** "hot_cues" reads "hot cues". */
function spaced(value: string): string {
	return value.replaceAll('_', ' ');
}

const GRID_LINES_WORDS: Record<string, string> = {
	beats_bars: 'beat and bar lines',
	bars: 'bar lines only',
	off: 'off'
};

function waveformDesign(a: ToolArgs): string {
	const style = typeof a.design === 'string' ? ` in the ${a.design} style` : '';
	const height = typeof a.height === 'string' ? ` at ${a.height} height` : '';
	return style || height ? `Draw waveforms${style}${height}` : 'Change how waveforms are drawn';
}

function waveformOverlay(a: ToolArgs): string {
	if (a.overlay === 'grid_lines') {
		const words = typeof a.value === 'string' ? GRID_LINES_WORDS[a.value] : undefined;
		return `Set the waveform grid lines to ${words ?? 'a new setting'}`;
	}
	const what =
		typeof a.overlay === 'string'
			? `the ${spaced(a.overlay)} waveform overlay`
			: 'a waveform overlay';
	return switched(a.enabled, what);
}

function waveZoom(a: ToolArgs): string {
	const zoom = typeof a.zoom === 'number' ? String(a.zoom) : a.zoom;
	const span =
		zoom === 'whole' ? 'the whole track' : typeof zoom === 'string' ? `${zoom} bars` : '';
	const on = typeof a.deck === 'number' ? ` on ${deck(a.deck)}` : '';
	return span ? `Zoom the waveform${on} to ${span}` : `Change the waveform zoom${on}`;
}

function layoutPreset(a: ToolArgs): string {
	const screen = typeof a.surface === 'string' ? ` on the ${a.surface} screen` : '';
	if (a.preset === 'auto') return `Use the skin's own layout${screen}`;
	if (typeof a.preset === 'string') return `Use the ${a.preset} layout${screen}`;
	return `Change the screen layout${screen}`;
}

function skinParts(a: ToolArgs): string {
	const parts: string[] = [];
	if (typeof a.wave_palette === 'string' && a.wave_palette !== 'auto')
		parts.push(`${a.wave_palette} waveform colours`);
	if (a.wave_split_master === 'on' || a.wave_split_master === 'off')
		parts.push(`split master waveform ${a.wave_split_master}`);
	return parts.length ? `: ${parts.join(', ')}` : '';
}

//----- the table

/** One sentence per typed tool of the second batch, from its call arguments. */
export const TYPED_TOOL_LABEL_2: Record<string, (a: ToolArgs) => string> = {
	// track_edit.py
	track_update: (a) => `Change ${track(a.stable_id)}${editFields(a)}`,
	tracks_bulk_edit: (a) => `Change ${tracks(a.stable_ids)}${editFields(a)}`,
	// history.py
	play_history: () => 'Looking up your play history',
	// similar.py
	genre_list: (a) => `Looking up the genres of ${tracks(a.stable_ids)}`,
	tracks_similar: (a) => `Looking up tracks like ${track(a.stable_id)}`,
	// dedup.py
	dedup_clusters: () => 'Looking up duplicate tracks',
	dedup_decide: (a) => {
		if (a.action === 'merge')
			return `Mark ${group(a.cluster_id)} to merge into ${track(a.survivor)}`;
		if (a.action === 'keep-all') return `Mark ${group(a.cluster_id)} to keep every copy`;
		if (a.action === 'skip') return `Skip ${group(a.cluster_id)} for now`;
		return `Decide ${group(a.cluster_id)}`;
	},
	dedup_apply: (a) =>
		`Merge ${group(a.cluster_id)} into ${track(a.survivor)}${a.confirm_cue_loss === true ? ', accepting lost cues' : ''}`,
	dedup_undo: (a) => `Undo the merge of ${group(a.cluster_id)}`,
	// title_fix.py
	title_fix_proposals: (a) =>
		`Looking up title fixes${typeof a.state === 'string' && a.state !== 'any' ? ` (${a.state})` : ''}`,
	title_fix_apply: (a) => `Apply ${some(a.proposal_ids, 'title fix', 'title fixes')}`,
	title_fix_reject: (a) => `Reject ${some(a.proposal_ids, 'title fix', 'title fixes')}`,
	title_fix_undo: (a) =>
		`Undo ${called('title-fix batch', a.batch_id)}${a.skip_conflicts === true ? ', skipping changed tracks' : ''}`,
	// beatgrid.py
	beatgrid_flags: (a) =>
		`Looking up beatgrid flags${typeof a.grid_class === 'string' && a.grid_class !== 'all' ? ` (${a.grid_class})` : ''}`,
	beatgrid_flag_dismiss: (a) =>
		`${a.dismissed === false ? 'Restore' : 'Dismiss'} the beatgrid flag on ${track(a.stable_id)}`,
	// relocate.py
	relocate_candidates: (a) => `Looking up where ${track(a.stable_id)} moved to`,
	relocate_apply: (a) => `Relink ${track(a.stable_id)} to ${pathText(a.new_path)}`,
	// pair_transitions.py
	pair_transition_get: (a) => `Looking up the saved transition ${pair(a.out, a.in)}`,
	pair_transition_save: (a) =>
		`Save the transition ${pair(planTrack(a.plan, 'out'), planTrack(a.plan, 'in'))}${typeof a.style === 'string' ? ` (${a.style})` : ''}`,
	pair_transition_forget: (a) => `Forget the saved transition ${pair(a.out, a.in)}`,
	// gain.py
	track_gain: (a) => `Looking up the gain of ${track(a.stable_id)}`,
	track_gain_offset_set: (a) =>
		typeof a.offset_db === 'number'
			? `Set the gain of ${track(a.stable_id)} to ${signed(a.offset_db)} dB`
			: a.offset_db === null
				? `Clear your gain offset on ${track(a.stable_id)}`
				: `Change the gain offset of ${track(a.stable_id)}`,
	// export.py
	playlist_export_text: (a) => `Looking up ${playlist(a.playlist_id)} as text`,
	set_tracklist: (a) => `Looking up the tracklist of ${called('recorded set', a.session_id)}`,
	// cloud_sync.py
	cloudsync_status: () => 'Looking up cloud sync',
	playlist_pin_list: (a) =>
		`Looking up playlist pins${a.machine_id === undefined ? '' : ` on ${called('machine', a.machine_id)}`}`,
	playlist_pin_set: (a) =>
		`Set ${playlist(a.playlist_id)} to ${typeof a.mode === 'string' ? a.mode : 'a new mode'} on ${called('machine', a.machine_id)}`,
	// deck_sync.py
	deck_beat_sync: (a) => switched(a.enabled, `beat sync for ${deck(a.deck)}`),
	deck_key_shift: (a) =>
		typeof a.semitones === 'number'
			? `Shift the key of ${deck(a.deck)} by ${signed(a.semitones)} ${Math.abs(a.semitones) === 1 ? 'semitone' : 'semitones'}`
			: `Shift the key of ${deck(a.deck)}`,
	deck_key_sync: (a) =>
		a.enabled === false
			? `Turn off key sync for ${deck(a.deck)}`
			: `Key sync ${deck(a.deck)} to ${keySyncTarget(a)}`,
	deck_master: (a) => `Make ${deck(a.deck)} the master deck`,
	deck_tempo: (a) =>
		typeof a.ratio === 'number'
			? `Set the tempo of ${deck(a.deck)} to ${signed((a.ratio - 1) * 100)}%`
			: `Change the tempo of ${deck(a.deck)}`,
	// autoplay.py
	autoplay_set: (a) => switched(a.enabled, 'AutoPlay'),
	autoplay_arm_next: (a) =>
		`Arm AutoPlay's next transition${between(a)}${intent(a)}${typeof a.from_ms === 'number' ? ` at ${clockText(a.from_ms)}` : ''}`,
	autoplay_decide_now: () => 'Have AutoPlay pick the next track now',
	autoplay_cancel_next: () => "Cancel AutoPlay's next transition",
	autoplay_decision: () => "Looking up AutoPlay's next pick",
	// stem_mix.py
	stem_level: (a) => `Set ${stem(a.stem)} on ${deck(a.deck)} to ${percent(a.value)}`,
	stem_mute: (a) => `${a.enabled === false ? 'Unmute' : 'Mute'} ${stem(a.stem)} on ${deck(a.deck)}`,
	stem_solo: (a) =>
		`${a.enabled === false ? 'Stop soloing' : 'Solo'} ${stem(a.stem)} on ${deck(a.deck)}`,
	// browser.py
	browser_related: (a) =>
		a.active === false ? 'Leave the Related tracks tab' : 'Show the Related tracks tab',
	browser_related_criteria: (a) => `Change what Related tracks match${relatedCriteria(a)}`,
	browser_show_playlist: (a) =>
		`Show ${playlist(a.playlist_id)} in the browser${typeof a.stable_id === 'string' ? `, selecting ${track(a.stable_id)}` : ''}`,
	preview_play: (a) =>
		`Preview ${track(a.stable_id)} in the headphones${a.ratio === 0 ? ' from the start' : typeof a.ratio === 'number' ? ` from ${percent(a.ratio)} in` : ''}`,
	preview_stop: () => 'Stop the headphone preview',
	// view.py
	view_waveform_design: waveformDesign,
	view_waveform_overlay: waveformOverlay,
	view_wave_zoom: waveZoom,
	view_layout_preset: layoutPreset,
	view_skin: (a) =>
		`Switch to ${typeof a.skin === 'string' ? `the ${a.skin} skin` : 'a new skin'}${skinParts(a)}`
};
