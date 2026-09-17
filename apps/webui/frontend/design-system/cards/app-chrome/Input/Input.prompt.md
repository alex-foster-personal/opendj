App chrome form controls: bare `<input>`, `<select>`, `<textarea>` styled globally (surface fill, border, 6 px radius).

```html
<label>Playlist name<input type="text" value="Peak hour, Sat" /></label>
<label>Source<select><option>rekordbox</option></select></label>
<label>Notes<textarea></textarea></label>
```

Put the label text above the control in `--muted` 12 px. Validation messages use `--danger` text below the control;
never change the control's fill color for state.
