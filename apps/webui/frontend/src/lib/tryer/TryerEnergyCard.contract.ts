import { expect } from 'storybook/test';

import { TRYER_ENERGY_FIXTURE } from './energy-fixture';
import { energyLineSegments, energyStripReady } from './tryer-model';

type Ctx = { canvasElement: HTMLElement };

export const contracts = {
	/** Fixture candidate with a scored MIK energy block (issue #10132). */
	Scored: {
		args: { energy: TRYER_ENERGY_FIXTURE },
		play: async ({ canvasElement }: Ctx) => {
			const line = canvasElement.querySelector('[data-testid="tryer-energy-line"]');
			await expect(line).not.toBeNull();
			await expect(line).toHaveTextContent(TRYER_ENERGY_FIXTURE.line);
			await expect(energyStripReady(TRYER_ENERGY_FIXTURE)).toBe(true);
			const segments = energyLineSegments(TRYER_ENERGY_FIXTURE);
			await expect(segments.filter((s) => s.title !== null).length).toBe(
				TRYER_ENERGY_FIXTURE.numbers.length
			);
			const strip = canvasElement.querySelector('.strip');
			await expect(strip).not.toBeNull();
		}
	}
};
