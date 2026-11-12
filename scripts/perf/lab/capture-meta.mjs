// perf-lab: plain capture metadata the driver and abwin.mjs share.
//
// Which Chromium runs a capture, and why a capture that ran to the end still
// measured a broken app. Comparability (whether two captures ran the same
// commit, dependencies, browser build and fixture bytes) is deliberately not
// here: `stack.py up` writes one manifest (manifest.py), and each capture
// copies it.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { browserEnv } from './browser-env.mjs';
import { pinnedExecutable } from './resolve-exe.mjs';

/** Why a capture that ran to the end still measured a broken app, as a list
 * of reasons ([] when nothing is wrong). A latency press that never reached
 * its control, or that the app never reflected, left that row's path
 * unexercised, and a deck still carrying a processor error at the end failed
 * during a scenario no health check watched (latency,
 * leak). Either voids the capture rather than publishing its numbers. */
export function captureFaults({ latencyRows = [], finalQuery = null } = {}) {
	const why = [];
	const failed = latencyRows.filter((r) => r.press_error);
	if (failed.length) {
		const r = failed[0];
		why.push(`${failed.length} latency press(es) never took effect, first ${r.action}: ${String(r.press_error).slice(0, 200)}`);
	}
	for (const [deck, d] of Object.entries(finalQuery ?? {})) {
		if (d?.processor_error) why.push(`deck ${deck} ended the capture with processor_error=${d.processor_error}`);
	}
	return why;
}

/** An installed Playwright Chromium, or null: PLAYWRIGHT_BROWSERS_PATH first,
 * then Playwright's default cache on Linux (~/.cache), macOS
 * (~/Library/Caches) and Windows (%LOCALAPPDATA%), newest build first. The
 * frontend may pin a Playwright whose own build is not installed, so the
 * drivers fall back to this rather than downloading one. */
export function playwrightChromium(env = process.env) {
	const home = env.HOME || env.USERPROFILE || '';
	const roots = [
		env.PLAYWRIGHT_BROWSERS_PATH,
		home && path.join(home, '.cache', 'ms-playwright'),
		home && path.join(home, 'Library', 'Caches', 'ms-playwright'),
		env.LOCALAPPDATA && path.join(env.LOCALAPPDATA, 'ms-playwright')
	].filter(Boolean);
	const build = (n) => Number(n.slice('chromium-'.length));
	for (const root of roots) {
		if (!fs.existsSync(root)) continue;
		const builds = fs.readdirSync(root).filter((n) => /^chromium-\d+$/.test(n)).sort((a, b) => build(b) - build(a));
		for (const d of builds) {
			for (const sub of fs.readdirSync(path.join(root, d)).sort()) {
				const rel = sub.startsWith('chrome-linux') ? ['chrome']
					: sub.startsWith('chrome-mac') ? ['Chromium.app', 'Contents', 'MacOS', 'Chromium']
					: sub.startsWith('chrome-win') ? ['chrome.exe'] : null;
				const p = rel && path.join(root, d, sub, ...rel);
				if (p && fs.existsSync(p)) return path.resolve(p);
			}
		}
	}
	return null;
}

/** An operator's --chrome as an absolute path, which must exist. */
export function explicitChrome(p) {
	const given = path.resolve(String(p ?? ''));
	if (!fs.existsSync(given) || !fs.statSync(given).isFile()) throw new Error(`--chrome ${given} is not a file`);
	return given;
}

/** The Chromium a capture launches, as an absolute path: an explicit --chrome
 * first, then the given Playwright `chromium`'s own build when installed, then
 * any installed Playwright Chromium. The driver and browser-id.mjs (which
 * `stack.py up` records in the manifest) both resolve through here. */
export function resolveChrome(chromium, explicit) {
	if (explicit) return explicitChrome(explicit);
	try {
		const own = chromium.executablePath();
		if (fs.existsSync(own)) return path.resolve(own);
	} catch { /* fall through */ }
	const found = playwrightChromium();
	if (found) return found;
	throw new Error('no Chromium found: pass --chrome <path>');
}

