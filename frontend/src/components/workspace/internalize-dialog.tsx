"use client";

import {
  CopyIcon,
  ExternalLinkIcon,
  LoaderIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { writeTextToClipboard } from "@/core/clipboard";
import { useI18n } from "@/core/i18n/hooks";
import { SharingRequestError } from "@/core/sharing/api";
import { formatBytes, formatSnapshotDate } from "@/core/sharing/format";
import {
  useInternalizeThread,
  useRefreshThreadShare,
  useRevokeThreadShare,
  useThreadSharePreview,
  useThreadShareStatus,
} from "@/core/sharing/hooks";

function errorOf(...candidates: unknown[]): Error | null {
  for (const candidate of candidates) {
    if (candidate instanceof Error) {
      return candidate;
    }
  }
  return null;
}

export function SharingErrorAlert({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const sessionExpired =
    error instanceof SharingRequestError && error.isSessionExpired;
  return (
    <Alert variant="destructive">
      <TriangleAlertIcon />
      <AlertTitle>
        {sessionExpired ? t.sharing.sessionExpired : t.sharing.failed}
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-2">
        <span>{error.message}</span>
        <span>
          {sessionExpired ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => window.location.reload()}
            >
              {t.sharing.reload}
            </Button>
          ) : onRetry ? (
            <Button size="sm" variant="outline" onClick={onRetry}>
              {t.sharing.retry}
            </Button>
          ) : null}
        </span>
      </AlertDescription>
    </Alert>
  );
}

export function ShareLinkRow({ url }: { url: string }) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground text-xs">
        {t.sharing.linkLabel}
      </span>
      <div className="flex items-center gap-2">
        <Input
          readOnly
          value={url}
          aria-label={t.sharing.linkLabel}
          onFocus={(event) => event.currentTarget.select()}
        />
        <Button
          size="icon"
          variant="outline"
          aria-label={t.sharing.copyLink}
          title={t.sharing.copyLink}
          onClick={() => {
            void writeTextToClipboard(url).then((didCopy) => {
              if (didCopy) {
                toast.success(t.sharing.linkCopied);
              } else {
                toast.error(t.clipboard.failedToCopyToClipboard);
              }
            });
          }}
        >
          <CopyIcon />
        </Button>
        <Button size="icon" variant="outline" asChild>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={t.sharing.openLink}
            title={t.sharing.openLink}
          >
            <ExternalLinkIcon />
          </a>
        </Button>
      </div>
    </div>
  );
}

