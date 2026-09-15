import { describe, expect, it } from "vitest";

import { parseEventBlock, readEventStream } from "./sse";

describe("parseEventBlock", () => {
  it("parses typed JSON events", () => {
    expect(
      parseEventBlock('id: 4-0\nevent: message.delta\ndata: {"delta":"你好"}'),
    ).toEqual({ id: "4-0", type: "message.delta", data: { delta: "你好" } });
  });

  it("ignores heartbeat comments", () => {
    expect(parseEventBlock(": heartbeat")).toBeNull();
  });
});

describe("readEventStream", () => {
  it("handles event boundaries split across chunks", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode("id: 1-0\nevent: message.delta\ndata: {\"delta\":\"你"));
        controller.enqueue(
          encoder.encode("好\"}\n\nid: 2-0\nevent: run.completed\ndata: {\"trace_id\":\"t\"}\n\n"),
        );
        controller.close();
      },
    });
    const events: string[] = [];

    await readEventStream(stream, (event) => {
      events.push(event.type);
    });

    expect(events).toEqual(["message.delta", "run.completed"]);
  });
});
