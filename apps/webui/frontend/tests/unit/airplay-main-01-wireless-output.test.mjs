// requirement: AIRPLAY-MAIN-01 (AirPlay speakers as MAIN, never CUE, never auto-picked)
// [if] the Mac shell lists an output with transport airplay [then] both I/O pickers label it "Wireless, about 2 s delay"
// [if] the operator picks an AirPlay output as HEADPHONE CUE [then] the pick is refused before the live CUE route is touched
// [if] the room output is auto-picked [then] an AirPlay output is never chosen, while a wired output still is
// [if] MAIN is an AirPlay output [then] the panel says the room hears the DJ about 2 s late
import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

const SPEAKERS = {
  id: "native:BuiltInSpeakerDevice",
  label: "MacBook Pro Speakers",
  transport: "builtin",
};
const USB = {
  id: "native:UsbInterface",
  label: "USB Interface",
  transport: "usb",
};
const KITCHEN = {
  id: "native:AirPlayKitchen",
  label: "Kitchen",
  transport: "airplay",
};

let wireless;
let audio;

before(async () => {
  const store = new Map();
  globalThis.window = {
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
  };
  wireless = await loadTypeScriptModule("src/lib/player/wireless-output.ts");
  audio = await loadTypeScriptModule(
    "tests/unit/fixtures/headphone-select-entry.ts",
  );
});

test("AirPlay rows carry the delay note and wired rows do not", () => {
  assert.equal(
    wireless.outputOptionLabel(KITCHEN),
    "Kitchen (Wireless, about 2 s delay)",
  );
  assert.equal(wireless.outputOptionLabel(USB), "USB Interface");
  assert.equal(wireless.outputOptionLabel({ id: "x", label: "" }), "x");
  assert.equal(
    wireless.outputIsWirelessDelayed({}),
    false,
    "a browser listing has no transport",
  );
});

test("the wireless MAIN note appears only while MAIN is AirPlay", () => {
  const outputs = [SPEAKERS, KITCHEN];
  assert.match(
    wireless.wirelessMainNote(outputs, KITCHEN.id),
    /about 2 s late/,
  );
  assert.equal(wireless.wirelessMainNote(outputs, SPEAKERS.id), null);
  assert.equal(wireless.wirelessMainNote(outputs, null), null);
});

test("auto-pick never chooses AirPlay as the room but still chooses a wired output", () => {
  assert.equal(
    audio.preferredMasterOutputDeviceId([KITCHEN, USB], null),
    USB.id,
  );
  assert.equal(
    audio.preferredMasterOutputDeviceId(
      [{ ...KITCHEN, label: "Kitchen Speaker" }, USB],
      null,
    ),
    USB.id,
  );
  assert.equal(audio.preferredMasterOutputDeviceId([KITCHEN], null), null);
  // Control: a non-AirPlay output with the same name is still picked.
  assert.equal(
    audio.preferredMasterOutputDeviceId(
      [{ ...KITCHEN, transport: "usb" }],
      null,
    ),
    KITCHEN.id,
  );
});

test("an AirPlay HEADPHONE CUE pick is refused without touching the live route", async () => {
  audio.disposeHeadphoneMonitor();
  const hp = audio.mixerState.headphones;
  hp.outputs = [SPEAKERS, USB, KITCHEN];
  hp.output_mode = "two_outputs";
  hp.selected_output_device_id = USB.id;
  hp.routes.cue = { state: "selected", selected: true };
  const source = () => {
    throw new Error("the monitor source must not be read for a refused pick");
  };
  await assert.rejects(
    audio.selectHeadphoneOutput(KITCHEN.id, source),
    /HEADPHONE CUE cannot be Kitchen/,
  );
  assert.equal(hp.selected_output_device_id, USB.id);
  assert.deepEqual(hp.routes.cue, { state: "selected", selected: true });
  assert.equal(hp.output_mode, "two_outputs");
  // Control: the guard names only AirPlay; a wired CUE passes it.
  assert.doesNotThrow(() =>
    wireless.assertCueNotWirelessDelayed(USB.id, hp.outputs),
  );
});
