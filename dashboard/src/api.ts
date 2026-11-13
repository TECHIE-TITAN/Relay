import { useEffect, useRef, useState } from "react";

export async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let why = r.statusText;
    try {
      const body = await r.json();
      why = body.detail ?? why;
    } catch {
      /* keep the status text */
    }
    throw new Error(`${r.status} ${why}`);
  }
  return (await r.json()) as T;
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Fetch a path (null = nothing to fetch). Keeps the previous data while refetching, so nothing flashes. */
export function useApi<T>(path: string | null): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const last = useRef<string | null>(null);

  useEffect(() => {
    if (path === null) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    if (last.current !== path) {
      setData(null); // a different resource: do not show the old one
      last.current = path;
    }
    setLoading(true);
    getJson<T>(path)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path, tick]);

  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}
