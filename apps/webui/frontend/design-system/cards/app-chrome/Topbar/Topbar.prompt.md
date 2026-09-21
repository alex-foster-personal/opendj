Topbar: `.topbar` muted status strip with per-count hover titles; `.banner-warning` full-width red page alert.

```html
<div class="topbar">
  <span>rekordbox</span>
  <span title="Tracks with resolvable audio: 1186 of 8355 rows">1186 present</span>
  <span style="margin-left:auto">engine 8585</span>
</div>
<div class="banner-warning">Read-only: rekordbox is running.</div>
```

Counts always name their denominator in the title (honest denominators rule). The banner is for page-wide state, not
per-item errors (use a toast).
