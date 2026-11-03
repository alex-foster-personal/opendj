/**
 * Words the sidebar shows for contract names (agent-contract.ts), and the
 * held-call keyboard rule. Pure: no DOM, so node tests run it directly.
 */
import type { ContextKind, ContextRef, Tier } from './agent-contract';

export const TIER_LABEL: Record<Tier, string> = { instant: 'Instant', fast: 'Fast', think: 'Think' };

export const TIER_TITLE: Record<Tier, string> = {
	instant: 'Answered by a rule on this computer, no model call',
	fast: 'Answered by the fast model',
	think: 'Answered by the planning model'
};

const TOOL_LABEL: Record<string, string> = {
	status: 'Engine status',
	app_state: 'Deck state',
	library_read: 'Search library',
	library_write: 'Change library',
	command: 'Deck command',
	system_volume_get: 'Read system volume',
	system_volume_set: 'Set system volume',
	usb_list: 'List USB sticks',
	usb_eject: 'Eject USB stick'
};

/** Unknown tool names show as themselves rather than vanishing. */
export function toolLabel(name: string): string {
	return TOOL_LABEL[name] ?? name;
}

//----- setup state and context chips

/**
 * What the sidebar can do right now, from one /assistant/status reading.
 * 'keyless': the engine answered but has no usable model upstream, so only
 * instant commands (rule-matched, no model) work.
 */
export type Availability = 'checking' | 'unreachable' | 'keyless' | 'ready';

export function availabilityOf(status: { configured: boolean } | null, statusError: string | null): Availability {
	if (statusError !== null) return 'unreachable';
	if (status === null) return 'checking';
	return status.configured ? 'ready' : 'keyless';
}

export const AVAILABILITY_NOTICE: Record<Exclude<Availability, 'ready'>, string> = {
	checking: 'Checking whether the assistant is set up...',
	unreachable: 'The assistant could not be reached. Check that the engine is running, then reopen this panel.',
	keyless:
		'No model is set up on this computer, so only instant commands work, such as "play deck 1" or "set the system volume to 20%". For anything else, add your OpenRouter key or the Open DJ gateway address and install token (not both) to the engine environment and restart it.'
};

/** Refusals that mean no usable model upstream; mirrors the *_CODE names in apps/engine_core/assistant/upstream.py. */
export const SETUP_CODES: readonly string[] = [
	'assistant_key_missing',
	'assistant_upstream_ambiguous',
	'assistant_install_token_missing'
];

/** A setup refusal reads as the keyless notice; its raw detail stays in /assistant/status. */
export function errorText(code: string, message: string): string {
	return SETUP_CODES.includes(code) ? AVAILABILITY_NOTICE.keyless : message;
}

export const KEYLESS_SEND_TITLE =
	'Without a model key only instant commands work, such as "play deck 1", "pause deck 2" or "set master volume to 80%"';

const CONTEXT_KIND_LABEL: Record<ContextKind, string> = {
	deck: 'Deck',
	playlist: 'Playlist',
	usb: 'USB stick',
	track: 'Track'
};

export function contextChipId(ref: ContextRef): string {
	return `${ref.kind}:${ref.id}`;
}

export function contextChip(ref: ContextRef): { id: string; label: string; title: string } {
	return { id: contextChipId(ref), label: ref.label, title: `${CONTEXT_KIND_LABEL[ref.kind]}: ${ref.label}` };
}

//----- held-call keys

export interface HeldKeyFacts {
	key: string;
	/** Any of Alt, Ctrl, Meta or Shift held. */
	modified: boolean;
	/** The event target is inside the sidebar root. */
	inSidebar: boolean;
	/** The target is an input, textarea, select or contenteditable. */
	editing: boolean;
	/** The target is a button, whose own Enter activation must win. */
	onButton: boolean;
}

export interface SendFacts {
	draft: string;
	composerOpen: boolean;
	busy: boolean;
	held: boolean;
	keyless: boolean;
	/** Keyless only: the engine's verdict on the draft, null while it is asked. */
	instantDraft: boolean | null;
	/** Keyless only: the check itself failed, so no verdict is coming for this draft. */
	instantCheckFailed: boolean;
}

/**
 * What Enter or Send does with the draft. A keyless draft the engine is still
 * checking is queued and resolved by this same rule when the answer lands, so
 * a fast Enter is never dropped. A check that failed will never answer, so
 * the draft is refused rather than queued forever.
 */
export function sendOutcome(facts: SendFacts): 'send' | 'queue' | 'refuse' | 'ignore' {
	if (!facts.composerOpen || facts.busy || facts.held || !facts.draft.trim()) return 'ignore';
	if (!facts.keyless || facts.instantDraft === true) return 'send';
	return facts.instantDraft === null && !facts.instantCheckFailed ? 'queue' : 'refuse';
}

export const QUEUED_SEND_NOTE = 'Sends as soon as the engine confirms this is an instant command...';

/** The draft check failed, not the assistant: the composer stays open and any edit asks again. */
export function instantCheckFailedNote(reason: string): string {
	return `Could not check whether this is an instant command. Edit the draft to try again. (${reason})`;
}

/** Esc in the composer closes the sidebar only when nothing would be lost or left unanswered. */
export function composerEscapeAction(facts: { draft: string; held: boolean }): 'close' | 'blur' {
	return !facts.draft.trim() && !facts.held ? 'close' : 'blur';
}

/** Enter approves and Esc declines the next held call, only from inside the sidebar. */
export function heldKeyAction(facts: HeldKeyFacts): 'approve' | 'decline' | null {
	if (facts.modified || !facts.inSidebar || facts.editing) return null;
	if (facts.key === 'Escape') return 'decline';
	if (facts.key === 'Enter' && !facts.onButton) return 'approve';
	return null;
}
