/**
 * Server state, through TanStack Query.
 *
 * Which queries poll and which do not is the interesting decision here. A
 * finished run never changes, so re-fetching it is pure waste; a running one
 * changes constantly, and the WebSocket already says when. So nothing polls on
 * a timer: the socket invalidates the queries it affects, and the cache
 * re-fetches exactly what became stale.
 *
 * The exception is a run with no socket -- if progress streaming is
 * unavailable, the detail query falls back to polling, because a user watching
 * a spinner that will never move is worse than a request every few seconds.
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { UseQueryOptions } from "@tanstack/react-query";

import { api } from "./client";
import type { Depth, ResearchDetail } from "./types";

const FINISHED = new Set(["completed", "failed", "cancelled", "partial"]);

export const keys = {
  health: ["health"] as const,
  list: (limit: number) => ["research", { limit }] as const,
  detail: (id: string) => ["research", id] as const,
  report: (id: string) => ["research", id, "report"] as const,
  claims: (id: string) => ["research", id, "claims"] as const,
  evidence: (id: string) => ["research", id, "evidence"] as const,
  sources: (id: string) => ["research", id, "sources"] as const,
  trace: (id: string) => ["research", id, "trace"] as const,
  cost: (id: string) => ["research", id, "cost"] as const,
};

export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: api.health,
    refetchInterval: 30_000,
    retry: false,
  });
}

export function useHistory(limit = 20) {
  return useQuery({ queryKey: keys.list(limit), queryFn: () => api.list(limit) });
}

export function useResearch(id: string, { live }: { live: boolean }) {
  return useQuery({
    queryKey: keys.detail(id),
    queryFn: () => api.detail(id),
    // Polled only when the socket cannot do the job. A live stream makes a
    // timer redundant, and running both means the same state arrives twice by
    // two paths that can disagree about which is newer.
    refetchInterval: (query) => {
      if (live) return false;
      const data = query.state.data as ResearchDetail | undefined;
      return data && FINISHED.has(data.status) ? false : 3_000;
    },
  });
}

/** Results of a finished run: fetched once and never refreshed.
 *
 *  A completed run is immutable, so `staleTime: Infinity` is not a
 *  micro-optimisation -- it is what stops a user's citation numbers changing
 *  under them while they read. */
function finishedResource<T>(
  key: readonly unknown[],
  fetcher: () => Promise<T>,
  enabled: boolean,
): UseQueryOptions<T, Error, T, readonly unknown[]> {
  return { queryKey: key, queryFn: fetcher, enabled, staleTime: Infinity };
}

export function useReport(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.report(id), () => api.report(id), ready));
}

export function useClaims(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.claims(id), () => api.claims(id), ready));
}

export function useEvidence(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.evidence(id), () => api.evidence(id), ready));
}

export function useSources(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.sources(id), () => api.sources(id), ready));
}

export function useTrace(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.trace(id), () => api.trace(id), ready));
}

export function useCost(id: string, ready: boolean) {
  return useQuery(finishedResource(keys.cost(id), () => api.cost(id), ready));
}

export function useSubmit() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      question,
      depth,
      maxTasks,
    }: {
      question: string;
      depth: Depth;
      maxTasks?: number;
    }) => api.submit(question, depth, maxTasks),
    onSuccess: () => {
      // The new run belongs at the top of the history the moment it exists,
      // not the next time the page happens to be visited.
      void client.invalidateQueries({ queryKey: ["research"] });
    },
  });
}

export function useCancel(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancel(id),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.detail(id) }),
  });
}