/** The graphics stack this browser renders with, as Chromium's GPU process
 * reports it (CDP SystemInfo.getInfo): each GPU's vendor, device and driver,
 * and the GL renderer, vendor, version and implementation in use. Two
 * machines of one host class with other GPUs or drivers, or one browser
 * falling back to software, paint and composite at different cost, so the
 * ledger compares captures only within one graphics identity. Only stable
 * fields are kept (no init time or crash count). Throws when the report has
 * no GPU section, so an unreadable stack is never an empty identity. */
export async function graphicsIdentity(browser) {
	const session = await browser.newBrowserCDPSession();
	try {
		const { gpu } = await session.send('SystemInfo.getInfo');
		const aux = gpu?.auxAttributes;
		if (!Array.isArray(gpu?.devices) || !gpu.devices.length || !aux) throw new Error('SystemInfo.getInfo reported no GPU devices');
		const devices = gpu.devices.map((d) => ({
			vendor: `${d.vendorString} (0x${d.vendorId.toString(16)})`,
			device: `${d.deviceString} (0x${d.deviceId.toString(16)})`,
			driver: `${d.driverVendor} ${d.driverVersion}`
		}));
		const id = { devices, gl_renderer: aux.glRenderer, gl_vendor: aux.glVendor, gl_version: aux.glVersion, gl_implementation: aux.glImplementationParts, skia_backend: aux.skiaBackendType ?? null };
		const missing = ['gl_renderer', 'gl_vendor', 'gl_version', 'gl_implementation'].filter((k) => typeof id[k] !== 'string' || !id[k]);
		if (missing.length) throw new Error(`SystemInfo.getInfo reported no ${missing.join(', ')}`);
		return id;
	} finally {
		await session.detach();
	}
}

// Windows: the default render endpoint (eRender, eConsole) through the Core
// Audio IMMDeviceEnumerator, compiled inline by PowerShell's Add-Type: the
// endpoint id and its PKEY_Device_FriendlyName, printed as "id<TAB>name".
// Every interface is [ComImport] with its IID and InterfaceIsIUnknown, or the
// runtime will not cast the COM object to it; each declares its methods in
// the vtable order of the Windows SDK headers up to the last one called
// (mmdeviceapi.h IMMDeviceEnumerator: EnumAudioEndpoints,
// GetDefaultAudioEndpoint; IMMDevice: Activate, OpenPropertyStore, GetId;
// propsys.h IPropertyStore: GetCount, GetAt, GetValue). The slots before the
// one called are placeholders that are never invoked.
const WINDOWS_DEFAULT_OUTPUT = `
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class OdjMMDeviceEnumerator {}
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IOdjMMDeviceEnumerator {
  int EnumAudioEndpoints();
  [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IOdjMMDevice device);
}
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IOdjMMDevice {
  int Activate();
  [PreserveSig] int OpenPropertyStore(int access, out IOdjPropertyStore store);
  [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IOdjPropertyStore {
  int GetCount();
  int GetAt();
  [PreserveSig] int GetValue(ref OdjPropertyKey key, out OdjPropVariant value);
}
[StructLayout(LayoutKind.Sequential)] struct OdjPropertyKey { public Guid fmtid; public int pid; }
[StructLayout(LayoutKind.Explicit, Size = 24)] struct OdjPropVariant { [FieldOffset(0)] public short vt; [FieldOffset(8)] public IntPtr p; }
public static class OdjDefaultOutput {
  public static string Read() {
    var e = (IOdjMMDeviceEnumerator)(new OdjMMDeviceEnumerator());
    IOdjMMDevice d;
    Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0, 0, out d));
    string id;
    Marshal.ThrowExceptionForHR(d.GetId(out id));
    IOdjPropertyStore ps;
    Marshal.ThrowExceptionForHR(d.OpenPropertyStore(0, out ps));
    var key = new OdjPropertyKey { fmtid = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), pid = 14 };
    OdjPropVariant v;
    Marshal.ThrowExceptionForHR(ps.GetValue(ref key, out v));
    return id + "\\t" + (v.vt == 31 ? Marshal.PtrToStringUni(v.p) : "");
  }
}
'@
[OdjDefaultOutput]::Read()
`;

