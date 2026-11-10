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

export type ControlEventV2Draft = Omit<ControlEventV2Wire, 'seq'>;

export const CONTROL_COALESCE_MS = 10;
