// perf-lab: the environment every measured Chromium launches with.
//
// Playwright hands the browser the driver's whole environment unless told
// otherwise, and settings such as MALLOC_ARENA_MAX, LD_PRELOAD, GLIBC_TUNABLES,
// PULSE_SERVER or Chromium's own runtime variables change browser CPU, memory
// and audio timing without reaching the manifest or the graphics identity. So a
// measured browser inherits only by allowlist, the same rule stack.py applies to
// the engine and vite: what a process needs to find its programs, home and
// temp space, plus the session locators a headful browser needs to reach its
// display and audio server (where they are, never how they behave).

const INHERITED = new Set([
	'PATH',
	'HOME',
	'USER',
	'LOGNAME',
	'TMPDIR',
	'TEMP',
	'TMP',
	// a headful browser's display and session: without these it cannot open a
	// window or reach the user's audio server at its default address
	'DISPLAY',
	'XAUTHORITY',
	'WAYLAND_DISPLAY',
	'XDG_RUNTIME_DIR',
	'DBUS_SESSION_BUS_ADDRESS',
	// Windows: a process cannot start, or find its profile, without these
	'SYSTEMROOT',
	'SYSTEMDRIVE',
	'WINDIR',
	'COMSPEC',
	'PATHEXT',
	'USERPROFILE',
	'APPDATA',
	'LOCALAPPDATA',
	'PROGRAMDATA'
]);

/** The allowlisted part of inherited; names are compared upper-cased, as
 * Windows compares them. Pass as chromium.launch({ env }). */
export function browserEnv(inherited = process.env) {
	return Object.fromEntries(Object.entries(inherited).filter(([k, v]) => v !== undefined && INHERITED.has(k.toUpperCase())));
}
