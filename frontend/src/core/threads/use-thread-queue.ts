import { useCallback, useEffect, useState } from "react";

import { type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { type InputBoxSubmitOptions } from "@/components/workspace/input-box";
import { useAuth } from "@/core/auth/AuthProvider";

export interface QueuedMessage {
  id: string;
  message: PromptInputMessage;
  options?: InputBoxSubmitOptions;
  createdAt: number;
}

const QUEUE_PREFIX = "deerflow:thread-queue:v1";

function buildQueueKey(userId: string, threadId: string): string {
  return [
    QUEUE_PREFIX,
    encodeURIComponent(userId || "anonymous"),
    encodeURIComponent(threadId),
  ].join(":");
}

function readStoredQueue(key: string): QueuedMessage[] {
  try {
    if (typeof window === "undefined") return [];
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeStoredQueue(key: string, queue: QueuedMessage[]): void {
  try {
    if (typeof window === "undefined") return;
    if (queue.length === 0) {
      window.sessionStorage.removeItem(key);
    } else {
      window.sessionStorage.setItem(key, JSON.stringify(queue));
    }
  } catch {}
}

export function useThreadQueue(threadId: string | undefined) {
  const auth = useAuth();
  const userId = auth.user?.id || "anonymous";
  const storageKey = threadId ? buildQueueKey(userId, threadId) : null;

  const [queue, setQueue] = useState<QueuedMessage[]>(() => {
    return storageKey ? readStoredQueue(storageKey) : [];
  });

  useEffect(() => {
    if (!storageKey) {
      setQueue([]);
      return;
    }
    setQueue(readStoredQueue(storageKey));
  }, [storageKey]);

  const enqueue = useCallback(
    (message: PromptInputMessage, options?: InputBoxSubmitOptions) => {
      const item: QueuedMessage = {
        id: `queue_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
        message,
        options,
        createdAt: Date.now(),
      };
      setQueue((prev) => {
        const next = [...prev, item];
        if (storageKey) writeStoredQueue(storageKey, next);
        return next;
      });
      return item;
    },
    [storageKey],
  );

  const dequeue = useCallback((): QueuedMessage | null => {
    let item: QueuedMessage | null = null;
    setQueue((prev) => {
      if (prev.length === 0) return prev;
      item = prev[0] ?? null;
      const next = prev.slice(1);
      if (storageKey) writeStoredQueue(storageKey, next);
      return next;
    });
    return item;
  }, [storageKey]);

  const remove = useCallback(
    (id: string) => {
      setQueue((prev) => {
        const next = prev.filter((item) => item.id !== id);
        if (storageKey) writeStoredQueue(storageKey, next);
        return next;
      });
    },
    [storageKey],
  );

  const update = useCallback(
    (id: string, text: string) => {
      setQueue((prev) => {
        const next = prev.map((item) => {
          if (item.id === id) {
            return {
              ...item,
              message: {
                ...item.message,
                text,
              },
            };
          }
          return item;
        });
        if (storageKey) writeStoredQueue(storageKey, next);
        return next;
      });
    },
    [storageKey],
  );

  const clear = useCallback(() => {
    setQueue([]);
    if (storageKey) writeStoredQueue(storageKey, []);
  }, [storageKey]);

  return {
    queue,
    enqueue,
    dequeue,
    remove,
    update,
    clear,
  };
}
