/**
 * Spoken names for keyboard selector node ids (#14068, epic #9372). Pure, no runtime imports.
 *
 * Selector registrations carry an id such as `deck.1.cues` or `mixer.ch2.cal.ceiling` and no
 * label, so the polite announcement would read dots aloud. Each dotted segment becomes one
 * spoken level, levels are joined with a comma ("Deck 1, Hot cues"), numbers stay digits, and
 * a segment missing from the tables is split on dashes and capitalised. Context-only parents
 * (`mixer`, `topbar`, `sampler`, `assign`, `stem`) are silent when a child follows, so
 * `mixer.ch2` is "Channel 2". DJ control names mimic rekordbox where it has one.
 */

/** Names keyed by `<parent segment>.<segment>`, checked before the plain segment table. */
const SCOPED_NAMES: Readonly<Record<string, string>> = {
	'sampler.l': 'Left sampler',
	'sampler.r': 'Right sampler',
	'assign.a': 'Crossfader assign A',
	'assign.b': 'Crossfader assign B',
	'stem.vocal': 'Vocal stem',
	'stem.instrumental': 'Instrumental stem',
	'stem.drums': 'Drums stem',
	'stem.bass': 'Bass stem',
	'stem.other': 'Other stem',
	'topbar.mode': 'App mode',
	'cues.mode': 'Pad mode',
	'cal.red': 'Red',
	'cal.ceiling': 'Ceiling'
};

/** Names for one segment wherever it sits. */
const SEGMENT_NAMES: Readonly<Record<string, string>> = {
	// sections and groups
	topbar: 'Top bar',
	waveforms: 'Waveforms',
	decks: 'Decks',
	library: 'Library',
	mixer: 'Mixer',
	deck: 'Deck',
	// deck parts
	header: 'Header',
	cues: 'Hot cues',
	loop: 'Loop',
	transport: 'Transport',
	jog: 'Jog wheel',
	pitch: 'Tempo',
	stems: 'Stems',
	'grid-adjust': 'Grid adjust',
	'grid-shift': 'Grid shift',
	undo: 'Undo',
	cluster: 'Loop controls',
	'jump-size-down': 'Beat jump size down',
	'jump-size-up': 'Beat jump size up',
	'jump-back': 'Beat jump back',
	'jump-forward': 'Beat jump forward',
	// channel strip and mixer
	trim: 'Trim',
	high: 'High EQ',
	mid: 'Mid EQ',
	low: 'Low EQ',
	filter: 'Filter',
	fader: 'Channel fader',
	cue: 'Headphone cue',
	fx: 'Color FX',
	cal: 'Calibration',
	crossfader: 'Crossfader',
	headphones: 'Headphones',
	'crossfade-curve': 'Crossfader curve',
	'group-pad': 'Group pad',
	// topbar controls
	posture: 'Resource posture',
	oj: 'OJ',
	'wave-settings': 'Waveform settings',
	master: 'Master volume',
	// library panes
	tree: 'Tree view',
	search: 'Search',
	tracks: 'Track list'
};

/** Prefixes of `<word><n>` segments, spoken as "<Word> <n>" (`ch2` is "Channel 2"). */
const NUMBERED_NAMES: Readonly<Record<string, string>> = {
	ch: 'Channel',
	deck: 'Deck',
	pad: 'Pad'
};

/** Parents whose name the child already carries, so they say nothing when a child follows. */
const SILENT_PARENTS: ReadonlySet<string> = new Set([
	'mixer',
	'topbar',
	'sampler',
	'assign',
	'stem'
]);

//----- helpers

function _capitalise(text: string): string {
	return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Unknown segment: words split on dashes, underscores and letter-digit edges, first one capitalised. */
function _fallbackName(segment: string): string {
	const words = segment
		.replace(/([a-z])(\d)/gi, '$1 $2')
		.split(/[^a-z0-9]+/i)
		.filter((word) => word !== '')
		.map((word) => word.toLowerCase());
	return _capitalise(words.join(' '));
}

function _segmentName(parent: string | undefined, segment: string): string {
	const scoped = parent === undefined ? undefined : SCOPED_NAMES[`${parent}.${segment}`];
	if (scoped !== undefined) return scoped;
	const named = SEGMENT_NAMES[segment];
	if (named !== undefined) return named;
	const numbered = /^([a-z]+)(\d+)$/.exec(segment);
	const prefix = numbered === null ? undefined : NUMBERED_NAMES[numbered[1]];
	if (numbered !== null && prefix !== undefined) return `${prefix} ${numbered[2]}`;
	return _fallbackName(segment);
}

//----- public

/** The words a screen reader speaks for node `id`; a non-blank explicit `label` wins. */
export function selectorSpokenName(id: string, label?: string | null): string {
	if (label != null && label.trim() !== '') return label;
	const segments = id.split('.').filter((segment) => segment !== '');
	const levels: string[] = [];
	segments.forEach((segment, i) => {
		const isLast = i === segments.length - 1;
		if (/^\d+$/.test(segment) && levels.length > 0) {
			levels[levels.length - 1] += ` ${segment}`;
		} else if (!isLast && SILENT_PARENTS.has(segment)) {
			return;
		} else {
			levels.push(_segmentName(segments[i - 1], segment));
		}
	});
	return levels.filter((level) => level !== '').join(', ');
}
