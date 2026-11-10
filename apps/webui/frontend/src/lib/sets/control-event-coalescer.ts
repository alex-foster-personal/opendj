import type { ControlEventKind, ControlEventV2Wire } from './control-event-types';
import { CONTROL_COALESCE_MS } from './control-event-types';

interface CoalesceSlot {
	control: string;
	kind: ControlEventKind;
	last: ControlEventV2Wire;
	first: ControlEventV2Wire;
}

export class ControlEventCoalescer {
	private readonly _slots = new Map<string, CoalesceSlot>();
	private readonly _triggers: ControlEventV2Wire[] = [];
	private _windowStartMs: number | null = null;

	constructor(private readonly quantumMs: number = CONTROL_COALESCE_MS) {}

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
