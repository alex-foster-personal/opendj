import { expect, fireEvent, fn, userEvent, within } from 'storybook/test';

import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemControl, StemControlState, StemDeckState } from '$lib/rb/stem-types';

/**
 * StemRow UI contract, shared by the Svelte story (StemRow.stories.ts) and
 * its React twin (StemRow.stories.tsx). See src/react/README.md.
 *
 * StemRow reads only `deck.deck_id` and `deck.stems`, so the fixture fills
 * those two and casts; every other DeckState field is irrelevant here.
 */

type Args = {
	deck: Pick<DeckState, 'deck_id' | 'stems'>;
	pending: boolean;
	testIdScope?: 'deck' | 'channel';
	onMute: (stem: StemControl) => Promise<void>;
	onSolo: (stem: StemControl) => Promise<void>;
	onLoad?: () => Promise<void>;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const ctl = (over: Partial<StemControlState> = {}): StemControlState => ({ muted: false, solo: false, gain: 0.5, ...over });

function deck(deck_id: 1 | 2 | 3 | 4, stems: Partial<StemDeckState>): DeckState {
	const full: StemDeckState = {
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
		alignment: null,
		controls: { vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() },
		error: null,
		load: null,
		...stems
	};
	return { deck_id, stems: full } as DeckState;
}

const chips = (root: HTMLElement): HTMLButtonElement[] => Array.from(root.querySelectorAll<HTMLButtonElement>('button.chip'));
const chipText = (root: HTMLElement): string[] => chips(root).map((c) => c.textContent?.trim() ?? '');

const READY_TIP = 'real htdemucs stems (demucs4) - click mute, Shift+click solo';

export const contracts = {
	/** Four-stem bundle: five live chips, mute and solo state shown, click mutes, Shift+click solos. */
	ReadyDemucs: {
		args: {
			deck: deck(2, {
				controls: { vocal: ctl({ muted: true }), instrumental: ctl(), drums: ctl({ solo: true }), bass: ctl(), other: ctl() }
			}),
			pending: false,
			onMute: fn(),
			onSolo: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const group = canvas.getByRole('group', { name: 'stem controls deck 2' });
			await expect(group).toHaveAttribute('data-stems-status', 'ready');
			await expect(group).toHaveAttribute('data-stems-state', 'ready');
			await expect(group).toHaveAttribute('title', READY_TIP);
			await expect(canvas.getByText('MUTE')).toHaveClass('mute');
			await expect(chipText(canvasElement)).toEqual(['VOCAL', 'INST', 'DRUMS', 'BASS', 'HARM']);
			for (const chip of chips(canvasElement)) await expect(chip).toBeEnabled();

			const vocal = canvas.getByTestId('stem-vocal-deck-2');
			await expect(vocal).toHaveClass('muted');
			await expect(vocal).toHaveAttribute('aria-pressed', 'true');
			await expect(vocal).toHaveAttribute('data-muted', 'true');
			await expect(vocal).toHaveAttribute('data-solo', 'false');
			await expect(vocal).toHaveAttribute('aria-busy', 'false');
			await expect(vocal).toHaveAttribute('data-unavailable', 'false');
			await expect(vocal).toHaveAttribute('data-performance-control', 'stem-vocal');
			await expect(vocal).toHaveAttribute('title', `VOCAL stem is muted now. Click to mute, Shift+click to solo. ${READY_TIP}`);
			const drums = canvas.getByTestId('stem-drums-deck-2');
			await expect(drums).toHaveClass('solo');
			await expect(drums).toHaveAttribute('title', `DRUMS stem is solo selected. Click to mute, Shift+click to solo. ${READY_TIP}`);
			const inst = canvas.getByRole('button', { name: 'INST stem mute deck 2; Shift+click solo' });
			await expect(inst).toHaveAttribute(
				'title',
				`INST stem is unmuted (other solo/group controls still apply). Click to mute, Shift+click to solo. ${READY_TIP}`
			);
			await expect(inst.style.getPropertyValue('--chip-color').trim()).toBe('var(--rb-stem-instrumental, #9ad67a)');

			await userEvent.click(inst);
			await expect(args.onMute).toHaveBeenCalledWith('instrumental');
			await fireEvent.click(canvas.getByTestId('stem-bass-deck-2'), { shiftKey: true });
			await expect(args.onSolo).toHaveBeenCalledWith('bass');
			await expect(args.onMute).toHaveBeenCalledTimes(1);
			// No status text while the chips speak for themselves.
			await expect(canvasElement.querySelector('.status')).toBeNull();
		}
	},
	/** Two-stem RoFormer bundle on a mixer channel: BASS/HARM hidden, DRUMS inert with the reason, 3-letter labels. */
	RoformerChannel: {
		args: {
			deck: deck(1, { source: 'roformer', model: 'roformer', layout: 'roformer2', available_controls: ['vocal', 'instrumental'] }),
			pending: false,
			testIdScope: 'channel',
			onMute: fn(),
			onSolo: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(chipText(canvasElement)).toEqual(['VOC', 'INS', 'DRU']);
			const drums = canvas.getByTestId('stem-drums-channel-1');
			await expect(drums).toBeDisabled();
			await expect(drums).toHaveClass('unavailable');
			await expect(drums).toHaveAttribute('data-unavailable', 'true');
			await expect(drums).toHaveAttribute(
				'title',
				'DRUMS is not a separate stem in this roformer2 - it is mixed into INST, so it cannot be muted on its own'
			);
			await expect(canvas.getByTestId('stem-vocal-channel-1')).toBeEnabled();
			await userEvent.click(canvas.getByTestId('stem-vocal-channel-1'));
			await expect(args.onMute).toHaveBeenCalledWith('vocal');
		}
	},
	/** A failed load with onLoad: chips disabled, a RETRY action button that calls onLoad. */
	FailedRetry: {
		args: {
			deck: deck(3, { status: 'error', source: null, model: null, layout: null, available_controls: [], error: 'decode failed' }),
			pending: false,
			onMute: fn(),
			onSolo: fn(),
			onLoad: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(chipText(canvasElement)).toEqual(['VOCAL', 'INST', 'DRUMS']);
			for (const chip of chips(canvasElement)) await expect(chip).toBeDisabled();
			const retry = canvas.getByRole('button', { name: 'Stems failed to load: decode failed. Click to retry. Deck 3.' });
			await expect(retry).toHaveClass('status', 'action', 'failed');
			await expect(retry).toHaveTextContent('STEMS FAILED - RETRY');
			await expect(retry).toHaveAttribute('data-stems-action', 'retry');
			await expect(retry).toHaveAttribute('data-testid', 'stem-status-deck-3');
			await userEvent.click(retry);
			await expect(args.onLoad).toHaveBeenCalledTimes(1);
		}
	},
	/** A cloud fetch in flight: a busy status label with file progress, chips busy and disabled. */
	Fetching: {
		args: {
			deck: deck(4, {
				status: 'loading',
				available_controls: [],
				load: { phase: 'fetching', progress: { files_total: 4, files_done: 2, bytes_done: 3 * 1024 * 1024 }, reason: null }
			}),
			pending: true,
			onMute: fn(),
			onSolo: fn(),
			onLoad: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const status = canvas.getByRole('status');
			await expect(status).toHaveClass('status', 'busy');
			await expect(status).toHaveTextContent('FETCHING STEMS 2/4');
			await expect(status).toHaveAttribute('data-stems-state', 'fetching');
			await expect(status).toHaveAttribute(
				'title',
				"Fetching this track's stems from the cloud: 2 of 4 files, 3.0 MB so far. The deck already plays; the stem buttons switch on when this finishes."
			);
			for (const chip of chips(canvasElement)) {
				await expect(chip).toBeDisabled();
				await expect(chip).toHaveAttribute('aria-busy', 'true');
			}
			await expect(args.onLoad).not.toHaveBeenCalled();
		}
	}
};
