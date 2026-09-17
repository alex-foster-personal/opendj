Lit button: `.rb-lit-button` (dim raised chrome), `.lit` (blue + glow), `.master-btn.lit` (gold), `.rb-inert` + PARITY-TODO for unbuilt controls. Inside `.perf-root`.

```html
<div class="perf-root">
  <button class="rb-lit-button">LOOP IN</button>
  <button class="rb-lit-button lit">SYNC</button>
  <button class="rb-lit-button master-btn lit">MASTER</button>
  <button class="rb-lit-button rb-inert" disabled title="not implemented - see PARITY-TODO" aria-label="not implemented - see PARITY-TODO">QUANTIZE</button>
</div>
```

Labels are short uppercase words (10 px). State is the fill, never an icon swap. Exactly one master per mixer.
