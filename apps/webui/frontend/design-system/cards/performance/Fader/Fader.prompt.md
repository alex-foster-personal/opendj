Fader: `.rb-fader` vertical track + `.rb-fader-thumb` positioned by inline `top`; `.playing` pulses the blue line, `.looped` turns it orange. Inside `.perf-root`.

```html
<div class="rb-fader playing" title="Channel 2 level: -6 dB">
  <div class="rb-fader-track"></div>
  <div class="rb-fader-thumb" style="top: 45%"></div>
</div>
```

Give the container an explicit height (min 64 px). The `title` carries the level with its unit.
