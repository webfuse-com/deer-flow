import { redirect } from "next/navigation";

// The old creation wizard URL. Agents are created with New agent in the
// gallery ([argus patch #110]); keep this URL alive as a redirect so stale
// links and bookmarks land on the gallery instead of 404.
export default function NewAgentPage() {
  redirect("/workspace/agents");
}
