"use client";

import { CheckIcon, Clock3Icon, PencilIcon, Trash2Icon, XIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { type QueuedMessage } from "@/core/threads/use-thread-queue";
import { cn } from "@/lib/utils";

interface QueuedMessagesProps {
  queue: QueuedMessage[];
  onRemove: (id: string) => void;
  onUpdate?: (id: string, text: string) => void;
  className?: string;
}

export function QueuedMessages({
  queue,
  onRemove,
  onUpdate,
  className,
}: QueuedMessagesProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  if (queue.length === 0) {
    return null;
  }

  const startEditing = (item: QueuedMessage) => {
    setEditingId(item.id);
    setEditText(item.message.text);
  };

  const cancelEditing = () => {
    setEditingId(null);
    setEditText("");
  };

  const saveEditing = (id: string) => {
    if (editText.trim().length > 0) {
      onUpdate?.(id, editText);
    } else {
      onRemove(id);
    }
    setEditingId(null);
    setEditText("");
  };

  return (
    <div className={cn("flex flex-col gap-2 px-1 pb-2", className)}>
      <div className="text-muted-foreground flex items-center gap-1.5 text-xs font-medium">
        <Clock3Icon className="size-3.5" />
        <span>Queued next ({queue.length})</span>
      </div>
      <div className="flex flex-col gap-1.5">
        {queue.map((item, index) => {
          const isEditing = editingId === item.id;
          const text = item.message.text.trim();
          const fileCount = item.message.files?.length ?? 0;

          if (isEditing) {
            return (
              <div
                key={item.id}
                className="bg-muted/80 border-border/80 flex flex-col gap-2 rounded-xl border p-2 text-xs backdrop-blur-sm"
              >
                <textarea
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      saveEditing(item.id);
                    } else if (e.key === "Escape") {
                      cancelEditing();
                    }
                  }}
                  autoFocus
                  rows={2}
                  className="bg-background border-border/50 text-foreground focus:ring-ring w-full resize-none rounded-lg border p-2 text-xs focus:ring-1 focus:outline-none"
                  placeholder="Edit queued message..."
                />
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground text-[10px]">
                    Press Enter to save, Esc to cancel
                  </span>
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      className="text-muted-foreground hover:text-foreground size-6 rounded-md p-0"
                      onClick={cancelEditing}
                      title="Cancel"
                      type="button"
                    >
                      <XIcon className="size-3.5" />
                    </Button>
                    <Button
                      variant="secondary"
                      size="icon-sm"
                      className="size-6 rounded-md p-0"
                      onClick={() => saveEditing(item.id)}
                      title="Save"
                      type="button"
                    >
                      <CheckIcon className="size-3.5" />
                    </Button>
                  </div>
                </div>
              </div>
            );
          }

          return (
            <div
              key={item.id}
              className="bg-muted/60 border-border/60 hover:bg-muted/80 flex items-center justify-between gap-2 rounded-xl border px-3 py-2 text-xs transition-colors backdrop-blur-sm"
            >
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <span className="text-muted-foreground font-mono text-[10px]">
                  #{index + 1}
                </span>
                <span className="truncate font-normal text-foreground">
                  {text || (fileCount > 0 ? `Attached ${fileCount} file(s)` : "Empty message")}
                </span>
                {fileCount > 0 && text && (
                  <span className="text-muted-foreground shrink-0 text-[10px]">
                    (+{fileCount} file{fileCount > 1 ? "s" : ""})
                  </span>
                )}
              </div>
              <div className="flex items-center gap-0.5">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="text-muted-foreground hover:text-foreground size-6 shrink-0 rounded-md p-0"
                  onClick={() => startEditing(item)}
                  title="Edit queued message"
                  type="button"
                >
                  <PencilIcon className="size-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="text-muted-foreground hover:text-destructive size-6 shrink-0 rounded-md p-0"
                  onClick={() => onRemove(item.id)}
                  title="Remove from queue"
                  type="button"
                >
                  <Trash2Icon className="size-3.5" />
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
