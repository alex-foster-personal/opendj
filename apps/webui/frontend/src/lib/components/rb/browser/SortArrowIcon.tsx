import { sortArrowPath } from '$lib/ui/icon-glyphs';

import './SortArrowIcon.react.css';

// React twin of SortArrowIcon.svelte: same DOM, held to SortArrowIcon.contract.ts.
export type SortArrowIconProps = { asc: boolean };

export default function SortArrowIcon({ asc }: SortArrowIconProps) {
	return (
		<svg className="sort-arrow" viewBox="0 0 12 12" width="8" height="8" aria-hidden="true">
			<path d={sortArrowPath(asc)} fill="currentColor" />
		</svg>
	);
}