export function InternalizeDialog({
  threadId,
  open,
  onOpenChange,
}: {
  threadId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const [includeToolBodies, setIncludeToolBodies] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  const status = useThreadShareStatus(threadId, { enabled: open });
  const shared = status.data?.shared === true;
  const preview = useThreadSharePreview(threadId, includeToolBodies, {
    enabled: open && status.isSuccess && !shared,
  });
  const internalize = useInternalizeThread(threadId);
  const refresh = useRefreshThreadShare(threadId);
  const revoke = useRevokeThreadShare(threadId);

  useEffect(() => {
    if (!open) {
      setConfirmRevoke(false);
    }
  }, [open]);

  const busy = internalize.isPending || refresh.isPending || revoke.isPending;
  const tooLarge =
    preview.error instanceof SharingRequestError &&
    preview.error.code === "snapshot_too_large";
  const blockingError = errorOf(
    status.error,
    tooLarge ? null : preview.error,
    internalize.error,
    refresh.error,
    revoke.error,
  );

  const share = status.data?.share ?? null;
  const shareUrl = status.data?.share_url ?? "";
  const previewData = preview.data;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {shared ? t.sharing.shared : t.sharing.dialogTitle}
          </DialogTitle>
          <DialogDescription>
            {shared && share
              ? t.sharing.sharedSince(formatSnapshotDate(share.snapshot_at))
              : t.sharing.dialogDescription}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 text-sm">
          {status.isPending && (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          )}

          {blockingError && (
            <SharingErrorAlert
              error={blockingError}
              onRetry={() => {
                void status.refetch();
                void preview.refetch();
              }}
            />
          )}

          {!shared && status.isSuccess && (
            <>
              {preview.isPending && !tooLarge && (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-4 w-1/3" />
                </div>
              )}
              {previewData && (
                <ul className="text-muted-foreground flex flex-col gap-1">
                  <li>
                    {t.sharing.preview(
                      previewData.counts.human ?? 0,
                      previewData.counts.ai ?? 0,
                      previewData.counts.tool ?? 0,
                    )}
                  </li>
                  <li>
                    {t.sharing.textSize(formatBytes(previewData.text_bytes))}
                  </li>
                  <li>
                    {previewData.files.count === 0
                      ? t.sharing.noFiles
                      : previewData.files.included
                        ? t.sharing.files(
                            previewData.files.count,
                            formatBytes(previewData.files.bytes),
                          )
                        : t.sharing.filesTooLarge(
                            formatBytes(previewData.files.bytes),
                            "10 MB",
                          )}
                  </li>
                </ul>
              )}
              <label className="flex items-start justify-between gap-4">
                <span className="flex flex-col gap-0.5">
                  <span className="font-medium">
                    {t.sharing.includeToolBodies}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {t.sharing.includeToolBodiesHint}
                  </span>
                </span>
                <Switch
                  aria-label={t.sharing.includeToolBodies}
                  checked={includeToolBodies}
                  disabled={busy}
                  onCheckedChange={setIncludeToolBodies}
                />
              </label>
              {previewData && previewData.scrub.count > 0 && (
                <Alert className="border-amber-500/50 text-amber-700 dark:text-amber-400">
                  <TriangleAlertIcon />
                  <AlertTitle>
                    {t.sharing.scrubWarning(previewData.scrub.count)}
                  </AlertTitle>
                  <AlertDescription>
                    <span className="flex flex-wrap gap-1">
                      {Object.entries(previewData.scrub.kinds).map(
                        ([kind, count]) => (
                          <span
                            key={kind}
                            className="rounded border px-1.5 py-0.5 text-xs"
                          >
                            {kind} x{count}
                          </span>
                        ),
                      )}
                    </span>
                    <span>{t.sharing.scrubHint}</span>
                  </AlertDescription>
                </Alert>
              )}
              {tooLarge && preview.error && (
                <Alert variant="destructive">
                  <TriangleAlertIcon />
                  <AlertTitle>{t.sharing.tooLarge}</AlertTitle>
                  <AlertDescription>{preview.error.message}</AlertDescription>
                </Alert>
              )}
            </>
          )}

          {shared && share && (
            <>
              <ShareLinkRow url={shareUrl} />
              <ul className="text-muted-foreground flex flex-col gap-1">
                <li>
                  {share.include_tool_bodies
                    ? t.sharing.toolBodiesIncluded
                    : t.sharing.toolBodiesOmitted}
                </li>
                <li>
                  {share.files_included
                    ? t.sharing.filesBundled(formatBytes(share.files_bytes))
                    : t.sharing.filesNotIncluded}
                </li>
                {share.scrub_findings > 0 && (
                  <li>{t.sharing.scrubWarning(share.scrub_findings)}</li>
                )}
              </ul>
              {confirmRevoke && (
                <Alert variant="destructive">
                  <TriangleAlertIcon />
                  <AlertDescription>{t.sharing.revokeConfirm}</AlertDescription>
                </Alert>
              )}
            </>
          )}
        </div>

        <DialogFooter>
          {!shared ? (
            <>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => onOpenChange(false)}
              >
                {t.common.cancel}
              </Button>
              <Button
                disabled={
                  busy ||
                  !status.isSuccess ||
                  tooLarge ||
                  !previewData ||
                  Boolean(blockingError)
                }
                onClick={() => {
                  internalize.mutate(includeToolBodies, {
                    onSuccess: () => toast.success(t.sharing.success),
                  });
                }}
              >
                {internalize.isPending && (
                  <LoaderIcon className="size-4 animate-spin" />
                )}
                {internalize.isPending
                  ? t.sharing.confirming
                  : t.sharing.confirm}
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => {
                  refresh.mutate(undefined, {
                    onSuccess: () => toast.success(t.sharing.refreshed),
                  });
                }}
              >
                {refresh.isPending ? (
                  <LoaderIcon className="size-4 animate-spin" />
                ) : (
                  <RefreshCwIcon className="size-4" />
                )}
                {t.sharing.refresh}
              </Button>
              {confirmRevoke ? (
                <Button
                  variant="destructive"
                  disabled={busy}
                  onClick={() => {
                    revoke.mutate(undefined, {
                      onSuccess: () => {
                        setConfirmRevoke(false);
                        toast.success(t.sharing.revoked);
                      },
                    });
                  }}
                >
                  {revoke.isPending && (
                    <LoaderIcon className="size-4 animate-spin" />
                  )}
                  {t.sharing.revokeYes}
                </Button>
              ) : (
                <Button
                  variant="destructive"
                  disabled={busy}
                  onClick={() => setConfirmRevoke(true)}
                >
                  {t.sharing.revoke}
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
