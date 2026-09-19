import type { RuntimeEvent } from "../protocol/runtimeEvents";

type Delta = Extract<RuntimeEvent, { type: "assistant.delta" }> & { runtime_pid?: number };

/** Only transient text is batched. Durable events and approvals bypass this buffer. */
export class AssistantDeltaBuffer {
  private pending: Delta[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private characters = 0;

  constructor(private readonly deliver: (events: Delta[]) => void, private readonly delayMs = 32) {}

  push(event: Delta) {
    const last = this.pending[this.pending.length - 1];
    if (last && !last.data.reset && !event.data.reset
      && last.session_id === event.session_id && last.task_id === event.task_id
      && last.runtime_pid === event.runtime_pid) {
      this.pending[this.pending.length - 1] = { ...last, data: { ...last.data, text: last.data.text + event.data.text } };
    } else {
      this.pending.push(event);
    }
    this.characters += event.data.text.length;
    if (this.characters >= 32_768 || this.pending.length >= 128) this.flush();
    else if (this.timer === undefined) this.timer = setTimeout(() => this.flush(), this.delayMs);
  }

  flush() {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    const batch = this.pending;
    this.pending = [];
    this.characters = 0;
    if (batch.length) this.deliver(batch);
  }

  dispose() {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.pending = [];
    this.characters = 0;
  }
}
