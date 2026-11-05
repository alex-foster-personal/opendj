import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import {
	DECLINED_CODE,
	type AgentEvent,
	type ToolCall,
	type ToolError
} from '$lib/assistant/agent-contract';
import {
	EMPTY_SESSION,
	foldEvents,
	resumeTurn,
	startTurn,
	type Session
} from '$lib/assistant/agent-session';

import AgentSidebarStoryDock from './AgentSidebarStoryDock.svelte';
import AgentSidebarView from './AgentSidebarView.svelte';

/**
 * One story per state of the agent sidebar. Every transcript is a recorded
 * AgentEvent array in the exact wire shape of agent-contract.ts, folded by
 * the real reducer (agent-session.ts foldEvents), so a story can only show
 * what the live sidebar would show for the same stream.
 */

//----- recorded wire

const FAST = 'google/gemini-3.7-flash';
const THINK = 'anthropic/claude-sonnet-5';

const call = (id: string, name: string, args: Record<string, unknown> = {}): ToolCall => ({
	id,
	name,
	arguments: args
});

/** The assistant turn that asked for `calls`, echoed for the client history. */
const asked = (...calls: ToolCall[]): AgentEvent => ({
	type: 'message',
	message: { role: 'assistant', content: null, tool_calls: calls }
});

const answered = (callId: string, result: unknown): AgentEvent => ({
	type: 'message',
	message: { role: 'tool', content: JSON.stringify(result), tool_call_id: callId }
});

/** A failed or declined call's tool message: the engine stores `{error}` in history. */
const refused = (callId: string, error: ToolError): AgentEvent => answered(callId, { error });

const said = (content: string): AgentEvent => ({
	type: 'message',
	message: { role: 'assistant', content }
});

/** Text as it streams: split into small deltas, the way the engine sends it. */
const deltas = (text: string): AgentEvent[] =>
	(text.match(/[\s\S]{1,12}/g) ?? []).map((delta) => ({ type: 'text', delta }));

const reply = (text: string): AgentEvent[] => [...deltas(text), said(text)];

const turn = (text: string, events: AgentEvent[], from: Session = EMPTY_SESSION): Session =>
	foldEvents(startTurn(from, text), events);

const resume = (from: Session, events: AgentEvent[]): Session =>
	foldEvents(resumeTurn(from), events);

//----- transcripts

const TRACKS = [
	{
		title: 'Sample Track A',
		artist: 'Sample Artist One',
		bpm: 128,
		key: '8A',
		last_played_at: '2026-06-02'
	},
	{
		title: 'Sample Track B',
		artist: 'Sample Artist Two',
		bpm: 127.5,
		key: '9A',
		last_played_at: null
	},
	{
		title: 'Sample Track C',
		artist: 'Sample Artist Three',
		bpm: 128,
		key: '8B',
		last_played_at: '2026-04-19'
	}
];

const LISTING = { items: TRACKS, next_cursor: null, non_music_hidden: 0 };

const search = call('call_lib_1', 'library_read', { path: '/api/v1/tracks?q=techno&limit=20' });
const librarySearch = turn('Find techno around 128 BPM I have not played lately', [
	{ type: 'tier', tier: 'fast', model: FAST, reason: 'library question, one tool round' },
	asked(search),
	{ type: 'tool_call', call: search, gate: 'read' },
	{ type: 'tool_result', call_id: search.id, ok: true, result: LISTING, error: null },
	answered(search.id, LISTING),
	...reply(
		'Three techno tracks near **128 BPM** you have not played since spring:\n\n' +
			'1. **Sample Track B** - Sample Artist Two, 127.5 BPM, 9A (never played)\n' +
			'2. **Sample Track C** - Sample Artist Three, 128 BPM, 8B (last 19 Apr)\n' +
			'3. **Sample Track A** - Sample Artist One, 128 BPM, 8A (last 2 Jun)\n\n' +
			'`Sample Track B` into `Sample Track C` is a clean 9A to 8B move.'
	),
	{ type: 'done', reason: 'stop' }
]);

const volume = call('call_vol_1', 'system_volume_set', { volume: 30 });
const VOLUME_READBACK = { volume: 30, muted: false, backend: 'core_audio' };
const volumeHeld = turn('set the system volume to 30%', [
	{ type: 'tier', tier: 'instant', model: null, reason: 'rule: system volume command' },
	asked(volume),
	{ type: 'tool_call', call: volume, gate: 'confirm' },
	{ type: 'confirm', call: volume, reason: 'Runs only after your approval' },
	{ type: 'done', reason: 'needs_confirmation' }
]);
const instantVolume = resume(volumeHeld, [
	{ type: 'tool_result', call_id: volume.id, ok: true, result: VOLUME_READBACK, error: null },
	answered(volume.id, VOLUME_READBACK),
	...reply('System volume is now 30%.'),
	{ type: 'done', reason: 'stop' }
]);

