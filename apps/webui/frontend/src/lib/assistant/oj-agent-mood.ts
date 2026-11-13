/**
 * OJ's mood in the agent sidebar (issue #13937, epic #9374): the header glass reacts to what
 * OJ just did. A tool result that lands ok smiles for one beat, a failed or declined call or a
 * failed turn sloshes, and a write (a confirm-gated call) that lands adds a bubble burst.
 * Pure, no DOM and no timers: the container arms the one timer that ends a beat. Nothing
 * reacts unless ojMotionPolicy keeps motion on, and the `calm` variant keeps think and idle only.
 */
import type { OjAgentLook } from '$lib/components/assistant/oj-agent-header';
import { OJ_GESTURE_MS } from '$lib/components/assistant/oj-glass-gestures';
import { ojMotionAnimates } from '$lib/components/assistant/oj-glass-motion';
import type { OjMotion } from '$lib/components/assistant/oj-motion-policy';
import { defineVariantSetting } from '$lib/settings/variant-setting';
import type { AgentEvent } from './agent-contract';
import type { Item } from './agent-session';

export type OjMoodStyle = 'react' | 'calm';

export const OJ_MOOD_VARIANT = defineVariantSetting({
	id: 'variant_anim_oj_mood',
	label: 'OJ sidebar mood',
	group: 'advanced',
	keywords: ['variant', 'oj', 'assistant', 'sidebar', 'glass', 'mood', 'smile', 'slosh'],
	detail: 'How the OJ glass in the agent sidebar reacts to tool outcomes (issue #13937).',
	variants: [
		{
			value: 'react',
			label: 'Reacts',
			summary: 'smile on a tool success, slosh on a failure or decline, bubbles when a write lands'
		},
		{ value: 'calm', label: 'Calm', summary: 'think while OJ works, idle otherwise' }
	],
	defaultValue: 'react'
});

/**
 * One beat: the sidebar has no tempo, so a beat is the glass's own gesture length (1.2 s, the
 * CSS smile and slosh animation), which also keeps the end-of-beat timer above OJ_MIN_TIMER_MS.
 */
export const OJ_MOOD_BEAT_MS = OJ_GESTURE_MS;

export interface OjMoodBeat {
	glass: 'smile' | 'slosh';
	/** Bubbles rise in the glass for the beat: a write landed. */
	burst: boolean;
	/** Epoch ms when the beat ends and the glass returns to its base look. */
	endsAt: number;
}

export interface OjMoodContext {
	/** The session items after the events were folded; tool items carry each call's gate. */
	items: readonly Item[];
	motion: OjMotion;
	style: OjMoodStyle;
	now: number;
}

/** Parse a stored mood variant value; throws on anything unknown. */
export function ojMoodStyle(value: string): OjMoodStyle {
	if (value === 'react' || value === 'calm') return value;
	throw new Error(`OJ mood style ${JSON.stringify(value)} is not react|calm`);
}

/** The beat the last reacting event in `events` starts, or null when nothing reacts. */
export function ojMoodBeat(
	events: readonly AgentEvent[],
	context: OjMoodContext
): OjMoodBeat | null {
	if (context.style === 'calm' || !ojMotionAnimates(context.motion)) return null;
	let beat: OjMoodBeat | null = null;
	for (const event of events) {
		const glass = moodOf(event, context.items);
		if (glass !== null) beat = { ...glass, endsAt: context.now + OJ_MOOD_BEAT_MS };
	}
	return beat;
}

/** The header look with the mood applied: a live beat overrides the glass; calm keeps think and idle. */
export function ojMoodLook(
	look: OjAgentLook,
	beat: OjMoodBeat | null,
	now: number,
	style: OjMoodStyle
): OjAgentLook {
	if (style === 'calm') return { ...look, glass: look.glass === 'think' ? 'think' : 'idle' };
	if (beat === null || now >= beat.endsAt || !ojMotionAnimates(look.motion)) return look;
	return { ...look, glass: beat.glass, glassBubbles: look.glassBubbles || beat.burst };
}

//----- helpers

function moodOf(event: AgentEvent, items: readonly Item[]): Omit<OjMoodBeat, 'endsAt'> | null {
	switch (event.type) {
		case 'tool_result':
			if (!event.ok) return { glass: 'slosh', burst: false };
			return { glass: 'smile', burst: gateOf(items, event.call_id) === 'confirm' };
		case 'error':
			return { glass: 'slosh', burst: false };
		case 'tier':
		case 'text':
		case 'tool_call':
		case 'confirm':
		case 'message':
		case 'done':
			return null;
		default: {
			const _exhaustive: never = event;
			throw new Error(`Unhandled agent event: ${JSON.stringify(_exhaustive)}`);
		}
	}
}

function gateOf(items: readonly Item[], callId: string): string | null {
	for (const item of items) if (item.kind === 'tool' && item.call.id === callId) return item.gate;
	return null;
}
