/**
 * Issue #9898, LIGHT-10573, review thread PRRT_kwDOSEvNd861Mlds: typed lighting-stage
 * session (AGENTS.md "Hit every surface" — UI controls share the same command path as agents).
 */
import { openReplay, type ReplaySource } from './frames-client';
import type { FrameMessage, StagePatch } from './stage-render';

export type StageMode = 'live' | 'replay';

export type StageSession = {
	mode: StageMode;
	replay: ReplaySource | null;
	replay_name: string | null;
	elapsed_s: number;
	playing: boolean;
	last_error: string | null;
};

export type StageCommand =
	| { type: 'load_replay'; text: string; name?: string }
	| { type: 'play' }
	| { type: 'pause' }
	| { type: 'toggle_play' }
	| { type: 'seek'; elapsed_s: number }
	| { type: 'tick'; dt_s: number }
	| { type: 'go_live' };

export const STAGE_COMMAND_TYPES: readonly StageCommand['type'][] = [
	'load_replay',
	'play',
	'pause',
	'toggle_play',
	'seek',
	'tick',
	'go_live'
];

export function newStageSession(): StageSession {
	return {
		mode: 'live',
		replay: null,
		replay_name: null,
		elapsed_s: 0,
		playing: false,
		last_error: null
	};
}

function requireReplayType(type: string, s: StageSession): ReplaySource {
	if (s.mode !== 'replay' || s.replay === null) {
		throw new Error(`${type}: no replay is loaded`);
	}
	return s.replay;
}

function finiteNumber(value: unknown, path: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`${path}: expected a finite number`);
	}
	return value;
}

export function parseStageCommand(value: unknown): StageCommand {
	if (value === null || typeof value !== 'object') {
		throw new TypeError('stage command: expected object');
	}
	const obj = value as Record<string, unknown>;
	const type = obj.type;
	if (typeof type !== 'string') {
		throw new TypeError('stage command: expected type string');
	}
	switch (type) {
		case 'load_replay': {
			const text = obj.text;
			if (typeof text !== 'string') {
				throw new TypeError('load_replay: expected text string');
			}
			const name = obj.name;
			if (name !== undefined && typeof name !== 'string') {
				throw new TypeError('load_replay: name must be a string when given');
			}
			return name === undefined ? { type, text } : { type, text, name };
		}
		case 'play':
		case 'pause':
		case 'toggle_play':
		case 'go_live':
			return { type };
		case 'seek':
			return { type, elapsed_s: finiteNumber(obj.elapsed_s, 'seek.elapsed_s') };
		case 'tick': {
			const dt_s = finiteNumber(obj.dt_s, 'tick.dt_s');
			if (dt_s < 0) throw new TypeError('tick.dt_s: expected a number >= 0');
			return { type, dt_s };
		}
		default:
			throw new TypeError(`stage command: unknown type ${type}`);
	}
}

export function applyStageCommand(s: StageSession, cmd: StageCommand): StageSession {
	switch (cmd.type) {
		case 'load_replay': {
			const replay = openReplay(cmd.text);
			return {
				mode: 'replay',
				replay,
				replay_name: cmd.name ?? null,
				elapsed_s: 0,
				playing: true,
				last_error: null
			};
		}
		case 'go_live':
			if (s.mode === 'live') {
				return { ...s, last_error: null };
			}
			return {
				mode: 'live',
				replay: null,
				replay_name: null,
				elapsed_s: 0,
				playing: false,
				last_error: null
			};
		case 'play': {
			requireReplayType('play', s);
			return { ...s, playing: true, last_error: null };
		}
		case 'pause': {
			requireReplayType('pause', s);
			return { ...s, playing: false, last_error: null };
		}
		case 'toggle_play': {
			requireReplayType('toggle_play', s);
			return { ...s, playing: !s.playing, last_error: null };
		}
		case 'seek': {
			const replay = requireReplayType('seek', s);
			let elapsed = cmd.elapsed_s;
			if (elapsed < 0) elapsed = 0;
			if (elapsed > replay.durationS) elapsed = replay.durationS;
			return { ...s, elapsed_s: elapsed, last_error: null };
		}
		case 'tick': {
			const replay = requireReplayType('tick', s);
			if (!s.playing) {
				return { ...s, last_error: null };
			}
			let elapsed = s.elapsed_s + cmd.dt_s;
			if (elapsed > replay.durationS) elapsed = replay.durationS;
			return { ...s, elapsed_s: elapsed, last_error: null };
		}
		default: {
			const _exhaustive: never = cmd;
			throw new Error(`unhandled command: ${String(_exhaustive)}`);
		}
	}
}

export function currentFrame(s: StageSession): { frame: FrameMessage; index: number } | null {
	if (s.mode !== 'replay' || s.replay === null) return null;
	return s.replay.frameAt(s.elapsed_s);
}

export function activePatch(
	s: StageSession,
	livePatch: StagePatch | null
): { patch: StagePatch | null; source: 'replay' | 'live' | null } {
	if (s.mode === 'replay' && s.replay !== null) {
		if (s.replay.patch !== null) {
			return { patch: s.replay.patch, source: 'replay' };
		}
		if (livePatch !== null) {
			return { patch: livePatch, source: 'live' };
		}
		return { patch: null, source: null };
	}
	if (livePatch !== null) {
		return { patch: livePatch, source: 'live' };
	}
	return { patch: null, source: null };
}

function copyFrame(frame: FrameMessage | null): FrameMessage | null {
	if (frame === null) return null;
	return {
		t_ns: frame.t_ns,
		patch_version: frame.patch_version,
		fixtures: frame.fixtures.map((fx) => ({
			id: fx.id,
			rgb: [fx.rgb[0], fx.rgb[1], fx.rgb[2]],
			intensity: fx.intensity,
			pan_deg: fx.pan_deg,
			tilt_deg: fx.tilt_deg,
			strobe_hz: fx.strobe_hz
		}))
	};
}

export type StageQuery = {
	version: 1;
	mode: StageMode;
	playing: boolean;
	elapsed_s: number;
	duration_s: number;
	replay_name: string | null;
	frame_count: number;
	frame_index: number | null;
	frame: FrameMessage | null;
	patch_source: 'replay' | 'live' | null;
	patch_version: string | number | null;
	patch_fixture_count: number;
	last_error: string | null;
};

export function stageQuery(
	s: StageSession,
	live: { frame: FrameMessage | null; patch: StagePatch | null }
): StageQuery {
	const { patch, source } = activePatch(s, live.patch);
	const patch_fixture_count = patch?.fixtures.length ?? 0;
	const patch_version = patch?.version ?? null;

	if (s.mode === 'live') {
		return Object.freeze({
			version: 1 as const,
			mode: 'live' as const,
			playing: false,
			elapsed_s: 0,
			duration_s: 0,
			replay_name: null,
			frame_count: 0,
			frame_index: null,
			frame: copyFrame(live.frame),
			patch_source: source,
			patch_version,
			patch_fixture_count,
			last_error: s.last_error
		});
	}

	const replay = s.replay;
	const duration_s = replay?.durationS ?? 0;
	const frame_count = replay?.frames.length ?? 0;
	const at = currentFrame(s);

	return Object.freeze({
		version: 1 as const,
		mode: 'replay' as const,
		playing: s.playing,
		elapsed_s: s.elapsed_s,
		duration_s,
		replay_name: s.replay_name,
		frame_count,
		frame_index: at?.index ?? null,
		frame: copyFrame(at?.frame ?? null),
		patch_source: source,
		patch_version,
		patch_fixture_count,
		last_error: s.last_error
	});
}
