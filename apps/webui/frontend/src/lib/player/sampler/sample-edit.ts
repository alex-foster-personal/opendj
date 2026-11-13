/**
 * Edge sampler edit modal: the pure rules ((private path) 7.4, 3.2; issue #11126).
 * No runes, no DOM: EditSampleModal feeds a slot in and sends what these return.
 *
 * Requirements (mini-PRD):
 *   ✔︎ a rename never hides the source: the source line always names the track title
 *     [if] a slot is renamed to "Big Drop" [then] its source line still reads its track title [else ⛔️]
 *   ✔︎ a name or description edit sends a PATCH only when it changed; a name equal to the title clears the rename
 *     [if] the name draft is the track title [then] the patch is { name: null } [else ⛔️]
 *     [if] the draft is the current value [then] no patch is sent [else ⛔️]
 *   ✔︎ Escape cancels an edit, Enter saves the name (a description takes newlines)
 *     [if] Escape is pressed in the name input [then] the action is cancel and the old name stays [else ⛔️]
 *   ✔︎ the captured mix reads as chips naming only its non-neutral parts, plus Master Tempo
 *     [if] a capture cut low EQ by 12 dB and muted vocals [then] chips read EQ low -12 dB and Vocals muted [else ⛔️]
 *   ✔︎ a settings change sends the full settings; One shot plays once, leaving it loops again
 *     [if] Mode goes from One shot to Loop [then] repeat goes from once back to loop [else ⛔️]
 *     [if] the downbeat is nudged past 3 beats [then] it stays at 3 [else ⛔️]
 */

import type { SlotPatch } from './api-sampler';
import type { KeyPlan } from './sampler-store-logic';
import {
	regionBars,
	type SamplerSlot,
	type SamplerVector,
	type SlotSettings
} from './sampler-types';

/** Longest sample name the modal's input takes (mock `maxLength`). */
export const NAME_MAX = 60;
/** Longest description the modal's textarea takes. */
export const DESCRIPTION_MAX = 200;

export type EditField = 'name' | 'description';
type EditKeyAction = 'save' | 'cancel' | null;

/** Where a slot came from, in the source line's words. */
function originLabel(slot: Pick<SamplerSlot, 'cue_letter' | 'source'>): string {
	switch (slot.source) {
		case 'loop_hot_cue':
			return `hot cue ${slot.cue_letter ?? '?'}`;
		case 'memory_loop':
			return 'memory loop';
		case 'capture':
			return 'deck loop, dragged in';
		case 'starter':
			return 'starter sample';
		default: {
			const never: never = slot.source;
			throw new Error(`Unhandled slot source: ${String(never)}`);
		}
	}
}

/** The source line after "Source:": the track title (shown bold) and the rest, whatever the rename. */
export function sourceLine(slot: SamplerSlot): { title: string; rest: string } {
	const bars = regionBars(slot);
	const parts = [
		slot.artist,
		originLabel(slot),
		`${bars} ${bars === 1 ? 'bar' : 'bars'}`,
		`${slot.native_bpm.toFixed(1)} BPM${slot.key ? ` ${slot.key}` : ''}`
	].filter((p) => p !== '');
	return { title: slot.title, rest: parts.map((p) => ` . ${p}`).join('') };
}

/** The PATCH a finished name or description edit sends; null when nothing changed. */
export function editPatch(slot: SamplerSlot, field: EditField, draft: string): SlotPatch | null {
	const v = draft.trim();
	if (field === 'name') {
		const name = v !== '' && v !== slot.title ? v : null;
		return name === slot.name ? null : { name };
	} else if (field === 'description') {
		return v === slot.description ? null : { description: v };
	}
	const never: never = field;
	throw new Error(`Unhandled edit field: ${String(never)}`);
}

/** What a key does in the name or description input: save, cancel (old value stays) or nothing. */
export function editKeyAction(field: EditField, key: string): EditKeyAction {
	if (key === 'Escape') return 'cancel';
	if (key === 'Enter' && field === 'name') return 'save';
	return null;
}

const db = (v: number): string => `${v > 0 ? '+' : ''}${Math.round(v * 10) / 10} dB`;

/** Chips for the captured mix (Trim, EQ, Filter, stems, key, MT); only non-neutral parts. */
export function vectorChips(v: SamplerVector): string[] {
	const chips: string[] = [];
	if (v.trim_db !== 0) chips.push(`Trim ${db(v.trim_db)}`);
	for (const band of ['low', 'mid', 'high'] as const)
		if (v.eq_db[band] !== 0) chips.push(`EQ ${band} ${db(v.eq_db[band])}`);
	if (v.filter !== 0)
		chips.push(`Filter ${v.filter < 0 ? 'LP' : 'HP'} ${Math.round(Math.abs(v.filter) * 100)}%`);
	for (const [part, st] of Object.entries(v.stems ?? {})) {
		const label = part[0].toUpperCase() + part.slice(1);
		if (st.mute) chips.push(`${label} muted`);
		else if (st.solo) chips.push(`${label} solo`);
		else if (st.gain_db !== 0) chips.push(`${label} ${db(st.gain_db)}`);
	}
	if (v.key_shift_st !== 0) chips.push(`Key ${v.key_shift_st > 0 ? '+' : ''}${v.key_shift_st} st`);
	chips.push(`Master Tempo ${v.master_tempo ? 'on' : 'off'}`);
	return chips;
}

