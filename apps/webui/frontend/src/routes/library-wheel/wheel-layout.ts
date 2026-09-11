/** Pure sunburst geometry for the LIBUX-06 Library Wheel -- no rendering, no DOM. */
import type { LibraryWheelResponse, WheelTrack } from './types';

export interface WheelWedge {
	stableId: string | null; // null for a family/genre band wedge, set for a track leaf
	title: string;
	color: string;
	startAngle: number; // radians, 0 = +x axis
	endAngle: number;
	innerRadius: number;
	outerRadius: number;
	track: WheelTrack | null;
}

export interface WheelLayout {
	families: WheelWedge[];
	genres: WheelWedge[];
	tracks: WheelWedge[];
}

const TAU = 2 * Math.PI;
const FAMILY_BAND = { inner: 0.18, outer: 0.4 };
const GENRE_BAND = { inner: 0.42, outer: 0.62 };
const TRACK_BAND = { inner: 0.64, outer: 1.0 };

/** Track-leaf outer radius grows with axis_value (real data only -- a null
 * axis_value, e.g. a disabled axis, always renders at the band floor rather
 * than guessing a size). */
function trackOuterRadius(track: WheelTrack, maxAxisValue: number): number {
	if (track.axis_value === null || maxAxisValue <= 0) return TRACK_BAND.inner;
	const extent = Math.max(0, Math.min(1, track.axis_value / maxAxisValue));
	return TRACK_BAND.inner + extent * (TRACK_BAND.outer - TRACK_BAND.inner);
}

export function buildWheelLayout(data: LibraryWheelResponse): WheelLayout {
	const totalTracks = data.families.reduce((sum, f) => sum + f.track_count, 0);
	const maxAxisValue = Math.max(
		0,
		...data.families.flatMap((f) => f.genres.flatMap((g) => g.tracks.map((t) => t.axis_value ?? 0)))
	);

	const families: WheelWedge[] = [];
	const genres: WheelWedge[] = [];
	const tracks: WheelWedge[] = [];

	let familyCursor = 0;
	for (const family of data.families) {
		const familySpan = totalTracks > 0 ? (family.track_count / totalTracks) * TAU : 0;
		const familyStart = familyCursor;
		const familyEnd = familyCursor + familySpan;
		families.push({
			stableId: null,
			title: `${family.name} (${family.track_count})`,
			color: family.color,
			startAngle: familyStart,
			endAngle: familyEnd,
			innerRadius: FAMILY_BAND.inner,
			outerRadius: FAMILY_BAND.outer,
			track: null
		});

		let genreCursor = familyStart;
		for (const genre of family.genres) {
			const genreSpan = family.track_count > 0 ? (genre.track_count / family.track_count) * familySpan : 0;
			const genreStart = genreCursor;
			const genreEnd = genreCursor + genreSpan;
			genres.push({
				stableId: null,
				title: `${genre.tag} (${genre.track_count})`,
				color: family.color,
				startAngle: genreStart,
				endAngle: genreEnd,
				innerRadius: GENRE_BAND.inner,
				outerRadius: GENRE_BAND.outer,
				track: null
			});

			let trackCursor = genreStart;
			const trackSpan = genre.track_count > 0 ? genreSpan / genre.track_count : 0;
			for (const track of genre.tracks) {
				const trackStart = trackCursor;
				const trackEnd = trackCursor + trackSpan;
				tracks.push({
					stableId: track.stable_id,
					title: track.title ?? track.stable_id,
					color: family.color,
					startAngle: trackStart,
					endAngle: trackEnd,
					innerRadius: TRACK_BAND.inner,
					outerRadius: trackOuterRadius(track, maxAxisValue),
					track
				});
				trackCursor = trackEnd;
			}
			genreCursor = genreEnd;
		}
		familyCursor = familyEnd;
	}

	return { families, genres, tracks };
}

/** SVG path `d` for one donut-segment wedge, unit circle (radius scaled by caller). */
export function wedgePath(wedge: WheelWedge): string {
	const { startAngle, endAngle, innerRadius, outerRadius } = wedge;
	if (endAngle <= startAngle) return '';
	const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;
	const point = (radius: number, angle: number) => [radius * Math.cos(angle), radius * Math.sin(angle)];
	const [x1, y1] = point(outerRadius, startAngle);
	const [x2, y2] = point(outerRadius, endAngle);
	const [x3, y3] = point(innerRadius, endAngle);
	const [x4, y4] = point(innerRadius, startAngle);
	return [
		`M ${x1} ${y1}`,
		`A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${x2} ${y2}`,
		`L ${x3} ${y3}`,
		`A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${x4} ${y4}`,
		'Z'
	].join(' ');
}
