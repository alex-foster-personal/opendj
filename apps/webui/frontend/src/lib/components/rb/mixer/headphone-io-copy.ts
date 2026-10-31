/**
 * Hover-explainer copy for the I/O half of HeadphoneCluster.svelte: the
 * VOL knob, I/O button, head delay, calibrate, room delay, rescan, output mode and
 * audio input pick controls. The MASTER and HEADPHONE CUE pick copy stays in
 * the component, where cueout-09 pins it.
 */
export const calibrateBullets = [
	'Measures how far the headphones lag the room with the built-in mic and splits the difference between HEAD DELAY and ROOM per the alignment mode.',
	'Live only in two outputs with a HEADPHONE CUE sink selected. Playing decks pause for the chirps and resume after.'
];

export const roomBullets = [
	'ROOM is the room (MASTER) delay, 0-1500 ms, the last node before the speakers. The phones never pay it.',
	'The waveform and PLAY light lag by the same amount on purpose, so what you see is what the room hears.'
];

export const rescanBullets = [
	'Re-enumerate outputs and inputs without flipping a Bluetooth headset to HFP.'
];

export const modeBullets = [
	'One output (practice): everything plays on MAIN; turn channel CUE on and use MIX to blend cue over the full master.',
	'Two outputs: MASTER / MAIN for the room and HEADPHONE CUE for the phones; cue never reaches the room.',
	'Split L/R: one stereo output; left = master, right = cue. Needs a DJ splitter cable (not a Y cable), and runs by itself when MAIN and CUE are the same device.'
];

export const inputPickBullets = [
	'Used to unlock output names and by CALIBRATE to time the chirps. Never pick a headphone/HFP mic.',
	'Test plays a few beeps on the master output. The thin meter is this input.'
];

export const ioBullets = ['Click for Speaker / Headphone CUE quick settings without interrupting audio.'];

export const delayBullets = [
	'Mixxx Head Delay, 0-500 ms, on the cue path only. It does not delay the room.',
	'CALIBRATE fills it from the measured offset when the phones are ahead of the room; type a value to override.',
	'Two independently clocked devices still drift. Bluetooth is for auditioning, not beatmatching.'
];

export const levelBullets = [
	'Headphone GAIN (Mixxx Head Gain). Scales the CUE path: the phones in two outputs, the cue ear in SPLIT, and the cue blend in MAIN.',
	'It does not change the room MASTER volume. Default is 1 (full). Turn down if the phones are hot.'
];
