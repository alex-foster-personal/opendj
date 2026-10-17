import { RELEASE_STAGE, RELEASE_STAGE_TITLE } from '$lib/release-stage';

import './AlphaBadge.react.css';

// React twin of AlphaBadge.svelte: same DOM, held to AlphaBadge.contract.ts.
export default function AlphaBadge() {
	return (
		<span className="release-stage" data-testid="release-stage-badge" title={RELEASE_STAGE_TITLE}>
			{RELEASE_STAGE}
		</span>
	);
}
