import type { DeckId } from '$lib/rb/deck-slots';
import type { ColorFxCommand } from '$lib/rb/color-fx';
import type { ColorFxSwitchPlan } from '$lib/rb/color-fx';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import {
	BEAT_FX_BEATS,
	BEAT_FX_ON,
	BEAT_FX_QUANTIZE,
	BEAT_FX_RELEASE,
	BEAT_FX_SELECT,
	BEAT_FX_TARGET,
	BEAT_FX_WET,
	FX_COLOR_ECHO_BEATS,
	FX_COLOR_SELECT,
	FX_COLOR_WET
} from './control_ids';

export type ControlEventKind = 'set' | 'trigger';
export type ControlEventV2Source = 'user' | 'controller' | 'agent' | 'replay';

export interface ControlEventV2Wire {
	v: 2;
	session_id: string;
	seq: number;
	sample: number;
	sample_rate: number;
	wall_clock: string;
	control: string;
	value: string | number | boolean | null;
	kind: ControlEventKind;
	source: ControlEventV2Source;
	deck?: number;
}

export interface SampleStamp {
	sample: number;
	sample_rate: number;
}

const COALESCE_MS = 10;

export function mapControlEventSource(source: CommandSource): ControlEventV2Source {
	if (source === 'agent-command') return 'agent';
	return 'user';
}

export type ControlEventV2Draft = Omit<ControlEventV2Wire, 'seq'>;

export function eventsFromColorFxCommand(
	command: ColorFxCommand,
	plan: ColorFxSwitchPlan,
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string
): ControlEventV2Draft[] {
	const deck = command.deck as DeckId;
	const mapped = mapControlEventSource(source);
	const out: ControlEventV2Draft[] = [];
	const base = {
		v: 2 as const,
		session_id: sessionId,
		sample: stamp.sample,
		sample_rate: stamp.sample_rate,
		wall_clock: wallClock,
		source: mapped,
		deck
	};

	if (command.effect !== undefined || plan.switched) {
		out.push({
			...base,
			control: FX_COLOR_SELECT[deck],
			value: plan.effect,
			kind: 'set'
		});
	}
	if (command.echo_beats !== undefined) {
		out.push({
			...base,
			control: FX_COLOR_ECHO_BEATS[deck],
			value: command.echo_beats,
			kind: 'set'
		});
	}
	if (command.value !== undefined || (plan.switched && command.value === undefined)) {
		out.push({
			...base,
			control: FX_COLOR_WET[deck],
			value: plan.value,
			kind: 'set'
		});
	}
	return out;
}

export type BeatFxCommandLike = {
	type: 'beat_fx';
	target?: string;
	effect?: string;
	beats?: number;
	level?: number;
	enabled?: boolean;
	quantize?: boolean;
	xpad?: number | null;
};

export type BeatFxReleaseCommandLike = {
	type: 'beat_fx_release';
	held: boolean;
	beats?: number;
	kind?: string;
};

export function eventsFromBeatFxCommand(
	command: BeatFxCommandLike,
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string
): ControlEventV2Draft[] {
	const mapped = mapControlEventSource(source);
	const out: ControlEventV2Draft[] = [];
	const base = {
		v: 2 as const,
		session_id: sessionId,
		sample: stamp.sample,
		sample_rate: stamp.sample_rate,
		wall_clock: wallClock,
		source: mapped
	};

	if (command.enabled !== undefined) {
		out.push({ ...base, control: BEAT_FX_ON, value: command.enabled, kind: 'set' });
	}
	if (command.effect !== undefined) {
		out.push({ ...base, control: BEAT_FX_SELECT, value: command.effect, kind: 'set' });
	}
	if (command.target !== undefined) {
		out.push({ ...base, control: BEAT_FX_TARGET, value: command.target, kind: 'set' });
	}
	if (command.beats !== undefined) {
		out.push({ ...base, control: BEAT_FX_BEATS, value: command.beats, kind: 'set' });
	}
	if (command.level !== undefined) {
		out.push({ ...base, control: BEAT_FX_WET, value: command.level, kind: 'set' });
	}
	if (command.quantize !== undefined) {
		out.push({ ...base, control: BEAT_FX_QUANTIZE, value: command.quantize, kind: 'set' });
	}
	if (command.xpad !== undefined) {
		out.push({
			...base,
			control: BEAT_FX_WET,
			value: command.xpad ?? 0,
			kind: command.xpad === null ? 'trigger' : 'set'
		});
	}
	return out;
}

export function eventsFromBeatFxReleaseCommand(
	command: BeatFxReleaseCommandLike,
	source: CommandSource,
	stamp: SampleStamp,
	sessionId: string,
	wallClock: string
): ControlEventV2Draft[] {
	const mapped = mapControlEventSource(source);
	return [
		{
			v: 2,
			session_id: sessionId,
			sample: stamp.sample,
			sample_rate: stamp.sample_rate,
			wall_clock: wallClock,
			control: BEAT_FX_RELEASE,
			value: command.held,
			kind: 'trigger',
			source: mapped
		}
	];
}

interface CoalesceSlot {
	control: string;
	kind: ControlEventKind;
	last: ControlEventV2Wire;
	first: ControlEventV2Wire;
}

export class FxControlEventCoalescer {
	private readonly _slots = new Map<string, CoalesceSlot>();
	private readonly _triggers: ControlEventV2Wire[] = [];
	private _windowStartMs: number | null = null;

	constructor(private readonly quantumMs: number = COALESCE_MS) {}

	push(event: ControlEventV2Wire, nowMs: number): void {
		if (event.kind === 'trigger') {
			this._triggers.push(event);
			return;
		}
		if (this._windowStartMs === null) this._windowStartMs = nowMs;
		const slot = this._slots.get(event.control);
		if (slot === undefined) {
			this._slots.set(event.control, {
				control: event.control,
				kind: event.kind,
				first: event,
				last: event
			});
		} else {
			slot.last = event;
		}
	}

	/** Flush coalesced continuous controls when the quantum elapses or forced. */
	flush(nowMs: number, force = false): ControlEventV2Wire[] {
		const due =
			force ||
			(this._windowStartMs !== null &&
				nowMs - this._windowStartMs >= this.quantumMs &&
				this._slots.size > 0);
		if (!due) return [];
		const out: ControlEventV2Wire[] = [...this._triggers];
		this._triggers.length = 0;
		for (const slot of this._slots.values()) {
			if (slot.first.seq !== slot.last.seq) out.push(slot.first);
			out.push(slot.last);
		}
		this._slots.clear();
		this._windowStartMs = null;
		out.sort((a, b) => a.seq - b.seq);
		return out;
	}
}
