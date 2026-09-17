Waveform row: `.rb-waverow` at `--rb-waverow-h` (43 px) per deck; band colors orange/mid/high, red cues and tick; decks 3 and 4 use `--rb-waverow-secondary`; `.finished-eject` chip when a track ends. Inside `.perf-root`.

```html
<div class="rb-waverow" style="position: relative; height: var(--rb-waverow-h)" title="Deck 1 waveform, 3:42 of 5:10">
  <!-- waveform canvas: lows --rb-orange, mids --rb-wave-mid, highs --rb-wave-high; tick --rb-red -->
  <button class="finished-eject" title="Unload deck 1">EJECT</button>
</div>
```

The row's `title` names the deck and position. Never draw waveforms in the accent blue; blue is reserved for controls.
