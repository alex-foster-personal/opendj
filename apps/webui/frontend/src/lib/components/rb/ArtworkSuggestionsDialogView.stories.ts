import type { Meta, StoryObj } from '@storybook/sveltekit';
import ArtworkSuggestionsDialogView from './ArtworkSuggestionsDialogView.svelte';

const NOTE =
	'* Deezer and iTunes terms do not allow caching their content; only save covers for tracks you own and bought on their platform.';

// A grey square stands in for the store image (the real dialog loads it through the engine, live).
const IMG =
	'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"><rect width="56" height="56" fill="%23555"/></svg>';

const suggestion = (id: string, source: 'deezer' | 'itunes', artist: string, title: string) => ({
	stable_id: id,
	source,
	source_label: source === 'deezer' ? 'Deezer' : 'iTunes',
	cover_url: `https://example.invalid/${id}.jpg`,
	hit_title: title,
	hit_artist: artist,
	track_title: title,
	track_artist: artist,
	image_url: IMG
});

const PENDING = [
	suggestion('s1', 'deezer', 'Charles B', 'The Phantom of the Opera - Techno Remix'),
	suggestion('s2', 'itunes', 'Vik Portos', "Capriccio D'inverno"),
	suggestion('s3', 'deezer', 'ASCO', 'Aria')
];

const meta = {
	title: 'Components/ArtworkSuggestionsDialogView',
	component: ArtworkSuggestionsDialogView,
	tags: ['autodocs'],
	args: {
		suggestions: PENDING,
		note: NOTE,
		selected: new Set(PENDING.map((s) => s.stable_id)),
		ontoggle: () => {},
		onsaveselected: () => {},
		onsaveall: () => {},
		onskip: () => {},
		onclose: () => {}
	}
} satisfies Meta<typeof ArtworkSuggestionsDialogView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** LIBM-189: every found cover ticked, the consent note under the buttons. */
export const AllTicked: Story = {};

/** One unticked: Save selected and Skip leave it pending. */
export const OneUnticked: Story = { args: { selected: new Set(['s1', 's3']) } };

/** A store image that could not be fetched on Save names the track. */
export const SaveFailed: Story = {
	args: {
		error: 's2: itunes answered 404 for the cover',
		selected: new Set(['s2']),
		suggestions: [PENDING[1]]
	}
};
