// GENERATED from apps/sets/control_ids.py by `uv run python -m apps.sets.control_ids --write`.
// Do not edit: change the Python registry and regenerate.

export const CONTROL_EVENT_VERSION = 2 as const;
export const CONTROL_COALESCE_QUANTUM_MS = 10.0;

export type ControlKind = 'set' | 'trigger';
export type ControlSource = 'user' | 'controller' | 'agent' | 'replay';

export const TRANSPORT_TEMPO_CONTROL_TEMPLATES = [
	'deck.{deck}.play',
	'deck.{deck}.cue',
	'deck.{deck}.seek_ms',
	'deck.{deck}.tempo',
	'deck.{deck}.tempo_fader',
	'deck.{deck}.pitch_range',
	'deck.{deck}.master_tempo',
	'deck.{deck}.key_nudge'
] as const;
