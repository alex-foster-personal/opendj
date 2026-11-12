import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import type { ControlEventV2Draft, SampleStamp } from './control-event-types';
import { hotCueId } from './control_ids';
import { mapControlEventSource } from './stem-control-events';

export function eventsFromHotCueTrigger(
	command: { deck: DeckId; slot: HotCueSlot },
	cueInMs: number,
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string
): ControlEventV2Draft[] {
	return [
		{
			v: 2,
			session_id: sessionId,
			sample: stamp.sample,
			sample_rate: stamp.sample_rate,
			wall_clock: wallClock,
			source: mapControlEventSource(source),
			deck: command.deck,
			control: hotCueId(command.deck, command.slot),
			value: cueInMs,
			kind: 'trigger'
		}
	];
}
