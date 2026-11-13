import { useEffect, useRef, useState } from "react";
import { LiveModel } from "./live";

/** Follow one run's event stream (SSE) into a LiveModel. The stream replays the whole log first, so a finished run
 *  and a running one are handled the same way. The page re-renders at most every 400 ms. */
export function useLiveRun(runId: string | null): { model: LiveModel | null; version: number; ended: boolean; interrupted: boolean; error: boolean } {
  const model = useRef<LiveModel | null>(null);
  const modelRun = useRef<string | null>(null);
  const dirty = useRef(false);
  const [version, setVersion] = useState(0);
  const [ended, setEnded] = useState(false);
  const [interrupted, setInterrupted] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    model.current = runId ? new LiveModel(5) : null;
    modelRun.current = runId;
    dirty.current = true;
    setEnded(false);
    setInterrupted(false);
    setError(false);
    if (!runId) {
      setVersion((v) => v + 1);
      return;
    }
    const es = new EventSource(`/api/runs/${runId}/stream?poll_s=0.3`);
    es.onmessage = (m) => {
      try {
        model.current?.ingest(JSON.parse(m.data));
        dirty.current = true;
      } catch {
        /* a line that is not JSON: ignore it */
      }
    };
    es.addEventListener("end", (m) => {
      let intr = false;
      try {
        intr = Boolean(JSON.parse((m as MessageEvent).data).interrupted);
      } catch {
        /* no payload */
      }
      if (model.current) {
        model.current.ended = true;
        model.current.interrupted = intr;
      }
      setInterrupted(intr);
      setEnded(true);
      dirty.current = true;
      es.close();
    });
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) setError(true);
    };
    const tick = window.setInterval(() => {
      if (dirty.current) {
        dirty.current = false;
        setVersion((v) => v + 1);
      }
    }, 400);
    return () => {
      es.close();
      window.clearInterval(tick);
    };
  }, [runId]);

  // never hand back the previous run's model for a render or two after the run changed
  return { model: modelRun.current === runId ? model.current : null, version, ended, interrupted, error };
}
