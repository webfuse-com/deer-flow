import { redirect } from "next/navigation";

// Agent creation is done through the stack's GitHub repository (edit
// config.yaml + SOUL.md, commit, merge). Keep the old wizard URL alive as a
// redirect so stale links and bookmarks land on the gallery instead of 404.
export default function NewAgentPage() {
  redirect("/workspace/agents");
}
