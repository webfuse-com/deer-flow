"use client";

import { LoaderIcon, TriangleAlertIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { formatBytes, formatSnapshotDate } from "@/core/sharing/format";
import {
  useFileShareStatus,
  useInternalizeFile,
  useRevokeFileShare,
} from "@/core/sharing/hooks";
import { getFileName } from "@/core/utils/files";

import { SharingErrorAlert, ShareLinkRow } from "../internalize-dialog";

export function InternalizeFileDialog({
  threadId,
  filepath,
  open,
  onOpenChange,
}: {
  threadId: string;
  filepath: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const status = useFileShareStatus(threadId, filepath, { enabled: open });
  const internalize = useInternalizeFile(threadId, filepath);
  const revoke = useRevokeFileShare(threadId, filepath);

  useEffect(() => {
    if (!open) {
      setConfirmRevoke(false);
    }
  }, [open]);

  const shared = status.data?.shared === true;
  const share = status.data?.share ?? null;
  const busy = internalize.isPending || revoke.isPending;
  const error = [status.error, internalize.error, revoke.error].find(
    (candidate): candidate is Error => candidate instanceof Error,
  );
  const name = getFileName(filepath);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {shared
              ? t.sharing.artifact.shared
              : t.sharing.artifact.dialogTitle(name)}
          </DialogTitle>
          <DialogDescription>
            {shared && share
              ? t.sharing.sharedSince(formatSnapshotDate(share.shared_at))
              : t.sharing.artifact.dialogDescription}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 text-sm">
          {status.isPending && <Skeleton className="h-4 w-2/3" />}
          {error && (
            <SharingErrorAlert
              error={error}
              onRetry={() => void status.refetch()}
            />
          )}
          {shared && share && status.data?.share_url && (
            <>
              <ShareLinkRow url={status.data.share_url} />
              <ul className="text-muted-foreground flex flex-col gap-1">
                <li>
                  {share.title} ({formatBytes(share.bytes)})
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
          {!shared && status.isSuccess && (
            <p className="text-muted-foreground">
              <code className="text-foreground">{name}</code>
            </p>
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
                disabled={busy || !status.isSuccess}
                onClick={() => {
                  internalize.mutate(undefined, {
                    onSuccess: () => toast.success(t.sharing.artifact.success),
                  });
                }}
              >
                {internalize.isPending && (
                  <LoaderIcon className="size-4 animate-spin" />
                )}
                {t.sharing.artifact.confirm}
              </Button>
            </>
          ) : confirmRevoke ? (
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
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
