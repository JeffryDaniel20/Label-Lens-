import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  analysisEventsQueryKey,
  analysisQueryKey,
  isStoppingState,
} from "@/features/analysis/analysis";
import type { AnalysisSseEvent } from "@/api/types";

/** Live progress via the real P5-T5 SSE stream (`GET /v1/analyses/{id}/events`
 * with `Accept: text/event-stream`, the same path as the JSON history - see
 * `app.analysis.router.list_analysis_events`). `EventSource` sets that
 * `Accept` header itself and, on a dropped connection, retries automatically
 * using the last event's id as `Last-Event-ID` - which the backend honors by
 * replaying every transition since that sequence (`app.analysis.sse`), so a
 * reconnect after a network blip never misses a transition.
 *
 * One thing `EventSource` does NOT know on its own: the backend closes the
 * stream itself once an analysis stops (`STOPPING_STATES` - the three real
 * terminals plus the two human-review states), but a closed connection with
 * no error looks identical to a dropped one from `EventSource`'s point of
 * view, so it would otherwise keep trying to reopen a stream for an analysis
 * that will never send anything again. This hook closes the connection
 * itself the moment a transition's own `to_state` is a stopping state,
 * before `EventSource` gets a chance to retry. */
export function useAnalysisSse(analysisId: string | undefined): { connected: boolean } {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!analysisId) return;

    const source = new EventSource(`/v1/analyses/${analysisId}/events`, {
      withCredentials: true,
    });

    const handleTransition = (event: MessageEvent<string>) => {
      setConnected(true);
      const payload = JSON.parse(event.data) as AnalysisSseEvent;
      void queryClient.invalidateQueries({ queryKey: analysisQueryKey(analysisId) });
      void queryClient.invalidateQueries({ queryKey: analysisEventsQueryKey(analysisId) });
      if (isStoppingState(payload.to_state)) {
        source.close();
        setConnected(false);
      }
    };

    source.addEventListener("analysis.transition", handleTransition);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    return () => {
      source.close();
      setConnected(false);
    };
  }, [analysisId, queryClient]);

  return { connected };
}
