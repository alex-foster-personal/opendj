/**
 * Rule-based toast error classification (issue #3996, UX-TOAST-03).
 */

export type ToastErrorClass =
	| 'deck-load'
	| 'deck-processor'
	| 'beat-sync'
	| 'cue-transport'
	| 'autoplay'
	| 'audio-output'
	| 'browser-selection'
	| 'network'
	| 'unknown';

export interface ToastDiagnostic {
	classification: ToastErrorClass;
	feature: string;
	settingsSummary?: string;
	hint?: string;
}

function contextString(context: Record<string, unknown>, keys: string[]): string | undefined {
	const parts: string[] = [];
	for (const key of keys) {
		const value = context[key];
		if (value === undefined || value === null || value === '') continue;
		parts.push(`${key}: ${String(value)}`);
	}
	return parts.length > 0 ? parts.join('; ') : undefined;
}

function beatSyncSettings(context: Record<string, unknown>): string | undefined {
	const parts: string[] = [];
	if (context.beat_sync_mode !== undefined) parts.push(`Beat Sync: ${String(context.beat_sync_mode)}`);
	if (context.beat_sync_max === true) parts.push('Beat Sync Max: on');
	if (context.master_deck !== undefined) parts.push(`master deck: ${String(context.master_deck)}`);
	if (context.follower_deck !== undefined) parts.push(`follower deck: ${String(context.follower_deck)}`);
	return parts.length > 0 ? parts.join('; ') : undefined;
}

export function classifyToastError(input: {
	kind: 'info' | 'warn' | 'error';
	message: string;
	cause?: unknown;
	context?: Record<string, unknown>;
	feature?: string;
}): ToastDiagnostic {
	const message = input.message;
	const context = input.context ?? {};
	const lower = message.toLowerCase();
	const featureOverride = input.feature?.trim();

	if (
		context.source === 'browser-pane-load' ||
		/browser|playlist load|selection|library selection|autolist load|taglist load/i.test(message)
	) {
		const settings = contextString(context, [
			'playlist_id',
			'playlist_name',
			'pane_kind',
			'stable_id'
		]);
		return {
			classification: 'browser-selection',
			feature: featureOverride ?? 'Library selection',
			settingsSummary: settings,
			hint: settings !== undefined ? 'Retry after the pane finishes loading or pick another playlist.' : undefined
		};
	}

	if (/cue\.?jump|hot cue|cue jump/i.test(message) || context.cue_jump !== undefined) {
		const settings = beatSyncSettings(context);
		const cuePart = contextString(context, ['cue_slot', 'deck_id']);
		const merged = [settings, cuePart].filter(Boolean).join('; ');
		return {
			classification: 'cue-transport',
			feature: featureOverride ?? 'Cue / transport',
			settingsSummary: merged !== '' ? merged : undefined,
			hint: 'Check quantize, beat sync mode, and playhead position before jumping.'
		};
	}

	if (/beat sync|beat-sync|bar fold|tempo lock/i.test(message) || context.beat_sync_mode !== undefined) {
		const settings = beatSyncSettings(context);
		return {
			classification: 'beat-sync',
			feature: featureOverride ?? 'Beat Sync',
			settingsSummary: settings,
			hint:
				context.beat_sync_max === true
					? 'Try turning off Beat Sync Max or switch to BEAT mode.'
					: undefined
		};
	}

	if (
		input.cause instanceof RangeError &&
		(/cue|jump/i.test(message) || context.cue_jump !== undefined)
	) {
		const settings = beatSyncSettings(context);
		return {
			classification: 'cue-transport',
			feature: featureOverride ?? 'Cue / transport',
			settingsSummary: settings,
			hint: 'RangeError often means the target beat is outside the loaded track or sync window.'
		};
	}

	if (/autoplay/i.test(message)) {
		return {
			classification: 'autoplay',
			feature: featureOverride ?? 'AutoPlay',
			settingsSummary: contextString(context, ['deck_id', 'chain_id'])
		};
	}

	if (/presentation clock|output clock|audio context/i.test(message)) {
		return {
			classification: 'audio-output',
			feature: featureOverride ?? 'Audio output',
			settingsSummary: contextString(context, ['deck_id'])
		};
	}

	if (/processor failed/i.test(message)) {
		return {
			classification: 'deck-processor',
			feature: featureOverride ?? 'Deck audio',
			settingsSummary: contextString(context, ['deck_id', 'stage'])
		};
	}

	if (/load failed|could not load|stems unavailable|unload/i.test(message) || context.load_stage !== undefined) {
		return {
			classification: 'deck-load',
			feature: featureOverride ?? 'Deck load',
			settingsSummary: contextString(context, ['deck_id', 'stable_id', 'load_stage'])
		};
	}

	if (/network|fetch failed|failed to fetch|502|503|504/i.test(message)) {
		return {
			classification: 'network',
			feature: featureOverride ?? 'Network',
			settingsSummary: contextString(context, ['url', 'status'])
		};
	}

	if (featureOverride !== undefined && featureOverride !== '') {
		return {
			classification: 'unknown',
			feature: featureOverride,
			settingsSummary: contextString(context, ['deck_id', 'source'])
		};
	}

	if (lower.includes('deck') && /load|stem|processor/i.test(message)) {
		return {
			classification: /processor/i.test(message) ? 'deck-processor' : 'deck-load',
			feature: 'Deck audio',
			settingsSummary: contextString(context, ['deck_id'])
		};
	}

	return {
		classification: 'unknown',
		feature: featureOverride ?? 'Open DJ',
		settingsSummary: contextString(context, ['source', 'deck_id'])
	};
}
