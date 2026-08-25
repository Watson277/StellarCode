import type { RuntimeAttachment } from "../protocol/runtimeEvents";

/** Composer content owned by one conversation. */
export interface SessionDraft {
  prompt: string;
  attachments: RuntimeAttachment[];
}

export interface SessionDraftInput {
  prompt: string;
  attachments: readonly RuntimeAttachment[];
}

/**
 * In-memory conversation draft storage.
 *
 * Keeping this store memory-only is intentional: pasted images may carry a large
 * `data_base64` payload, which must not be copied into localStorage. The store
 * clones both arrays and attachment records on every boundary so React state (or
 * another caller) cannot mutate a saved draft accidentally.
 */
export class SessionDraftStore {
  private readonly drafts = new Map<string, SessionDraft>();

  get(sessionId: string): SessionDraft | undefined {
    const draft = this.drafts.get(sessionId);
    return draft ? cloneDraft(draft) : undefined;
  }

  save(sessionId: string, draft: SessionDraftInput): SessionDraft {
    assertSessionId(sessionId);
    const stored = cloneDraft(draft);
    this.drafts.set(sessionId, stored);
    return cloneDraft(stored);
  }

  /** Reset a known session while retaining an explicit empty draft entry. */
  clear(sessionId: string): SessionDraft {
    assertSessionId(sessionId);
    const emptyDraft: SessionDraft = { prompt: "", attachments: [] };
    this.drafts.set(sessionId, emptyDraft);
    return cloneDraft(emptyDraft);
  }

  /** Remove all draft state for a deleted conversation. */
  delete(sessionId: string): boolean {
    return this.drafts.delete(sessionId);
  }
}

/** Shared application instance; it lives only for the current renderer process. */
export const sessionDraftStore = new SessionDraftStore();

function cloneDraft(draft: SessionDraftInput): SessionDraft {
  return {
    prompt: draft.prompt,
    attachments: draft.attachments.map(cloneAttachment),
  };
}

function cloneAttachment(attachment: RuntimeAttachment): RuntimeAttachment {
  return { ...attachment };
}

function assertSessionId(sessionId: string): void {
  if (!sessionId.trim()) {
    throw new TypeError("SessionDraftStore requires a non-empty session id.");
  }
}
