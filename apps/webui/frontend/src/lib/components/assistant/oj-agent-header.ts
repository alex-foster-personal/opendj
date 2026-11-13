/**
 * OJ branding for the agent sidebar (issue #13711, epic #9374): what the header glass and the
 * panel's juice fill show for one agent session. Pure, no DOM and no timers; motion comes
 * from ojMotionPolicy and the loading look from one developer-only variant setting.
 */
import type { DoneReason } from '$lib/assistant/agent-contract';
import type { Item } from '$lib/assistant/agent-session';
import { defineVariantSetting } from '$lib/settings/variant-setting';
import type { OjFillMode, OjFillPhase } from './oj-fill';
import type { OjGlassState } from './oj-glass-gestures';
import type { OjMotion } from './oj-motion-policy';

export type OjLoadingStyle = 'juice' | 'plain';

export const OJ_LOADING_VARIANT = defineVariantSetting({
	id: 'variant_anim_oj_loading',
	label: 'OJ sidebar loading look',
	group: 'advanced',
	keywords: ['variant', 'oj', 'assistant', 'sidebar', 'juice', 'fill', 'loading'],
	detail: 'What the OJ agent sidebar shows while OJ answers (issue #13711).',
	variants: [
		{
			value: 'juice',
			label: 'Juice fill',
			summary: 'panel fills bottom-up with bubbles, drains on reply'
		},
		{ value: 'plain', label: 'Plain', summary: 'glass only, no panel fill' }
	],
	defaultValue: 'juice'
});

export interface OjAgentTurn {
	items: readonly Item[];
	busy: boolean;
	done: DoneReason | null;
}

export interface OjAgentLook {
	glass: OjGlassState;
	glassBubbles: boolean;
	motion: OjMotion;
	fillPhase: OjFillPhase;
	/** 'none' under the plain loading style; otherwise the policy's fill mode. */
	fillMode: OjFillMode;
	/** Reply characters streamed since the last user message. */
	streamedChars: number;
}

/** The look before any turn: idle glass, empty fill, no motion. */
export const OJ_AGENT_IDLE: OjAgentLook = Object.freeze({
	glass: 'idle',
	glassBubbles: false,
	motion: Object.freeze({ idleGestures: false, bubbles: false, fillMode: 'static' }),
	fillPhase: 'idle',
	fillMode: 'static',
	streamedChars: 0
});

/** Parse a stored loading-style variant value; throws on anything unknown. */
export function ojLoadingStyle(value: string): OjLoadingStyle {
	if (value === 'juice' || value === 'plain') return value;
	throw new Error(`OJ loading style ${JSON.stringify(value)} is not juice|plain`);
}

/** Characters of reply text after the last user message in `items`. */
export function ojTurnChars(items: readonly Item[]): number {
	let chars = 0;
	for (let i = items.length - 1; i >= 0; i -= 1) {
		const item = items[i];
		if (item.kind === 'user') break;
		if (item.kind === 'text') chars += item.text.length;
	}
	return chars;
}

/** Glass and fill for one session: think and fill while busy, smile and drain once a reply lands. */
export function ojAgentLook(
	turn: OjAgentTurn,
	motion: OjMotion,
	style: OjLoadingStyle
): OjAgentLook {
	const streamedChars = ojTurnChars(turn.items);
	const landed = !turn.busy && turn.done === 'stop';
	let fillPhase: OjFillPhase;
	if (turn.busy && streamedChars === 0) fillPhase = 'waiting';
	else if (turn.busy) fillPhase = 'streaming';
	else if (landed) fillPhase = 'done';
	else fillPhase = 'idle';
	let glass: OjGlassState;
	if (turn.busy) glass = 'think';
	else if (landed) glass = 'smile';
	else glass = 'idle';
	return {
		glass,
		glassBubbles: turn.busy && motion.bubbles,
		motion,
		fillPhase,
		fillMode: style === 'plain' ? 'none' : motion.fillMode,
		streamedChars
	};
}
