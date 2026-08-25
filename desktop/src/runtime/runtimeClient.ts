import {
  RUNTIME_PROTOCOL_VERSION,
  type RuntimeMessage,
  type RuntimeRequest,
  type RuntimeRequestDataMap,
  type RuntimeRequestType,
  type RuntimeResponse,
  type RuntimeResponseDataMap,
} from "../protocol/runtimeEvents";

export type RuntimeRequestSender = (request: RuntimeRequest) => Promise<void>;

export interface RuntimeClientOptions {
  defaultTimeoutMs?: number;
  createRequestId?: (method: RuntimeRequestType) => string;
}

export interface RuntimeCallOptions {
  requestId?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** A newer request in the same scope supersedes the previous request by default. */
  scope?: string;
  supersede?: boolean;
}

export class RuntimeClientError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "RuntimeClientError";
  }
}

export class RuntimeRequestError extends RuntimeClientError {
  constructor(
    readonly response: RuntimeResponse,
  ) {
    const code = response.error?.code ?? "runtime_error";
    const message = response.error?.message ?? "Runtime request failed";
    super(`${code}: ${message}`, code, response.request_id);
    this.name = "RuntimeRequestError";
  }
}

export class RuntimeRequestTimeoutError extends RuntimeClientError {
  constructor(requestId: string, timeoutMs: number) {
    super(
      `Runtime request ${requestId} timed out after ${timeoutMs} ms`,
      "request_timeout",
      requestId,
    );
    this.name = "RuntimeRequestTimeoutError";
  }
}

export class RuntimeRequestSupersededError extends RuntimeClientError {
  constructor(requestId: string, scope: string) {
    super(
      `Runtime request ${requestId} was superseded in scope ${scope}`,
      "request_superseded",
      requestId,
    );
    this.name = "RuntimeRequestSupersededError";
  }
}

export class RuntimeClientDisposedError extends RuntimeClientError {
  constructor(message = "Runtime client was disposed", requestId?: string) {
    super(message, "client_disposed", requestId);
    this.name = "RuntimeClientDisposedError";
  }
}

type PendingCall = {
  requestId: string;
  scope?: string;
  timer?: ReturnType<typeof setTimeout>;
  signal?: AbortSignal;
  abortListener?: () => void;
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
};

const DEFAULT_TIMEOUT_MS = 30_000;
let requestSequence = 0;

/**
 * Owns request/response correlation for the Sidecar protocol.
 *
 * Transport setup stays outside this class: Tauri can forward `runtime-message` payloads
 * to accept(), while tests and future transports can provide a small sender function.
 */
export class RuntimeClient {
  private readonly pending = new Map<string, PendingCall>();
  private readonly requestsByScope = new Map<string, Set<string>>();
  private readonly ownedRequestIds = new Set<string>();
  private readonly ownedRequestOrder: string[] = [];
  private readonly defaultTimeoutMs: number;
  private readonly createRequestId: (method: RuntimeRequestType) => string;
  private disposed = false;

  constructor(
    private readonly send: RuntimeRequestSender,
    options: RuntimeClientOptions = {},
  ) {
    this.defaultTimeoutMs = options.defaultTimeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.createRequestId = options.createRequestId ?? defaultRequestId;
  }

  get pendingCount() {
    return this.pending.size;
  }

  request<M extends RuntimeRequestType>(
    method: M,
    params: RuntimeRequestDataMap[M],
    options: RuntimeCallOptions = {},
  ): Promise<RuntimeResponseDataMap[M]> {
    if (this.disposed) {
      return Promise.reject(new RuntimeClientDisposedError());
    }
    const requestId = options.requestId ?? this.createRequestId(method);
    if (this.ownedRequestIds.has(requestId)) {
      return Promise.reject(new RuntimeClientError(
        `Runtime request id has already been used: ${requestId}`,
        "duplicate_request_id",
        requestId,
      ));
    }
    const scope = options.scope?.trim() || undefined;
    if (scope && options.supersede !== false) {
      const previousRequestIds = [...(this.requestsByScope.get(scope) ?? [])];
      for (const previousRequestId of previousRequestIds) {
        this.rejectPending(
          previousRequestId,
          new RuntimeRequestSupersededError(previousRequestId, scope),
        );
      }
    }
    if (options.signal?.aborted) {
      return Promise.reject(abortedRequestError(requestId));
    }

    const message = {
      kind: "request",
      protocol_version: RUNTIME_PROTOCOL_VERSION,
      request_id: requestId,
      method,
      params,
    } as RuntimeRequest;
    this.rememberRequestId(requestId);
    const timeoutMs = options.timeoutMs ?? this.defaultTimeoutMs;
    const response = new Promise<RuntimeResponseDataMap[M]>((resolve, reject) => {
      const pending: PendingCall = {
        requestId,
        scope,
        signal: options.signal,
        resolve: (result) => resolve(result as RuntimeResponseDataMap[M]),
        reject,
      };
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        pending.timer = setTimeout(() => {
          this.rejectPending(requestId, new RuntimeRequestTimeoutError(requestId, timeoutMs));
        }, timeoutMs);
      }
      if (options.signal) {
        pending.abortListener = () => {
          this.rejectPending(requestId, abortedRequestError(requestId));
        };
        options.signal.addEventListener("abort", pending.abortListener, { once: true });
      }
      this.pending.set(requestId, pending);
      if (scope) {
        const scopeRequests = this.requestsByScope.get(scope) ?? new Set<string>();
        scopeRequests.add(requestId);
        this.requestsByScope.set(scope, scopeRequests);
      }
    });