const play = call('call_cmd_1', 'command', { verb: 'play 2' });
const confirmHeld = turn('play deck 2', [
	{ type: 'tier', tier: 'instant', model: null, reason: 'rule: play command' },
	asked(play),
	{ type: 'tool_call', call: play, gate: 'confirm' },
	{
		type: 'confirm',
		call: play,
		reason: 'Runs only after your approval - a deck is audible, so the room will hear it'
	},
	{ type: 'done', reason: 'needs_confirmation' }
]);
const DECLINED: ToolError = { code: DECLINED_CODE, message: 'the user declined this call' };
const declined = resume(confirmHeld, [
	{ type: 'tool_result', call_id: play.id, ok: false, result: null, error: DECLINED },
	refused(play.id, DECLINED),
	...reply('OK, I left deck 2 alone.'),
	{ type: 'done', reason: 'stop' }
]);

const decks = call('call_state_1', 'app_state');
const NO_PAGE: ToolError = {
	code: 'no_performance_page',
	message: 'no performance page is open; open /performance in the app first'
};
const toolFailed = turn("What's loaded on each deck?", [
	{ type: 'tier', tier: 'fast', model: FAST, reason: 'deck question' },
	asked(decks),
	{ type: 'tool_call', call: decks, gate: 'read' },
	{ type: 'tool_result', call_id: decks.id, ok: false, result: null, error: NO_PAGE },
	refused(decks.id, NO_PAGE),
	...reply("I can't see any decks: open **Performance** and ask again."),
	{ type: 'done', reason: 'stop' }
]);

const rounds = Array.from({ length: 8 }, (_, round) =>
	call(`call_lib_${round + 1}`, 'library_read', {
		path: `/api/v1/tracks?limit=50&cursor=page${round + 1}`
	})
);
const turnCap = turn('Build me a two hour set from everything that mixes out of 8A', [
	{ type: 'tier', tier: 'think', model: THINK, reason: 'multi-step set building' },
	...rounds.flatMap((round): AgentEvent[] => [
		asked(round),
		{ type: 'tool_call', call: round, gate: 'read' },
		{ type: 'tool_result', call_id: round.id, ok: true, result: LISTING, error: null },
		answered(round.id, LISTING)
	]),
	{ type: 'done', reason: 'turn_cap' }
]);

const upstreamError = turn('Suggest a closer for tonight', [
	{ type: 'tier', tier: 'fast', model: FAST, reason: 'library question' },
	{
		type: 'error',
		code: 'assistant_upstream_error',
		message: 'The model service answered 502 Bad Gateway. Try again in a moment.'
	}
]);

/** A refusal before the stream starts; AgentSidebar folds it in as an error event. */
const spendCap = turn('Suggest a closer for tonight', [
	{
		type: 'error',
		code: 'assistant_upstream_error',
		message:
			"This install has used today's free preview allowance (the gateway answered 429). It resets at 00:00 UTC."
	}
]);

const streaming = turn('Find techno around 128 BPM I have not played lately', [
	{ type: 'tier', tier: 'fast', model: FAST, reason: 'library question, one tool round' },
	asked(search),
	{ type: 'tool_call', call: search, gate: 'read' },
	{ type: 'tool_result', call_id: search.id, ok: true, result: LISTING, error: null },
	answered(search.id, LISTING),
	...deltas(
		'Three techno tracks near **128 BPM**:\n\n1. **Sample Track B** - Sample Artist Two, 127.5 BPM\n2. **Steel'
	)
]);

const thinkPlan = turn('Plan a warm-up hour that ends ready for peak time', [
	{ type: 'tier', tier: 'think', model: THINK, reason: 'open-ended set planning' },
	...reply(
		'### Warm-up hour plan\n\n' +
			'1. **0-15 min** - deep, 118-120 BPM, keys around 5A-6A. Keep the low end light.\n' +
			'2. **15-35 min** - lift to 122 BPM, bring in percussion-led tracks.\n' +
			'3. **35-50 min** - 124-125 BPM, first vocal moment; move to 8A.\n' +
			'4. **50-60 min** - 126 BPM, hand over on a track with a long outro.\n\n' +
			'| Block | BPM | Keys |\n| --- | --- | --- |\n| Open | 118-120 | 5A-6A |\n| Build | 122 | 6A-7A |\n| Lift | 124-125 | 8A |\n| Hand-over | 126 | 8A-9A |\n\n' +
			'> Say "fill block 1" and I will pick tracks from your library for it.'
	),
	{ type: 'done', reason: 'stop' }
]);

