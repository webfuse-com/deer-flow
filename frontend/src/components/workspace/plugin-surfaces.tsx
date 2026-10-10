"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useAuth } from "@/core/auth/AuthProvider";
import type {
  PluginSurface,
  SurfaceProject,
  SurfaceSlot,
} from "@/core/extensions/contracts";
import {
  useFrontendExtensions,
  useFrontendServices,
} from "@/core/extensions/hooks";
import {
  activeFrontendExtensions,
  type LoadedContribution,
} from "@/core/extensions/registry";
import {
  bindFrontendServices,
  openConversation,
} from "@/core/extensions/services";
import { mountSurface } from "@/core/extensions/surfaces";
import { useI18n } from "@/core/i18n/hooks";

function Surface({
  entry,
  surface,
  threadId,
  project,
}: {
  entry: LoadedContribution;
  surface: PluginSurface;
  threadId?: string;
  project?: SurfaceProject;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const { locale, t } = useI18n();
  const { user } = useAuth();
  const services = useFrontendServices();
  const currentServices = useRef(services);
  currentServices.current = services;
  const [failed, setFailed] = useState(false);
  const projectId = project?.id;
  const projectName = project?.name;
  const projectStatus = project?.status;
  useEffect(() => {
    if (!ref.current) return;
    const abort = new AbortController();
    const cleanup = mountSurface(
      ref.current,
      surface,
      {
        namespace: entry.namespace,
        locale,
        settings: entry.settings,
        threadId,
        project:
          projectId !== undefined
            ? {
                id: projectId,
                name: projectName ?? "",
                status: projectStatus ?? "",
              }
            : undefined,
        openConversation: (id, signal) =>
          openConversation(id, (path) => router.push(path), signal),
        callBackend: bindFrontendServices(
          currentServices.current,
          entry,
          abort.signal,
        ).callBackend,
      },
      () => setFailed(true),
    );
    return () => {
      abort.abort();
      cleanup();
    };
  }, [
    entry,
    surface,
    locale,
    threadId,
    projectId,
    projectName,
    projectStatus,
    user?.id,
    router,
  ]);
  return (
    <section aria-label={surface.title}>
      {failed && <p role="alert">{t.extensions.viewFailed}</p>}
      <div ref={ref} />
    </section>
  );
}

export function PluginSurfaces({
  slot,
  namespace,
  surfaceId,
  threadId,
  project,
}: {
  slot: SurfaceSlot;
  namespace?: string;
  surfaceId?: string;
  threadId?: string;
  project?: SurfaceProject;
}) {
  const query = useFrontendExtensions();
  const { user } = useAuth();
  return activeFrontendExtensions(query.data ?? [])
    .filter(
      ({ contribution }) => !namespace || contribution.namespace === namespace,
    )
    .flatMap(({ contribution: entry, extension }) =>
      (extension.surfaces ?? [])
        .filter(
          (surface) =>
            surface.slot === slot && (!surfaceId || surface.id === surfaceId),
        )
        .map((surface) => (
          <Surface
            key={`${user?.id}:${threadId}:${project?.id}:${entry.namespace}:${surface.id}`}
            entry={entry}
            surface={surface}
            threadId={threadId}
            project={project}
          />
        )),
    );
}
