import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchFileShareStatus,
  fetchSharedThread,
  fetchThreadShareStatus,
  internalizeFile,
  internalizeThread,
  previewThreadShare,
  refreshThreadShare,
  revokeFileShare,
  revokeThreadShare,
} from "./api";

export const threadShareKey = (threadId: string) =>
  ["thread-share", threadId] as const;
export const threadSharePreviewKey = (
  threadId: string,
  includeToolBodies: boolean,
) => ["thread-share-preview", threadId, includeToolBodies] as const;
export const sharedThreadKey = (stack: string, threadId: string) =>
  ["shared-thread", stack, threadId] as const;
export const fileShareKey = (threadId: string, path: string) =>
  ["file-share", threadId, path] as const;

export function useThreadShareStatus(
  threadId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: threadShareKey(threadId),
    queryFn: () => fetchThreadShareStatus(threadId),
    enabled: enabled && threadId.length > 0,
    staleTime: 60_000,
    retry: false,
  });
}

export function useThreadSharePreview(
  threadId: string,
  includeToolBodies: boolean,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: threadSharePreviewKey(threadId, includeToolBodies),
    queryFn: () => previewThreadShare(threadId, { includeToolBodies }),
    enabled: enabled && threadId.length > 0,
    staleTime: 30_000,
    retry: false,
  });
}

export function useInternalizeThread(threadId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (includeToolBodies: boolean) =>
      internalizeThread(threadId, { includeToolBodies }),
    onSuccess: (status) => {
      queryClient.setQueryData(threadShareKey(threadId), status);
    },
  });
}

export function useRefreshThreadShare(threadId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => refreshThreadShare(threadId),
    onSuccess: (status) => {
      queryClient.setQueryData(threadShareKey(threadId), status);
    },
  });
}

export function useRevokeThreadShare(threadId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => revokeThreadShare(threadId),
    onSuccess: (status) => {
      queryClient.setQueryData(threadShareKey(threadId), status);
    },
  });
}

export function useSharedThread(stack: string, threadId: string) {
  return useQuery({
    queryKey: sharedThreadKey(stack, threadId),
    queryFn: () => fetchSharedThread(stack, threadId),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useFileShareStatus(
  threadId: string,
  path: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: fileShareKey(threadId, path),
    queryFn: () => fetchFileShareStatus(threadId, path),
    enabled: enabled && threadId.length > 0 && path.length > 0,
    staleTime: 60_000,
    retry: false,
  });
}

export function useInternalizeFile(threadId: string, path: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (title?: string) => internalizeFile(threadId, path, title),
    onSuccess: (status) => {
      queryClient.setQueryData(fileShareKey(threadId, path), status);
    },
  });
}

export function useRevokeFileShare(threadId: string, path: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => revokeFileShare(threadId, path),
    onSuccess: (status) => {
      queryClient.setQueryData(fileShareKey(threadId, path), status);
    },
  });
}
