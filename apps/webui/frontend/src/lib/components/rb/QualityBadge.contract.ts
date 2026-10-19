import { expect } from 'storybook/test';

/**
 * QualityBadge UI contract, shared by the Svelte reference story
 * (QualityBadge.stories.ts) and its React twin (QualityBadge.stories.tsx).
 * Both keep the reference title 'Components/QualityBadge' that the
 * numeric-readouts decision doc embeds, so their story ids match for
 * `pnpm story:parity`. See src/react/README.md.
 *
 * Shapes mirror apps/shared/audio_quality.py, the backend that actually
 * measures these. QualityBadge is pure props, so the args ARE the contract.
 * The rung shape is spelled out here rather than imported from
 * $lib/rb/library-types, so a story file does not widen that shared type
 * module's importer count.
 */

type Quality = {
	venue: string | null;
	label: string;
	rank: number | null;
	of: number;
	blurb: string;
	kbps: number | null;
	container: string;
	lossless: boolean;
};
type Args = { quality: Quality | null; showContainer?: boolean; compact?: boolean };
type Ctx = { canvasElement: HTMLElement; args: Args };

const rung = (over: Partial<Quality>): Quality => ({
	venue: 'club',
	label: 'Club',
	rank: 3,
	of: 6,
	blurb: 'holds up on a club rig',
	kbps: 256,
	container: '.mp3',
	lossless: false,
	...over
});

const badge = (root: HTMLElement): HTMLElement => {
	const el = root.querySelector<HTMLElement>('span.q-badge');
	if (el === null) throw new Error('no .q-badge rendered');
	return el;
};
const label = (root: HTMLElement): string => badge(root).querySelector('.q-label')?.textContent ?? '';
const ext = (root: HTMLElement): Element | null => badge(root).querySelector('.q-ext');

const CLUB_TITLE = 'Club -- holds up on a club rig. Effective 256 kbps, mp3. Rung 4 of 6.';

export const contracts = {
	/** Top rung: lossless, so the rung comes from format not bitrate. */
	Stadium: {
		args: {
			quality: rung({
				venue: 'stadium',
				label: 'Stadium',
				rank: 5,
				blurb: 'survives the biggest room',
				kbps: 1411,
				container: '.flac',
				lossless: true
			})
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-badge', 'q-stadium');
			await expect(el).toHaveAttribute('data-venue', 'stadium');
			await expect(label(canvasElement)).toBe('Stadium');
			await expect(ext(canvasElement)).toHaveTextContent('flac');
			await expect(el).toHaveAttribute(
				'title',
				'Stadium -- survives the biggest room. Effective 1411 kbps, flac. Rung 6 of 6. Lossless container, so the rung is set by format, not bitrate.'
			);
		}
	},
	Club: {
		args: { quality: rung({}) } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-club');
			await expect(el).not.toHaveClass('compact');
			await expect(label(canvasElement)).toBe('Club');
			await expect(ext(canvasElement)).toHaveTextContent('mp3');
			await expect(el).toHaveAttribute('title', CLUB_TITLE);
		}
	},
	/** Bottom rung: the badge is meant to look wrong at a glance. */
	NaughtyStep: {
		args: {
			quality: rung({
				venue: 'naughty_step',
				label: 'Naughty step',
				rank: 0,
				blurb: 'too lossy for anywhere',
				kbps: 96
			})
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-naughty_step');
			await expect(el).toHaveAttribute('data-venue', 'naughty_step');
			await expect(el).toHaveAttribute(
				'title',
				'Naughty step -- too lossy for anywhere. Effective 96 kbps, mp3. Rung 1 of 6.'
			);
		}
	},
	/**
	 * venue null: the backend could not measure it. The badge says Unknown and
	 * the hover carries the backend's REASON. It never guesses a rung, because
	 * a guessed rung would be mocked data.
	 */
	Unknown: {
		args: {
			quality: rung({
				venue: null,
				label: 'Unknown',
				rank: null,
				blurb: 'duration missing, cannot derive effective bitrate',
				kbps: null
			})
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-unknown');
			await expect(el).toHaveAttribute('data-venue', 'unknown');
			await expect(label(canvasElement)).toBe('Unknown');
			await expect(el).toHaveAttribute(
				'title',
				'Quality unknown -- duration missing, cannot derive effective bitrate, mp3.'
			);
		}
	},
	/** quality null: the row exists but its audio quality has not loaded yet. */
	NotLoaded: {
		args: { quality: null } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-unknown');
			await expect(label(canvasElement)).toBe('...');
			await expect(ext(canvasElement)).toBeNull();
			await expect(el).toHaveAttribute('title', 'audio quality not loaded yet for this row');
		}
	},
	/** Tight rows drop the container suffix; the hover still carries it. */
	WithoutContainer: {
		args: { quality: rung({}), showContainer: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(ext(canvasElement)).toBeNull();
			await expect(badge(canvasElement)).toHaveAttribute('title', CLUB_TITLE);
		}
	},
	/** Compact: the rung's initial only, the .compact class, and the full title on hover. */
	Compact: {
		args: { quality: rung({ venue: 'warehouse', label: 'warehouse', rank: 4 }), compact: true, showContainer: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).toHaveClass('q-badge', 'q-warehouse', 'compact');
			await expect(label(canvasElement)).toBe('W');
			await expect(el).toHaveAttribute(
				'title',
				'warehouse -- holds up on a club rig. Effective 256 kbps, mp3. Rung 5 of 6.'
			);
		}
	}
};
