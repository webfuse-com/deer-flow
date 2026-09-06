"use client";

import { Landmark, type LucideIcon, type LucideProps } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Argus patch #89: the one place the "Internalize" glyph lives. The thread
 * header trigger and the artifact action both render it, so swapping the
 * private and shared looks is a change here and nowhere else. Today both
 * states draw the temple (to the Agora); the shared state is carried by the
 * colour the callers apply and by `data-shared`.
 */
export function shareStateGlyph(_shared: boolean): LucideIcon {
  return Landmark;
}

export function ShareStateIcon({
  shared,
  className,
  ...props
}: LucideProps & { shared: boolean }) {
  const Glyph = shareStateGlyph(shared);
  return (
    <Glyph
      aria-hidden="true"
      className={cn(className)}
      data-shared={shared ? "true" : "false"}
      {...props}
    />
  );
}
