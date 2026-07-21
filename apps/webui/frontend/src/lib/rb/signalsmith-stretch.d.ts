declare module 'signalsmith-stretch' {
	export interface SignalsmithStretchConfiguration {
		blockMs?: number | null;
		intervalMs?: number;
		splitComputation?: boolean;
		preset?: 'default' | 'cheaper';
	}

	export interface SignalsmithStretchSchedule {
		output: number;
		outputTime?: number;
		active?: boolean;
		input?: number;
		rate?: number;
		semitones?: number;
		tonalityHz?: number;
		formantSemitones?: number;
		formantCompensation?: boolean;
		formantBaseHz?: number;
		loopStart?: number;
		loopEnd?: number;
	}

	export interface SignalsmithStretchNode extends AudioWorkletNode {
		inputTime: number;
		addBuffers(buffers: Float32Array[], transfer?: Transferable[]): Promise<number>;
		configure(configuration: SignalsmithStretchConfiguration): Promise<void>;
		dropBuffers(toSeconds?: number): Promise<{ start: number; end: number } | undefined>;
		latency(): Promise<number>;
		schedule(schedule: SignalsmithStretchSchedule): Promise<void>;
		setUpdateInterval(seconds: number, callback: (inputTime: number) => void): Promise<void>;
		start(when?: number, offset?: number): Promise<void>;
		stop(when?: number): Promise<void>;
	}

	export default function createSignalsmithStretch(
		audioContext: AudioContext,
		options?: AudioWorkletNodeOptions
	): Promise<SignalsmithStretchNode>;
}
