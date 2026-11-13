// requirement: IOPIN-12692 / issue #12692 (calibration stimulus through typed IPC)
// [if] headphone_calibration_probe is dispatched with click_train [then] the persisted config and the serialized diagnostics both read click_train [else stop].
// [if] the value is not chirp or click_train, or an extra field is sent [then] the command is refused and nothing changes [else stop].
// [if] a calibration run is in flight [then] the stimulus change is refused [else stop].
// [if] the operator picks Click train in the I/O panel stimulus select [then] onprobe receives click_train, mixer config storage is untouched, and the select follows the read model [else stop].
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { bundleHeadphoneClusterClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

const MIXER_CONFIG_STORAGE_KEY = 'mdt.rb.mixer-config.v1';
/** state.svelte.ts keeps one mixer read model per realm under this global. */
const MIXER_STATE_SINGLETON_KEY = '__mdtMixerStateSingleton';

/** Minimal localStorage + window so mixer-config persistence is live. */
function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

/** Every element under root, in document order. */
function allElements(root, hits = []) {
	for (const child of root.childNodes ?? []) {
		if (child.nodeType === 1) hits.push(child);
		allElements(child, hits);
	}
	return hits;
}

/** The option the select currently shows, as the operator would read it. */
function selectedOption(select) {
	return select.options.find((option) => option.selected) ?? null;
}

/**
 * The shim DOM has no form controls: give it the select/option surface Svelte
 * drives (`options`, `selected`, `selectedIndex`) with one selection per select,
 * plus the `toggleAttribute` the keyboard selector leaves use.
 */
function installSelectControls() {
	const proto = globalThis.Element.prototype;
	const descriptors = {
		options: {
			configurable: true,
			get() {
				return allElements(this).filter((node) => node.nodeName === 'OPTION');
			}
		},
		selected: {
			configurable: true,
			get() {
				return this._selected === true;
			},
			set(value) {
				if (value) {
					let select = this.parentNode;
					while (select && select.nodeName !== 'SELECT') select = select.parentNode;
					for (const option of select?.options ?? []) option._selected = false;
				}
				this._selected = Boolean(value);
			}
		},
		selectedIndex: {
			configurable: true,
			get() {
				return this.options.findIndex((option) => option.selected);
			},
			set(index) {
				this.options.forEach((option, i) => (option._selected = i === index));
			}
		},
		// The selectorNode action on the headphone leaves sets data-sel-disabled with toggleAttribute.
		toggleAttribute: {
			configurable: true,
			value(name, force) {
				if (force) this.setAttribute(name, '');
				else this.removeAttribute(name);
				return Boolean(force);
			}
		}
	};
	Object.defineProperties(proto, descriptors);
	globalThis.HTMLSelectElement = class {
		static [Symbol.hasInstance](node) {
			return node?.nodeName === 'SELECT';
		}
	};
	// Svelte re-selects when options change; this harness changes none after mount.
	globalThis.MutationObserver = class {
		observe() {}
		disconnect() {}
	};
	return () => {
		for (const name of Object.keys(descriptors)) delete proto[name];
		delete globalThis.HTMLSelectElement;
		delete globalThis.MutationObserver;
	};
}

/** Mount the real HeadphoneCluster, I/O panel open in two_outputs, recording onprobe calls. */
async function mountHeadphoneCluster() {
	const teardownDom = installQualityBadgeDom();
	const store = new Map();
	globalThis.localStorage = {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, String(value)),
		removeItem: (key) => store.delete(key)
	};
	const restoreSelects = installSelectControls();
	// The shim window is globalThis; module-level window listeners need a no-op surface.
	const windowEvents = new EventTarget();
	globalThis.addEventListener = windowEvents.addEventListener.bind(windowEvents);
	globalThis.removeEventListener = windowEvents.removeEventListener.bind(windowEvents);
	// Earlier tests built the mixer singleton with identity runes; this mount needs a reactive one.
	const priorMixerState = globalThis[MIXER_STATE_SINGLETON_KEY];
	delete globalThis[MIXER_STATE_SINGLETON_KEY];
	const client = await bundleHeadphoneClusterClient();
	const hp = client.mixerState.headphones;
	hp.output_mode = 'two_outputs';
	hp.calibration.diagnostics.probe = 'chirp';
	client.openIoView();

	const probes = [];
	const noop = () => {};
	const target = document.createElement('div');
	document.body.appendChild(target);
	const instance = client.mount(client.HeadphoneCluster, {
		target,
		props: {
			state: hp,
			onmix: noop,
			onlevel: noop,
			ondelay: noop,
			onrefresh: async () => ({ ok: true }),
			onacquire: noop,
			onselect: noop,
			onmaster: noop,
			oninput: noop,
			onmode: noop,
			oncalibrate: noop,
			onAlignmentMode: noop,
			onprobe: (probe) => probes.push(probe),
			testrun: (work) => work(() => true, new AbortController().signal)
		}
	});
	client.flushSync();

	return {
		client,
		probes,
		storage: store,
		byLabel(label) {
			const node = allElements(target).find((el) => el.getAttribute?.('aria-label') === label);
			assert.ok(node, `no element labelled ${label}`);
			return node;
		},
		change(node) {
			node.dispatchEvent({
				type: 'change',
				bubbles: true,
				cancelBubble: false,
				stopPropagation() {
					this.cancelBubble = true;
				}
			});
			client.flushSync();
		},
		teardown() {
			client.unmount(instance);
			target.remove();
			client.closeIoView();
			restoreSelects();
			delete globalThis.localStorage;
			delete globalThis.addEventListener;
			delete globalThis.removeEventListener;
			teardownDom();
			globalThis[MIXER_STATE_SINGLETON_KEY] = priorMixerState;
		}
	};
}

