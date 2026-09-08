"use client";

import {
  LockIcon,
  type LucideIcon,
  type LucideProps,
  UsersIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Argus patch #89: the one place the "Internalize" glyph lives. The thread
 * header trigger and the artifact action both render it, so the private and
 * shared looks change here and nowhere else. Private is a lock (only you);
 * shared is people (everyone at the company). Callers add the colour:
 * muted while private, `text-primary` once shared.
 */
export function shareStateGlyph(shared: boolean): LucideIcon {
  return shared ? UsersIcon : LockIcon;
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
      className={cn("size-4", className)}
      data-shared={shared ? "true" : "false"}
      {...props}
    />
  );
}
