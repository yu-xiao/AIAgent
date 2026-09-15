import { ApiError, createApiError } from "./client";
import type { RunEvent } from "./types";

const TERMINAL_EVENTS = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.timed_out",
  "run.snapshot",
]);

export interface StreamOptions {
  path: string;
  organizationId: string;
  signal: AbortSignal;
  onEvent: (event: RunEvent) => void | Promise<void>;
}

export async function streamRunEvents(options: StreamOptions): Promise<void> {
  let lastEventId: string | undefined;
  let attempt = 0;
  while (!options.signal.aborted) {
    let terminal = false;
    try {
      const headers = new Headers({
        Accept: "text/event-stream",
        "X-Organization-Id": options.organizationId,
      });
      if (lastEventId) headers.set("Last-Event-ID", lastEventId);
      const response = await fetch(options.path, {
        headers,
        credentials: "include",
        signal: options.signal,
      });
      if (!response.ok) throw await createApiError(response);
      if (!response.body) throw new Error("浏览器未提供流式响应。 ");

      await readEventStream(response.body, async (event) => {
        if (event.id) lastEventId = event.id;
        if (TERMINAL_EVENTS.has(event.type)) terminal = true;
        await options.onEvent(event);
      });
      if (terminal) return;
    } catch (error) {
      if (options.signal.aborted) throw error;
      if (error instanceof ApiError || attempt >= 2) throw error;
    }
    attempt += 1;
    await abortableDelay(Math.min(500 * 2 ** (attempt - 1), 2_000), options.signal);
  }
}

export async function readEventStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: RunEvent) => void | Promise<void>,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseEventBlock(block);
        if (event) await onEvent(event);
        boundary = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
    const finalEvent = parseEventBlock(buffer.trim());
    if (finalEvent) await onEvent(finalEvent);
  } finally {
    reader.releaseLock();
  }
}

export function parseEventBlock(block: string): RunEvent | null {
  if (!block || block.startsWith(":")) return null;
  let id: string | undefined;
  let type = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator >= 0 ? line.slice(0, separator) : line;
    const rawValue = separator >= 0 ? line.slice(separator + 1) : "";
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "id") id = value;
    else if (field === "event") type = value;
    else if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(data.join("\n")) as Record<string, unknown>;
  } catch {
    parsed = { message: data.join("\n") };
  }
  return id ? { id, type, data: parsed } : { type, data: parsed };
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}
