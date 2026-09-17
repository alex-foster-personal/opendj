Panel: `.rb-panel` surface with 1 px border; raised chrome uses `--rb-panel-raised`; a positioned `.rb-deck` containing `.jog-off-tempo` shows the pulsing red warning tint. Inside `.perf-root`.

```html
<div class="perf-root">
  <div class="rb-panel rb-deck" style="position: relative">
    <h3>DECK 2</h3>
    <p title="Tempo 122.40 BPM, drifting +0.4 BPM from master">122.40 BPM</p>
    <span class="jog-off-tempo">off tempo</span>
  </div>
</div>
```

Surfaces step `--rb-bg` < `--rb-panel` < `--rb-panel-raised`; no shadows. Deck titles are 13 px, body 10 to 11 px.
