Modal: `.conflict-dialog` fixed 75% black overlay, centered `.panel` (surface, border, 8 px radius, 640 px max), `.actions` right-aligned.

```html
<div class="conflict-dialog" role="dialog" aria-labelledby="t">
  <div class="panel">
    <h3 id="t">Cue points changed in rekordbox</h3>
    <p>...</p>
    <div class="actions"><button>Keep Open DJ</button><button class="primary">Use rekordbox</button></div>
  </div>
</div>
```

One primary action, on the right. Destructive choices are confirmed here, never via a red button elsewhere.
