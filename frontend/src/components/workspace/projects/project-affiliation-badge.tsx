"use client";

import { Folder } from "lucide-react";
import Link from "next/link";

import { useProject } from "@/core/projects";

/** The project a chat is filed under (or, for a new project chat, will be
 * filed under on its first message), linking to the project page. */
export function ProjectAffiliationBadge({ projectId }: { projectId: string }) {
  const { data: project } = useProject(projectId);
  if (!project) {
    return null;
  }
  return (
    <Link
      href={`/workspace/projects/${encodeURIComponent(project.id)}`}
      className="text-muted-foreground hover:text-foreground inline-flex max-w-40 shrink-0 items-center gap-1 truncate rounded-full border px-2 py-0.5 text-xs font-normal transition-colors"
    >
      <Folder className="size-3 shrink-0" />
      <span className="truncate">{project.name}</span>
    </Link>
  );
}
