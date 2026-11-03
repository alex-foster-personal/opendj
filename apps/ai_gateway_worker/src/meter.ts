// Read the metering half of a teed SSE stream and return the last usage object seen.
import type { Usage } from "./store";

const usageOf = (line: string): Usage | null => {
  if (!line.startsWith("data:")) return null;
  const data = line.slice(5).trim();
  if (data === "[DONE]") return null;
  try {
    const chunk = JSON.parse(data) as { usage?: Usage };
    return chunk.usage ?? null;
  } catch {
    return null;
  }
};

export async function lastUsage(stream: ReadableStream<Uint8Array>): Promise<Usage | null> {
  let usage: Usage | null = null;
  let buffered = "";
  const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
  for (;;) {
    const { done, value } = await reader.read();
    const lines = (buffered + (value ?? "")).split(/\r?\n/);
    buffered = done ? "" : lines.pop()!;
    for (const line of lines) usage = usageOf(line) ?? usage;
    if (done) return usage;
  }
}
