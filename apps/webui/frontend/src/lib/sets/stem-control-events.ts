import type { DeckId } from '$lib/rb/deck-slots';
import type { StemControl } from '$lib/rb/stem-types';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import type { ControlEventV2Draft, SampleStamp } from './control-event-types';
import { stemGainId, stemMuteId, stemSoloId } from './control_ids';

export function mapControlEventSource(source: CommandSource): 'user' | 'agent' {
	if (source === 'agent-command') return 'agent';
	return 'user';
}

type StemMuteCommand = { type: 'stem_mute'; deck: DeckId; stem: StemControl; muted: boolean };
type StemSoloCommand = { type: 'stem_solo'; deck: DeckId; stem: StemControl; solo: boolean };
type StemGainCommand = { type: 'stem_gain'; deck: DeckId; stem: StemControl; value: number };

export type StemCaptureCommand = StemMuteCommand | StemSoloCommand | StemGainCommand;

function _base(
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string,
	deck: DeckId
) {
	return {
		v: 2 as const,
		session_id: sessionId,
		sample: stamp.sample,
		sample_rate: stamp.sample_rate,
		wall_clock: wallClock,
		source: mapControlEventSource(source),
		deck
	};
}

export function eventsFromStemCommand(
	command: StemCaptureCommand,
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string
): ControlEventV2Draft[] {
	const base = _base(source, stamp, sessionId, wallClock, command.deck);
	if (command.type === 'stem_mute') {
		return [
			{
				...base,
				control: stemMuteId(command.deck, command.stem),
				value: command.muted,
				kind: 'set'
			}
		];
	}
	if (command.type === 'stem_solo') {
		return [
			{
				...base,
				control: stemSoloId(command.deck, command.stem),
				value: command.solo,
				kind: 'set'
			}
		];
	}
	return [
		{
			...base,
			control: stemGainId(command.deck, command.stem),
			value: command.value,
			kind: 'set'
		}
	];
}