const bpmText = (masterBpm: number | null): string =>
	masterBpm !== null ? `${masterBpm.toFixed(1)} BPM` : 'its own BPM';
const keyText = (plan: KeyPlan): string => plan.key ?? 'its own key';

/** The CUE deck's head line: headphones only, what it is matched to. */
export function cueInfo(masterBpm: number | null, plan: KeyPlan): string {
	const match =
		masterBpm !== null
			? `matched to MASTER ${masterBpm.toFixed(1)} BPM`
			: 'no MASTER deck, plays at its own BPM';
	return `Headphones only . ${match}, plays in ${keyText(plan)}`;
}

/** The play info line: native tempo and key, then what it plays at against the master. */
export function playInfo(slot: SamplerSlot, masterBpm: number | null, plan: KeyPlan): string {
	const native = `Native ${slot.native_bpm.toFixed(1)} BPM${slot.key ? ` ${slot.key}` : ''}`;
	const shift = plan.shift ? ` (${plan.shift > 0 ? '+' : ''}${plan.shift} st)` : '';
	return `${native}. Plays at ${bpmText(masterBpm)} in ${keyText(plan)}${shift}, on its own beatgrid.`;
}

//----- settings rows (spec 7.4: the deck's own words)

/** Whole beats the downbeat can move either way. */
export const DOWNBEAT_LIMIT = 3;

/** One segmented settings row: its label, the setting it writes, and each option's words and tip. */
interface SettingRow {
	label: string;
	key: 'launch' | 'key' | 'tempo' | 'mode';
	tip: string;
	options: Array<{ value: string; text: string; tip: string }>;
}

/** The modal's four segmented rows, in mock order and wording. */
export const SETTING_ROWS: SettingRow[] = [
	{
		label: 'Launch & sync to',
		key: 'launch',
		tip: 'When the sample starts, and what it lines up with on the MASTER deck.',
		options: [
			{
				value: 'bar',
				text: 'Next bar',
				tip: "Starts on the MASTER deck's next downbeat and stays locked to its bars."
			},
			{
				value: 'beat',
				text: 'Next beat',
				tip: "Starts on the MASTER deck's next beat and stays locked to its beats."
			},
			{ value: '4bars', text: '4 bars', tip: "Starts at the MASTER deck's next 4-bar phrase." },
			{
				value: 'off',
				text: 'Off (no sync)',
				tip: "Starts the moment you press play, not locked to the MASTER deck's grid."
			}
		]
	},
	{
		label: 'Key',
		key: 'key',
		tip: "How the sample's key follows the MASTER deck. Same controls as on a deck.",
		options: [
			{
				value: 'auto',
				text: 'KEY SYNC',
				tip: 'KEY SYNC: shifts the sample into a key compatible with the MASTER deck, up to the key shift limit in the compatible settings (shown as the superscript on the tab).'
			},
			{
				value: 'master_tempo',
				text: 'Master Tempo',
				tip: 'Master Tempo ON - hold musical key while changing tempo'
			},
			{
				value: 'off',
				text: 'Master Tempo OFF',
				tip: 'Master Tempo OFF - pitch and key shift together with tempo'
			}
		]
	},
	{
		label: 'Tempo',
		key: 'tempo',
		tip: "Whether the sample follows the MASTER deck's BPM.",
		options: [
			{
				value: 'match',
				text: 'Match MASTER',
				tip: "Plays at the MASTER deck's BPM, phase-locked to its beats (BEAT SYNC)."
			},
			{
				value: 'own',
				text: 'Own BPM',
				tip: 'Plays at its own BPM from its own beatgrid, not synced.'
			}
		]
	},
	{
		label: 'Mode',
		key: 'mode',
		tip: 'What happens after the sample starts.',
		options: [
			{ value: 'loop', text: 'Loop', tip: 'Repeats its loop until you press pause.' },
			{ value: 'one_shot', text: 'One shot', tip: 'Plays the region once, then stops.' },
			{
				value: 'gate',
				text: 'Gate',
				tip: 'Plays only while you hold play (or the pad), stops on release.'
			}
		]
	}
];

/** The full settings after one change; One shot plays once, leaving it loops again; downbeat and level clamp. */
export function settingsWith<K extends keyof SlotSettings>(
	settings: SlotSettings,
	key: K,
	value: SlotSettings[K]
): SlotSettings {
	const next: SlotSettings = { ...settings, [key]: value };
	if (key === 'mode') {
		if (value === 'one_shot') next.repeat = 'once';
		else if (settings.repeat === 'once') next.repeat = 'loop';
	}
	next.downbeat_offset = Math.max(-DOWNBEAT_LIMIT, Math.min(DOWNBEAT_LIMIT, next.downbeat_offset));
	next.level = Math.max(0, Math.min(100, next.level));
	return next;
}

/** The downbeat nudge as the row shows it: "on grid", "+1 beat", "-2 beat". */
export const downbeatLabel = (offset: number): string =>
	offset === 0 ? 'on grid' : `${offset > 0 ? '+' : ''}${offset} beat`;
