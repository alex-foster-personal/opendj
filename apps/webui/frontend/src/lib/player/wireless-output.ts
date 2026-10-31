/**
 * AIRPLAY-MAIN-01: AirPlay speakers as the room (MASTER / MAIN) output.
 *
 * The Mac shell tags AirPlay outputs with CoreAudio transport `airplay`
 * (`apps/desktop/src-tauri/src/cue_sink.rs`, transport_name). AirPlay 2
 * buffers about 2 s before it plays, longer than the 1500 ms the ROOM delay
 * can add (MASTER_DELAY_MAX_MS), so the room can never be lined up with the
 * headphones: the DJ cues and monitors locally and the speaker is a delayed
 * broadcast. Such an output may be MAIN, never HEADPHONE CUE, and is never
 * auto-picked as the room. Pure, so the picker and the router share one rule.
 */
import type { HeadphoneOutputDevice } from "$lib/rb/mixer-types";

/** Transports that play seconds late. Only AirPlay is classified today. */
const _WIRELESS_DELAYED_TRANSPORTS: ReadonlySet<string> = new Set(["airplay"]);

/** What the picker says after the device name. The figure is AirPlay 2's
 * published buffer, not measured on this machine. */
export const WIRELESS_DELAY_NOTE = "Wireless, about 2 s delay";

type OutputRow = Pick<HeadphoneOutputDevice, "id" | "label" | "transport">;

export function outputIsWirelessDelayed(
  output: Pick<HeadphoneOutputDevice, "transport">,
): boolean {
  return (
    typeof output.transport === "string" &&
    _WIRELESS_DELAYED_TRANSPORTS.has(output.transport)
  );
}

export function deviceIsWirelessDelayed(
  outputs: readonly OutputRow[],
  deviceId: string | null,
): boolean {
  if (deviceId === null) return false;
  const row = outputs.find((output) => output.id === deviceId);
  return row !== undefined && outputIsWirelessDelayed(row);
}

/** The option text both I/O pickers render for one output. */
export function outputOptionLabel(output: OutputRow): string {
  const name = output.label || output.id;
  return outputIsWirelessDelayed(output)
    ? `${name} (${WIRELESS_DELAY_NOTE})`
    : name;
}

/** HEADPHONE CUE on a speaker that plays 2 s late cannot cue anything. */
export function assertCueNotWirelessDelayed(
  deviceId: string,
  outputs: readonly OutputRow[],
): void {
  const row = outputs.find((output) => output.id === deviceId);
  if (row !== undefined && outputIsWirelessDelayed(row)) {
    throw new Error(
      `HEADPHONE CUE cannot be ${row.label || row.id}: AirPlay plays about 2 s late, so you would hear the cue after the room. Pick it as MAIN instead and cue on wired or built-in headphones.`,
    );
  }
}

/** The note under the pickers while MAIN is a wireless speaker, else null. */
export function wirelessMainNote(
  outputs: readonly OutputRow[],
  masterId: string | null,
): string | null {
  if (!deviceIsWirelessDelayed(outputs, masterId)) return null;
  return "MAIN is a wireless speaker: the room hears you about 2 s late, longer than ROOM delay or CALIBRATE can line up. Monitor the master in your headphones, not the room.";
}
