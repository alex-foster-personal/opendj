import {
	AUTO_GAIN_DEFAULT_TARGET_LUFS,
	AUTO_GAIN_TARGET_MAX_LUFS,
	AUTO_GAIN_TARGET_MIN_LUFS
} from '$lib/rb/auto-gain';
import type { SettingDef } from './catalog-types';

/** AUTOGAIN-05: Auto gain on/off. CLI/API: PUT /api/v1/ui-prefs {"auto_gain": {"enabled": ...}}. */
export const AUTO_GAIN_ENABLED_SETTING: SettingDef = {
	id: 'auto_gain.enabled',
	label: 'Auto gain',
	group: 'performance',
	keywords: ['auto gain', 'autogain', 'gain', 'trim', 'loudness', 'lufs', 'normalise', 'normalize', 'level', 'volume', 'replaygain'],
	title: 'Match every track to one loudness when it loads on a deck',
	detail:
		'Like rekordbox Auto Gain: at load each deck applies the target minus the track\'s measured integrated loudness (LUFS) at the trim stage, under the TRIM knob, which still works on top. A boost stops where the track\'s true peak would pass -1 dBTP. A track with no loudness analysis plays at 0 dB and its mixer readout says AG N/A. Click the AG readout under TRIM to set your own value for a track. Stored in ui-prefs.',
	implemented: true,
	control: { kind: 'boolean' }
};

/** AUTOGAIN-05: the loudness every track is matched to. */
export const AUTO_GAIN_TARGET_SETTING: SettingDef = {
	id: 'auto_gain.target_lufs',
	label: 'Auto gain target',
	group: 'performance',
	keywords: ['auto gain', 'autogain', 'target', 'reference', 'loudness', 'lufs', 'level'],
	title: 'The integrated loudness (LUFS) auto gain matches every track to',
	detail:
		'-12 LUFS by default: measured over this library it brings the middle 80% (10th to 90th percentile) of the 2,998 analysed tracks within about 0.3 LU of each other, because most quiet masters have no true-peak headroom to be boosted into. Louder targets boost more but leave quiet tracks behind; quieter ones cut more and need more master volume.',
	implemented: true,
	control: {
		kind: 'number',
		min: AUTO_GAIN_TARGET_MIN_LUFS,
		max: AUTO_GAIN_TARGET_MAX_LUFS,
		step: 0.5,
		defaultValue: AUTO_GAIN_DEFAULT_TARGET_LUFS,
		unit: ' LUFS'
	}
};
