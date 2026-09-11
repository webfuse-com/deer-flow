const SUPPORTED_RUN_STREAM_MODES: ReadonlySet<unknown> = new Set([
  "values",
  "messages-tuple",
  "updates",
  "debug",
  "tasks",
  "checkpoints",
  "custom",
] as const);

export const CHAT_RUN_STREAM_MODES = [
  "messages-tuple",
  "updates",
  "custom",
] as const;

const warnedUnsupportedStreamModes = new Set<string>();

export function warnUnsupportedStreamModes(
  modes: string[],
  warn: (message: string) => void = console.warn,
) {
  const unseenModes = modes.filter((mode) => {
    if (warnedUnsupportedStreamModes.has(mode)) {
      return false;
    }
    warnedUnsupportedStreamModes.add(mode);
    return true;
  });

  if (unseenModes.length === 0) {
    return;
  }

  warn(
    `[deer-flow] Dropped unsupported LangGraph stream mode(s): ${unseenModes.join(", ")}`,
  );
}

export function sanitizeRunStreamOptions<T>(options: T): T {
  if (typeof options !== "object" || options === null) {
    return options;
  }

  let sanitizedOptions: T = options;
  if ("streamResumable" in options) {
    const withoutStreamResumable = { ...options };
    delete withoutStreamResumable.streamResumable;
    sanitizedOptions = withoutStreamResumable as T;
  }

  if (!("streamMode" in options) || options.streamMode == null) {
    return sanitizedOptions;
  }

  const requestedModes = Array.isArray(options.streamMode)
    ? options.streamMode
    : [options.streamMode];
  const unsupportedModes = requestedModes.filter(
    (mode) => !SUPPORTED_RUN_STREAM_MODES.has(mode),
  );
  if (unsupportedModes.length === 0) {
    return sanitizedOptions;
  }

  const validModes = requestedModes.filter((mode) =>
    SUPPORTED_RUN_STREAM_MODES.has(mode),
  );
  if (validModes.length === 0) {
    throw new Error(
      `[deer-flow] Unsupported LangGraph stream mode(s): ${unsupportedModes.join(", ")}`,
    );
  }

  warnUnsupportedStreamModes(unsupportedModes.map(String));
  return { ...sanitizedOptions, streamMode: validModes };
}

/**
 * Keep chat streams on incremental events only. Without an explicit mode list,
 * the SDK's lazy message tracking also requests `values`, retransmitting the
 * full thread state (including message history) after graph steps.
 */
export function forceChatRunStreamOptions<T>(options: T): T {
  const sanitizedOptions = sanitizeRunStreamOptions(options);
  const preservedOptions =
    typeof AbortSignal !== "undefined" &&
    sanitizedOptions instanceof AbortSignal
      ? { signal: sanitizedOptions }
      : typeof sanitizedOptions === "object" && sanitizedOptions !== null
        ? sanitizedOptions
        : {};
  const requestedMode = Reflect.get(preservedOptions, "streamMode");
  const streamModes = new Set<string>([
    ...CHAT_RUN_STREAM_MODES,
    ...((Array.isArray(requestedMode)
      ? requestedMode
      : requestedMode == null
        ? []
        : [requestedMode]) as string[]),
  ]);
  streamModes.delete("values");

  return {
    ...preservedOptions,
    streamMode: [...streamModes],
  } as T;
}
