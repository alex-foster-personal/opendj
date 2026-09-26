/**
 * Grayed PARITY-TODO settings rows (rekordbox + djay stubs).
 * Kept out of catalog.ts for the frontend file-size ratchet.
 */
import type { SettingDef, SettingGroupId } from './catalog-types';

/** Same literal as catalog.ts's INERT_TITLE, deliberately re-declared rather
 * than imported (importing would make catalog.ts and this module import each
 * other): the drift check in inert-controls.test.mjs (H13 + M20) fails loudly
 * if the two ever disagree, which is the same guard SettingsOverlay.svelte's
 * own independent declaration already relies on. */
const TODO = 'not implemented - see PARITY-TODO';

function _todo(
	id: string,
	label: string,
	group: SettingGroupId,
	keywords: string[],
	title: string
): SettingDef {
	return {
		id,
		label,
		group,
		keywords,
		title: TODO,
		detail: `${title}. ${TODO}`,
		implemented: false,
		control: { kind: 'boolean' }
	};
}

export const REKORDBOX_PARITY_STUBS: readonly SettingDef[] = [
	_todo('rb.quantize', 'Quantize', 'rekordbox', ['quantize', 'grid'], 'Global quantize default'),
	_todo('rb.sync_mode', 'Beat Sync mode default', 'rekordbox', ['sync', 'bar', 'beat'], 'BAR vs BEAT sync default'),
	_todo('rb.master_tempo', 'Master Tempo default', 'rekordbox', ['key', 'lock', 'tempo'], 'Keep original key when pitching'),
	_todo('rb.vinyl_mode', 'Vinyl mode', 'rekordbox', ['vinyl', 'scratch', 'cdj'], 'Jog vinyl / CDJ feel'),
	_todo('rb.jog_sensitivity', 'Jog sensitivity', 'rekordbox', ['jog', 'platter'], 'Platter touch sensitivity'),
	_todo('rb.hot_cue_colors', 'Hot cue color map', 'rekordbox', ['cue', 'color', 'pad'], 'Pad color scheme'),
	_todo('rb.pad_mode', 'Pad mode memory', 'rekordbox', ['pad', 'hotcue', 'sampler'], 'Remember last pad bank'),
	_todo('rb.waveform_zoom', 'Waveform zoom default', 'rekordbox', ['waveform', 'zoom'], 'Default overview zoom'),
	_todo('rb.grid_edit', 'Allow beat grid edit', 'rekordbox', ['grid', 'beat', 'edit'], 'Enable grid nudge/edit'),
	_todo('rb.auto_gain', 'Auto gain', 'rekordbox', ['gain', 'loudness'], 'Normalize channel gain on load'),
	_todo('rb.karaoke', 'Karaoke / vocal mute', 'rekordbox', ['karaoke', 'vocal'], 'Vocal-oriented mute presets'),
	_todo('rb.export_usb', 'USB export defaults', 'rekordbox', ['usb', 'export', 'device'], 'Device export preferences'),
	_todo('rb.analysis_quality', 'Analysis quality', 'rekordbox', ['analysis', 'anlz', 'pqtz'], 'BPM/key analysis quality'),
	_todo('rb.phrase_analysis', 'Phrase analysis', 'rekordbox', ['phrase', 'structure'], 'Enable phrase detection'),
	_todo('rb.mytag_layout', 'MyTag layout', 'rekordbox', ['mytag', 'tag'], 'MyTag browser layout'),
	_todo('rb.track_info_fields', 'Track info fields', 'rekordbox', ['info', 'columns'], 'Which columns show in info'),
	_todo('rb.keyboard_map', 'Keyboard mapping', 'rekordbox', ['keyboard', 'shortcuts', 'midi'], 'Custom key bindings'),
	_todo('rb.dual_deck_layout', 'Dual deck layout', 'rekordbox', ['layout', '2deck', '4deck'], '2 vs 4 deck chrome')
];

export const DJAY_PARITY_STUBS: readonly SettingDef[] = [
	_todo('djay.automix', 'Automix', 'djay', ['automix', 'auto'], 'Automix transitions'),
	_todo('djay.eq_kill', 'EQ kill switches', 'djay', ['eq', 'kill'], 'Instant EQ kills'),
	_todo('djay.effects_rack', 'Effects rack layout', 'djay', ['fx', 'effects'], 'FX slot layout'),
	_todo('djay.stems_ui', 'Stems mixer UI', 'djay', ['stems', 'vocal', 'drums'], 'Stem fader visibility'),
	_todo('djay.library_source', 'Library source', 'djay', ['itunes', 'apple', 'spotify'], 'External library source'),
	_todo('djay.streaming', 'Streaming services', 'djay', ['tidal', 'soundcloud', 'beatport'], 'Connected streaming'),
	_todo('djay.cue_points', 'Cue point style', 'djay', ['cue', 'points'], 'Cue marker style'),
	_todo('djay.midi_learn', 'MIDI learn', 'djay', ['midi', 'map', 'controller'], 'Controller MIDI learn'),
	_todo('djay.audio_device', 'Audio device', 'djay', ['device', 'output', 'asio'], 'Output device selection'),
	_todo('djay.sample_rate', 'Sample rate', 'djay', ['sample', 'rate', '48000'], 'Engine sample rate'),
	_todo('djay.buffer_size', 'Buffer size', 'djay', ['buffer', 'latency'], 'Audio buffer / latency'),
	_todo('djay.recording', 'Session recording', 'djay', ['record', 'rec', 'session'], 'REC defaults'),
	_todo('djay.video', 'Video deck', 'djay', ['video', 'visual'], 'Video deck enable'),
	_todo('djay.neumann', 'NEUMANN UI scale', 'djay', ['ui', 'scale', 'retina'], 'Interface scaling')
];