//----- meta

const view = (session: Session) => ({
	items: session.items,
	busy: session.busy,
	done: session.done
});

const DECKS = [
	{ id: 'deck-1', label: 'Deck 1', title: 'Deck 1: Sample Track A - Sample Artist One, playing' },
	{ id: 'deck-2', label: 'Deck 2', title: 'Deck 2: Sample Track B - Sample Artist Two, cued' }
];
const PLAYLIST = { id: 'playlist', label: 'This playlist', title: 'Playlist: Warm-up (42 tracks)' };
const USB = { id: 'usb', label: 'USB', title: 'USB stick: CDJ-SET (2 playlists)' };

const meta = {
	title: 'Components/AgentSidebarView',
	component: AgentSidebarView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	decorators: [() => ({ Component: AgentSidebarStoryDock })],
	args: {
		items: [],
		busy: false,
		done: null,
		availability: 'ready',
		mode: 'gateway',
		tierChoice: 'auto',
		ontier: fn(),
		onremovecontext: fn(),
		onsend: fn(),
		onapprove: fn(),
		ondecline: fn(),
		onstop: fn(),
		onnewchat: fn(),
		onclose: fn()
	}
} satisfies Meta<typeof AgentSidebarView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** First open: what it can do, three suggestions, and every context chip. */
export const Empty: Story = {
	args: {
		suggestions: [
			'Find techno around 128 BPM I have not played lately',
			"What's loaded on each deck?",
			'Turn the system volume down a bit'
		],
		contexts: [...DECKS, PLAYLIST, USB]
	}
};

/** The status probe has not answered yet: notice only, composer closed. */
export const Checking: Story = { args: { availability: 'checking' } };

/** The engine did not answer the status probe: composer closed. */
export const Unreachable: Story = { args: { availability: 'unreachable' } };

/** No key or gateway on this computer: an instant command the engine matched sends. */
export const Keyless: Story = {
	args: { availability: 'keyless', draft: 'pause deck 4', instantDraft: true }
};

/** Keyless with a draft the engine's instant rules reject: Send is disabled and says why. */
export const KeylessNotInstant: Story = {
	args: { availability: 'keyless', draft: 'find me something dark', instantDraft: false }
};

/** The draft check failed: this draft is refused with why, and the composer stays open. */
export const KeylessCheckFailed: Story = {
	args: {
		availability: 'keyless',
		draft: 'pause deck 4',
		instantError: 'the assistant endpoint answered 503 without an error envelope'
	}
};

/** The user's own key: they are billed, so no free-preview badge. */
export const OwnKey: Story = { args: { mode: 'byok' } };

/** Mid-reply: the tool already answered, the text is still arriving. */
export const Streaming: Story = { args: { ...view(streaming), contexts: [PLAYLIST] } };

/** Instant tier: a rule matched, no model call; the volume change was held, approved, done. */
export const InstantVolume: Story = { args: view(instantVolume) };

/** Fast tier, one library read, markdown answer with a list. */
export const LibrarySearch: Story = { args: { ...view(librarySearch), contexts: [PLAYLIST] } };

/** A deck command waits for an OK because a deck is audible; Enter / Esc answer it. */
export const ConfirmHeld: Story = { args: { ...view(confirmHeld), contexts: DECKS } };

/** The user said Don't: the engine answered declined_by_user and the model moved on. */
export const Declined: Story = { args: view(declined) };

/** A read tool failed with its code; the error sits under the row. */
export const ToolFailed: Story = { args: view(toolFailed) };

/** The loop hit its round cap and stopped to ask. */
export const TurnCap: Story = { args: view(turnCap) };

/** The model service failed after the tier was chosen. */
export const UpstreamError: Story = { args: view(upstreamError) };

/** The gateway refused before streaming: the per-install spend cap is used up. */
export const SpendCapReached: Story = { args: view(spendCap) };

/** Think tier: a multi-step plan with headings, a list, a table and a quote. */
export const ThinkPlan: Story = { args: view(thinkPlan) };