    try {
      void Promise.resolve(this.send(message)).catch((error: unknown) => {
        this.rejectPending(requestId, normalizeError(error));
      });
    } catch (error) {
      this.rejectPending(requestId, normalizeError(error));
    }
    return response;
  }

  /** Returns true when the message matched and settled a pending request. */
  accept(message: RuntimeMessage): boolean {
    if (message.kind !== "response") return false;
    const pending = this.takePending(message.request_id);
    // A response can arrive after timeout, supersede, disconnect, or project
    // switching already rejected its Promise. It still belongs to this client
    // and must not leak into the legacy session-control response handler.
    if (!pending) return this.ownedRequestIds.has(message.request_id);
    if (message.ok) {
      pending.resolve(message.result ?? {});
    } else {
      pending.reject(new RuntimeRequestError(message));
    }
    return true;
  }

  rejectAll(reason: Error | string = "Runtime connection closed") {
    const error = normalizeError(reason);
    for (const requestId of [...this.pending.keys()]) {
      this.rejectPending(requestId, error);
    }
  }

  /** Rejects only requests whose logical scope starts with the supplied prefix. */
  rejectScopePrefix(prefix: string, reason: Error | string = "Runtime request scope closed") {
    if (!prefix) return 0;
    const error = normalizeError(reason);
    const requestIds = [...this.pending.values()]
      .filter((pending) => pending.scope?.startsWith(prefix))
      .map((pending) => pending.requestId);
    for (const requestId of requestIds) this.rejectPending(requestId, error);
    return requestIds.length;
  }

  dispose(reason = "Runtime client was disposed") {
    if (this.disposed) return;
    this.disposed = true;
    for (const requestId of [...this.pending.keys()]) {
      this.rejectPending(requestId, new RuntimeClientDisposedError(reason, requestId));
    }
  }

  private rejectPending(requestId: string, error: Error) {
    const pending = this.takePending(requestId);
    if (!pending) return false;
    pending.reject(error);
    return true;
  }

  private takePending(requestId: string) {
    const pending = this.pending.get(requestId);
    if (!pending) return undefined;
    this.pending.delete(requestId);
    if (pending.timer) clearTimeout(pending.timer);
    if (pending.signal && pending.abortListener) {
      pending.signal.removeEventListener("abort", pending.abortListener);
    }
    if (pending.scope) {
      const scopeRequests = this.requestsByScope.get(pending.scope);
      scopeRequests?.delete(requestId);
      if (scopeRequests?.size === 0) this.requestsByScope.delete(pending.scope);
    }
    return pending;
  }

  private rememberRequestId(requestId: string) {
    this.ownedRequestIds.add(requestId);
    this.ownedRequestOrder.push(requestId);
    if (this.ownedRequestOrder.length <= 4_096) return;
    for (const staleId of this.ownedRequestOrder.splice(0, 2_048)) {
      if (!this.pending.has(staleId)) this.ownedRequestIds.delete(staleId);
    }
  }
}

function defaultRequestId(method: RuntimeRequestType) {
  const prefix = method.replace(/\./g, "-");
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  requestSequence += 1;
  return `${prefix}-${Date.now()}-${requestSequence}`;
}

function abortedRequestError(requestId: string) {
  return new RuntimeClientError(
    `Runtime request ${requestId} was aborted`,
    "request_aborted",
    requestId,
  );
}

function normalizeError(error: unknown) {
  return error instanceof Error ? error : new Error(String(error));
}
