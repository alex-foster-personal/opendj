export interface PinReply {
  id: string;
  author: "operator" | "agent";
  text: string;
  created_at: string;
  agent_kind?: string | null;
}

export interface ThreadTurn {
  id: string;
  kind: "opening" | "reply";
  author: "operator" | "agent";
  text: string;
  created_at: string;
  agent_kind?: string | null;
}

export function pinThread(pin: {
  id: string;
  text: string;
  created_at: string;
  author?: string | null;
  agent_note?: string | null;
  updated_at?: string | null;
  replies?: PinReply[] | null;
}): ThreadTurn[] {
  const turns: ThreadTurn[] = [
    {
      id: pin.id,
      kind: "opening",
      author: pin.author === "agent" ? "agent" : "operator",
      text: pin.text,
      created_at: pin.created_at,
      agent_kind: null,
    },
  ];

  const replies = pin.replies;
  if (Array.isArray(replies) && replies.length > 0) {
    for (const reply of replies) {
      turns.push({
        id: reply.id,
        kind: "reply",
        author: reply.author,
        text: reply.text,
        created_at: reply.created_at,
        agent_kind: reply.agent_kind ?? null,
      });
    }
    return turns;
  }

  const agentNote = pin.agent_note;
  if (typeof agentNote === "string" && agentNote.trim() !== "") {
    turns.push({
      id: `${pin.id}:agent_note`,
      kind: "reply",
      author: "agent",
      text: agentNote,
      created_at: pin.updated_at ?? pin.created_at,
      agent_kind: null,
    });
  }

  return turns;
}