/** The concrete output device behind the OS default, read from the OS
 * through a platform seam, since AudioContext.sinkId is '' for the default
 * whichever device that is and enumerateDevices hides labels without a
 * microphone grant (which would open the real microphone on a workstation):
 *   linux   the PulseAudio/PipeWire default sink (pactl get-default-sink) and
 *           its `pactl list sinks short` line (driver module and sample spec),
 *           read with the measured browser's own environment, so the same
 *           sound server Chromium reaches;
 *   darwin  the CoreAudio default output in system_profiler SPAudioDataType:
 *           its name, manufacturer, transport and sample rate;
 *   win32   the Core Audio default render endpoint (IMMDeviceEnumerator
 *           GetDefaultAudioEndpoint(eRender, eConsole)): its endpoint id and
 *           friendly name, through PowerShell (WINDOWS_DEFAULT_OUTPUT);
 *   other   not read.
 * Every reader is launched by its resolved path, pinned for the life of the
 * process (resolve-exe.mjs), and that path is recorded as device_reader.
 * Returns { device, device_unknown: null, device_reader }, or { device: null,
 * device_unknown: why, device_reader } when the device cannot be determined
 * (no reader, no sound server, no default, an unsupported platform): fail
 * closed, never a guess. The ledger and the A/B refuse to compare a capture
 * whose device is unknown, so two unknowns are never taken as one output. */
