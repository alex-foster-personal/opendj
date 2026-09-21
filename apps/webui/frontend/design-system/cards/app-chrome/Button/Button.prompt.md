App chrome button: `<button>` default, `.primary` solid accent, `disabled`, and the inert PARITY-TODO variant.

```html
<button type="button">Cancel</button>
<button type="button" class="primary">Save changes</button>
<button type="button" disabled>Export</button>
<button type="button" disabled class="rb-inert" title="not implemented - see PARITY-TODO" aria-label="not implemented - see PARITY-TODO">Sync to cloud</button>
```

Rules: one `.primary` per action group; destructive actions confirm in a dialog rather than using a red button;
unbuilt actions render inert with the exact PARITY-TODO string in both `title` and `aria-label`.
