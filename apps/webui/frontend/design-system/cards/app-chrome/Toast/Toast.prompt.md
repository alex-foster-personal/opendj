Toast: `.toast` card in a fixed bottom-right `.toast-stack`; `.warn` amber border, `.error` red border.

```html
<div class="toast-stack">
  <div class="toast">Playlist saved</div>
  <div class="toast warn">3 tracks skipped: no audio on volume</div>
  <div class="toast error">Engine unreachable on port 8585</div>
</div>
```

Three rungs only. Severity is border color, never a fill. Copy states the fact and, for warnings, the denominator.
