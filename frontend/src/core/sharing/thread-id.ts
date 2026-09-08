/**
 * Shared-thread identities.
 *
 * A thread another citizen shared is rendered by the viewer's own frontend
 * from an Agora snapshot, never through the owner's gateway. Inside the
 * rendering tree it travels under a synthetic thread id, `shared:<stack>:<tid>`,
 * so that every artifact URL the message components build can be redirected
 * to the Agora's bundled files with one switch (see `core/artifacts/utils.ts`).
 *
 * The shape checks mirror `agora/src/threadshare.py` (`_STACK_RE`, `_THREAD_RE`).
 */
export const SHARED_THREAD_ID_PREFIX = "shared:";

const STACK_RE = /^[a-z][a-z0-9-]{1,30}$/;
const THREAD_ID_RE = /^[A-Za-z0-9_-]{1,80}$/;

export function isValidStack(stack: string): boolean {
  return STACK_RE.test(stack);
}

export function isValidThreadId(threadId: string): boolean {
  return THREAD_ID_RE.test(threadId);
}

export interface SharedThreadRef {
  stack: string;
  threadId: string;
}

export function toSharedThreadId(stack: string, threadId: string): string {
  return `${SHARED_THREAD_ID_PREFIX}${stack}:${threadId}`;
}

export function parseSharedThreadId(id: string): SharedThreadRef | null {
  if (!id.startsWith(SHARED_THREAD_ID_PREFIX)) {
    return null;
  }
  const rest = id.slice(SHARED_THREAD_ID_PREFIX.length);
  const sep = rest.indexOf(":");
  if (sep <= 0) {
    return null;
  }
  const stack = rest.slice(0, sep);
  const threadId = rest.slice(sep + 1);
  if (!isValidStack(stack) || !isValidThreadId(threadId)) {
    return null;
  }
  return { stack, threadId };
}

export function isSharedThreadId(id: string): boolean {
  return parseSharedThreadId(id) !== null;
}