export function defaultOutputDevice(env = browserEnv(), platform = process.platform) {
	let reader = null;
	const run = (args) => execFileSync(reader, args, { env, encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
	const unknown = (why) => ({ device: null, device_unknown: why, device_reader: reader });
	const known = (device) => ({ device, device_unknown: null, device_reader: reader });
	try {
		if (platform === 'linux') {
			reader = pinnedExecutable('pactl', env, platform);
			if (!reader) return unknown('pactl is not on PATH as an executable file');
			const sink = run(['get-default-sink']);
			if (!sink) return unknown('pactl names no default sink');
			const line = run(['list', 'sinks', 'short']).split('\n').map((l) => l.split('\t')).find((f) => f[1] === sink);
			if (!line) return unknown(`pactl's default sink ${sink} is not among its sinks`);
			// name, owning module/driver and sample spec; not the index or the RUNNING/IDLE state
			return known(`pulse:${sink} ${line[2] ?? ''} ${line[3] ?? ''}`.trim());
		}
		if (platform === 'darwin') {
			// the system's own copy, by absolute path: a PATH entry could shadow it
			if (!fs.existsSync('/usr/sbin/system_profiler')) return unknown('/usr/sbin/system_profiler is missing');
			reader = fs.realpathSync('/usr/sbin/system_profiler');
			const items = JSON.parse(run(['SPAudioDataType', '-json'])).SPAudioDataType?.flatMap((g) => g._items ?? []) ?? [];
			const out = items.find((d) => d.coreaudio_default_audio_output_device === 'spaudio_yes');
			if (!out) return unknown('system_profiler names no default audio output device');
			const f = (k) => out[k] ?? '';
			return known(`coreaudio:${f('_name')} | ${f('coreaudio_device_manufacturer')} | ${f('coreaudio_device_transport')} | ${f('coreaudio_device_srate')}`);
		}
		if (platform === 'win32') {
			reader = pinnedExecutable('powershell', env, platform);
			if (!reader) return unknown('powershell is not on PATH as an executable file');
			const encoded = Buffer.from(WINDOWS_DEFAULT_OUTPUT, 'utf16le').toString('base64');
			const [id, name] = run(['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded]).split('\t');
			if (!id) return unknown('Core Audio names no default render endpoint');
			return known(`mmdevice:${id} | ${name ?? ''}`);
		}
		return unknown(`no default-output reader on ${platform}`);
	} catch (e) {
		return unknown(`cannot read the default output: ${String(e?.stderr || e?.message || e).trim().split('\n')[0].slice(0, 200)}`);
	}
}

/** The audio output this browser plays through, as a fresh AudioContext
 * with the default options reports it once it is running: the sink (the
 * default output is '', null where AudioContext.sinkId is unsupported), the
 * device sample rate and channel count, and the processing buffer latency
 * (baseLatency, in ms rounded to 0.001). outputLatency is NOT part of the
 * identity: it is the sound server's live estimate of the stream's delay, and
 * on a real PulseAudio server it read 0, then 32, then 40 ms across reads in
 * one browser (Thu 15 Oct 2026), so as identity it would void every capture;
 * driver.mjs records it per latency row as a measurement instead. Another
 * output device, sample rate or buffer configuration sets
 * presented-audio latency and audio CPU, not the app, so the ledger compares
 * captures only within one output identity. Throws when the context never
 * runs or never advances, so an unmeasured output is never an empty
 * identity. The probe page is closed again before anything is measured.
 * device / device_unknown name the concrete output (defaultOutputDevice). */
export async function audioOutputIdentity(browser, env = browserEnv()) {
	const context = await browser.newContext();
	try {
		const page = await context.newPage();
		const format = await page.evaluate(async () => {
			const ctx = new AudioContext();
			try {
				await ctx.resume();
				// a silent source keeps the output path rendering while it is read
				const src = ctx.createConstantSource();
				const mute = ctx.createGain();
				mute.gain.value = 0;
				src.connect(mute).connect(ctx.destination);
				src.start();
				const t = performance.now();
				while ((ctx.state !== 'running' || ctx.currentTime === 0) && performance.now() - t < 5000) await new Promise((r) => setTimeout(r, 20));
				if (ctx.state !== 'running' || ctx.currentTime === 0) throw new Error(`audio output never ran (state ${ctx.state}, currentTime ${ctx.currentTime})`);
				await new Promise((r) => setTimeout(r, 300));
				const ms = (s) => Math.round(s * 1e6) / 1e3;
				return {
					sink_id: typeof ctx.sinkId === 'string' ? ctx.sinkId : null,
					sample_rate: ctx.sampleRate,
					channels: ctx.destination.maxChannelCount,
					base_latency_ms: ms(ctx.baseLatency)
				};
			} finally {
				await ctx.close();
			}
		});
		// the device behind the default, read after the context ran on it
		return { ...format, ...defaultOutputDevice(env) };
	} finally {
		await context.close();
	}
}

/** What a measuring browser launch ran on: its graphics stack and its audio
 * output. Read once per launch, before any measured page exists. */
export async function launchIdentity(browser) {
	return { graphics: await graphicsIdentity(browser), audio_output: await audioOutputIdentity(browser) };
}

/** The fields (graphics.<key>, audio_output.<key>) in which two launch
 * identities differ, sorted; [] when they are the same. A section one of
 * them lacks is named whole. */
export function identityChanges(a, b) {
	const out = [];
	for (const section of ['graphics', 'audio_output']) {
		const x = a?.[section], y = b?.[section];
		if (!x || !y) {
			if (x !== y) out.push(section);
			continue;
		}
		for (const k of new Set([...Object.keys(x), ...Object.keys(y)])) if (JSON.stringify(x[k]) !== JSON.stringify(y[k])) out.push(`${section}.${k}`);
	}
	return out.sort();
}

/** Why a browser's launch identity no longer holds once its measuring is
 * over, or null: launchIdentity read again and compared with what was
 * stamped at launch (identityChanges), or the re-read failing. A GPU process
 * that crashed and came back on another backend (or fell back to software,
 * or none) mid-launch, or an output switched mid-launch, painted or played
 * the measured work on another stack, though every launch stamp agrees.
 * driver.mjs and abwin.mjs both call this before closing a measuring
 * browser, so the two paths cannot drift apart. */
export async function identityDrift(browser, stamped) {
	let now;
	try {
		now = await launchIdentity(browser);
	} catch (e) {
		return `the browser identity could not be read again before closing: ${String(e?.message ?? e).slice(0, 200)}`;
	}
	const changed = identityChanges(stamped, now);
	return changed.length ? `the browser identity changed during the launch (${changed.join(', ')})` : null;
}

/** The revision a capture against a `stack.py up` state ran: the stack's
 * head, which names a dirty tree as <sha>+dirty.<hash> (stack.py
 * --allow-dirty), so ledger.py refuses it as a commit and dirty numbers are
 * never filed under the clean SHA. The manifest's bare commit is for
 * comparisons only. Throws for a state without a head. */
export function stackRevision(state, file = 'the stack state') {
	if (typeof state?.head !== 'string' || !state.head) throw new Error(`${file} names no head; bring the stack up with this stack.py`);
	return state.head;
}
