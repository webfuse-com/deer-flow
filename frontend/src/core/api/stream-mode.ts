const SUPPORTED_RUN_STREAM_MODES = new Set([
  "values",
  "messages-tuple",
  "updates",
  "debug",
  "tasks",
  "checkpoints",
  "custom",
] as const);

const warnedUnsupportedStreamModes = new Set<string>();
let warnedUnsupportedStreamResumable = false;

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
    `[deer-flow] Rejected unsupported LangGraph stream mode(s): ${unseenModes.join(", ")}`,
  );
}

export function sanitizeRunStreamOptions<T>(options: T): T {
  if (typeof options !== "object" || options === null) {
    return options;
  }

  const sanitizedOptions = { ...options } as Record<string, unknown>;
  if ("streamResumable" in sanitizedOptions) {
    delete sanitizedOptions.streamResumable;
  }

  if ("streamMode" in sanitizedOptions) {
    const streamMode = sanitizedOptions.streamMode;
    if (streamMode != null) {
      const requestedModes = Array.isArray(streamMode) ? streamMode : [streamMode];
      const validModes = requestedModes.filter((mode) =>
        SUPPORTED_RUN_STREAM_MODES.has(mode as any),
      );
      sanitizedOptions.streamMode = validModes;
    }
  }

  return sanitizedOptions as T;
}
