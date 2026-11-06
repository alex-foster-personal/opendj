/**
 * QLT letter rendering (QLTSCAN-05), shared by QualityBadge.svelte and its
 * React twin so the two cannot drift.
 *
 * The backend decides the letter, whether it was checked and the issues
 * (apps/shared/audio_grade.py). A listing row carries only those three; the
 * full hover text is loaded on hover (`loadHover` on the badge) because it
 * would push every row past its byte budget. Until it arrives the title is a
 * short summary built here. A grade that was not checked is drawn hollow; a
 * grade with issues carries a dot. Nothing here invents a letter.
 */

export type GradeLetter = 'A' | 'B' | 'C' | 'D';

export interface TrackQualityGrade {
	/** Null when the venue is unknown (no file, streaming, no duration). */
	letter: GradeLetter | null;
	/** False until a scan measured the current file under the current rule. */
	checked: boolean;
	/** Issue keys (clipping, dc_offset, ...); any key puts a dot on the badge. */
	issues: string[];
}

/** The first hover line's meaning per letter: mirrors MEANINGS in audio_grade.py. */
export const GRADE_MEANINGS: Record<GradeLetter, string> = {
	A: 'Safe on any system',
	B: 'Good on a club system',
	C: 'Thin on a big rig',
	D: 'Rough, replace if you can'
};

export interface GradeView {
	label: string;
	classes: string;
	/** The summary title shown until the full hover has loaded. */
	title: string;
	dot: boolean;
	dataGrade: string;
}

/** Key for badge hover-state (QLTSCAN-05): track id plus serialized grade, aligned with `api-audio-quality` fetch keys. */
export function qualityHoverStateKey(
	trackKey: string,
	grade: TrackQualityGrade | null | undefined
): string {
	return `${trackKey}|${JSON.stringify(grade ?? null)}`;
}

function _summary(grade: TrackQualityGrade): string {
	if (grade.letter === null) return 'Quality unknown';
	const head = grade.checked
		? `${grade.letter} -- ${GRADE_MEANINGS[grade.letter]}`
		: `${grade.letter} (not checked yet) -- from bitrate and format only`;
	if (!grade.checked) return head;
	const issues = grade.issues.length
		? grade.issues.map((key) => key.replaceAll('_', ' ')).join(', ')
		: 'none found';
	return `${head}\nIssues: ${issues}`;
}

/** Label, classes and summary title for a grade. `compact` shows the letter alone. */
export function gradeView(grade: TrackQualityGrade, compact: boolean): GradeView {
	const letter = grade.letter;
	const key = letter ?? 'none';
	const label = letter === null ? '?' : compact ? letter : `${letter} -- ${GRADE_MEANINGS[letter]}`;
	const classes = ['q-badge', 'q-grade', `q-grade-${key}`];
	if (!grade.checked) classes.push('hollow');
	if (compact) classes.push('compact');
	return {
		label,
		classes: classes.join(' '),
		title: _summary(grade),
		dot: grade.issues.length > 0,
		dataGrade: grade.checked ? key : `${key}-unchecked`
	};
}
