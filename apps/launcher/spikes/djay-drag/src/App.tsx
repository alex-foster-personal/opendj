import { invoke } from "@tauri-apps/api/core";
import { useState } from "react";

export default function App() {
  const [path, setPath] = useState("/Users/dev/Music/test-track.mp3");
  const [msg, setMsg] = useState<string>("");

  const onDragDown = async (e: React.MouseEvent) => {
    e.preventDefault();
    try {
      await invoke("start_track_drag", { path });
      setMsg("drag started - release on djay's deck");
    } catch (err) {
      setMsg(`error: ${err}`);
    }
  };

  return (
    <div style={{ padding: 16, fontFamily: "-apple-system, system-ui, sans-serif" }}>
      <h2 style={{ marginTop: 0 }}>djay drag spike</h2>
      <p style={{ fontSize: 12, opacity: 0.7 }}>
        Paste an absolute mp3/m4a/aiff path, press & hold the button, drag onto
        djay Pro's deck (windowed mode).
      </p>
      <input
        value={path}
        onChange={(e) => setPath(e.target.value)}
        style={{ width: "100%", padding: 6 }}
      />
      <button
        onMouseDown={onDragDown}
        style={{ marginTop: 12, padding: "8px 14px", width: "100%" }}
      >
        Start Drag
      </button>
      {msg && <p style={{ marginTop: 12, fontSize: 12 }}>{msg}</p>}
    </div>
  );
}
