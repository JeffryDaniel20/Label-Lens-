import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAnalysisSse } from "@/features/analysis/useAnalysisSse";

/** A minimal stand-in for the browser's real `EventSource` - jsdom doesn't
 * implement it. Only what this hook actually uses: `addEventListener` for
 * the named `analysis.transition` event, `onopen`/`onerror` assignment, and
 * `close()`. Tests dispatch events directly via each instance rather than
 * a real network/SSE parser, since the wire-format parsing itself is the
 * backend's own responsibility (`app.analysis.sse`), already tested there. */
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    (this.listeners[type] ??= []).push(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent<string>);
    }
  }
}

function Harness({ analysisId }: { analysisId: string }) {
  const { connected } = useAnalysisSse(analysisId);
  return <div>{connected ? "connected" : "disconnected"}</div>;
}

function renderHarness(analysisId: string) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <Harness analysisId={analysisId} />
    </QueryClientProvider>,
  );
}

describe("useAnalysisSse", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens a real EventSource against the analysis's own events endpoint", () => {
    renderHarness("a1");

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0]?.url).toBe("/v1/analyses/a1/events");
  });

  it("reports connected once a transition event arrives", async () => {
    const { getByText } = renderHarness("a1");
    const source = MockEventSource.instances[0]!;

    source.emit("analysis.transition", {
      sequence: 1,
      from_state: null,
      to_state: "queued",
      occurred_at: "2026-01-01T00:00:00Z",
      reason: null,
      percentage: 0,
    });

    await waitFor(() => expect(getByText("connected")).toBeInTheDocument());
  });

  it("closes the connection itself once a transition reaches a stopping state", () => {
    renderHarness("a1");
    const source = MockEventSource.instances[0]!;

    source.emit("analysis.transition", {
      sequence: 5,
      from_state: "scoring",
      to_state: "completed",
      occurred_at: "2026-01-01T00:05:00Z",
      reason: null,
      percentage: 100,
    });

    expect(source.closed).toBe(true);
  });

  it("does not close the connection for a non-stopping transition", () => {
    renderHarness("a1");
    const source = MockEventSource.instances[0]!;

    source.emit("analysis.transition", {
      sequence: 2,
      from_state: "queued",
      to_state: "validating",
      occurred_at: "2026-01-01T00:00:01Z",
      reason: null,
      percentage: 11,
    });

    expect(source.closed).toBe(false);
  });

  it("closes the connection on unmount", () => {
    const { unmount } = renderHarness("a1");
    const source = MockEventSource.instances[0]!;

    unmount();

    expect(source.closed).toBe(true);
  });
});
