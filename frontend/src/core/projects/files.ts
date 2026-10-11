/**
 * [argus patch #118] A project whose files live in a repository.
 *
 * A deployment that mirrors project folders from a repository (Argus mirrors
 * `projects/<slug>/` from each citizen's GitHub fork) describes it in the
 * project's `presentation.files`. Mirrored documents carry their path in the
 * repository as `source_name`; the Documents tab then becomes Files, links
 * each file there and lets the app change only the writable folder.
 */
import type { Project, ProjectFilesSource } from "./types";

export type RepoFile = {
  path: string;
  url: string | null;
  editUrl: string | null;
  /** Inside the writable folder: the app may remove it. */
  writable: boolean;
  /** Inside the writable folder with an editable suffix: the app may edit it. */
  editable: boolean;
};

const MIRRORED_KINDS = new Set(["mirror", "edit"]);

function httpsUrl(value: unknown): string | null {
  return typeof value === "string" && value.startsWith("https://")
    ? value
    : null;
}

/** The project's repository description, or null for an ordinary project. */
export function filesSourceOf(
  project: Pick<Project, "presentation">,
): ProjectFilesSource | null {
  const raw: unknown = project.presentation?.files;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const source = raw as Record<string, unknown>;
  const suffixes = Array.isArray(source.editable_suffixes)
    ? source.editable_suffixes.filter(
        (s): s is string => typeof s === "string" && s.startsWith("."),
      )
    : [];
  return {
    folder_url: httpsUrl(source.folder_url) ?? undefined,
    file_url: httpsUrl(source.file_url) ?? undefined,
    edit_url: httpsUrl(source.edit_url) ?? undefined,
    writable: typeof source.writable === "string" ? source.writable : undefined,
    editable_suffixes: suffixes,
  };
}

function fill(template: string | undefined, path: string): string | null {
  if (!template?.includes("{path}")) {
    return null;
  }
  return template.replace(
    "{path}",
    path.split("/").map(encodeURIComponent).join("/"),
  );
}

/** Where a mirrored document lives and what the app may do with it; null for other documents. */
export function repoFileOf(
  document: { source_kind?: string | null; source_name?: string | null },
  source: ProjectFilesSource | null,
): RepoFile | null {
  const path = document.source_name;
  if (
    !source ||
    !path ||
    !MIRRORED_KINDS.has(document.source_kind ?? "") ||
    path.split("/").includes("..")
  ) {
    return null;
  }
  const writable =
    Boolean(source.writable) && path.startsWith(source.writable!);
  const lower = path.toLowerCase();
  return {
    path,
    url: fill(source.file_url, path),
    editUrl: fill(source.edit_url, path),
    writable,
    editable:
      writable &&
      (source.editable_suffixes ?? []).some((suffix) =>
        lower.endsWith(suffix.toLowerCase()),
      ),
  };
}

/**
 * Tab values in display order: the project's `presentation.tabs` first (known
 * values only), then the rest in their default order.
 */
export function orderedTabs(
  defaults: string[],
  project: Pick<Project, "presentation">,
): string[] {
  const wanted: unknown = project.presentation?.tabs;
  const first = Array.isArray(wanted)
    ? wanted.filter(
        (value, index): value is string =>
          typeof value === "string" &&
          defaults.includes(value) &&
          wanted.indexOf(value) === index,
      )
    : [];
  return [...first, ...defaults.filter((value) => !first.includes(value))];
}