describe('IOPIN-12692 calibration stimulus IPC', () => {
	test('headphone_calibration_probe persists the probe and updates the serialized diagnostics', async () => {
		const store = installFakeWindow();
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		assert.deepEqual(
			ipc.performanceCommandQueueScopes({ type: 'headphone_calibration_probe', value: 'chirp' }),
			['headphone']
		);
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			await ipc.dispatchPerformanceCommand({
				type: 'headphone_calibration_probe',
				value: 'click_train'
			});
			const diagnostics = ipc.queryPerformanceState().mixer.headphones.calibration.diagnostics;
			assert.equal(diagnostics.probe, 'click_train');
			assert.equal(diagnostics.alternate_probe, 'chirp');
			assert.equal(
				JSON.parse(store.get(MIXER_CONFIG_STORAGE_KEY)).calibration_probe,
				'click_train'
			);

			await assert.rejects(
				ipc.dispatchPerformanceCommand({
					type: 'headphone_calibration_probe',
					value: 'pink_noise'
				}),
				/calibration probe must be chirp or click_train/
			);
			await assert.rejects(
				ipc.dispatchPerformanceCommand({
					type: 'headphone_calibration_probe',
					value: 'chirp',
					extra: 1
				}),
				/unexpected fields/
			);
			assert.equal(
				ipc.queryPerformanceState().mixer.headphones.calibration.diagnostics.probe,
				'click_train'
			);
			await ipc.dispatchPerformanceCommand({ type: 'headphone_calibration_probe', value: 'chirp' });
			assert.equal(
				ipc.queryPerformanceState().mixer.headphones.calibration.diagnostics.probe,
				'chirp'
			);
		} finally {
			uninstall();
		}
	});

	test('setCalibrationProbe refuses while a calibration run is in flight', async () => {
		installFakeWindow();
		const session = await loadTypeScriptModule('src/lib/rb/cue-align-session.svelte.ts');
		// The run is claimed before its first await; the refusing factory then ends it as failed.
		session.overrideCueAlignEffects(() => {
			throw new Error('test: effects refused');
		});
		try {
			const run = session.startCueAlignment({ interactive: false });
			assert.equal(session.cueAlignmentRunning(), true);
			assert.throws(() => session.setCalibrationProbe('click_train'), /calibration is running/);
			await assert.rejects(run, /test: effects refused/);
			assert.equal(session.cueAlignmentRunning(), false);
			session.setCalibrationProbe('click_train');
		} finally {
			session.overrideCueAlignEffects(null);
		}
	});

	test('the I/O panel select shows the read model probe and a pick calls onprobe, not mixer config', async () => {
		const harness = await mountHeadphoneCluster();
		try {
			const select = harness.byLabel('calibration stimulus');
			assert.equal(selectedOption(select)?.textContent, 'Sweep (chirp)');

			const configBefore = harness.storage.get(MIXER_CONFIG_STORAGE_KEY);
			select.value = 'click_train';
			harness.change(select);
			assert.deepEqual(harness.probes, ['click_train']);
			assert.equal(harness.storage.get(MIXER_CONFIG_STORAGE_KEY), configBefore);

			// The select follows the read model, so the pick shows once the command lands.
			harness.client.mixerState.headphones.calibration.diagnostics.probe = 'click_train';
			harness.client.flushSync();
			assert.equal(selectedOption(select)?.textContent, 'Click train');
		} finally {
			harness.teardown();
		}
	});
});
