// [argus patch #167] The command palette's own matching and ordering.
// cmdk's default filter is fuzzy (letters in order, anywhere), so "drata"
// matched long app titles, and cmdk 1.1 never reorders groups (its group sort
// looks groups up by id while the DOM carries the heading), so a weak match in
// an early group sat above an exact one in a later group. Here every query word
// must start a word of the label, description or a keyword, or occur inside
// the label; groups are ordered by their best match, items by score.

export type SearchableItem = {
  label: string;
  description?: string;
  keywords?: readonly string[];
};

const WORD_BREAK = /[\s\-_·.,:;/()|&'"]+/;

function words(text: string): string[] {
  return text.split(WORD_BREAK).filter(Boolean);
}

/** 0 when any query word is missing; higher is a better match. */
export function commandScore(item: SearchableItem, query: string): number {
  const needle = query.trim().toLowerCase();
  const terms = needle.split(/\s+/).filter(Boolean);
  if (!terms.length) return 1;
  const label = item.label.toLowerCase();
  const labelWords = words(label);
  const otherWords = words(
    [item.description ?? "", ...(item.keywords ?? [])].join(" ").toLowerCase(),
  );
  let score = label === needle ? 100 : 0;
  for (const term of terms) {
    if (label.startsWith(term)) score += 8;
    else if (labelWords.some((word) => word.startsWith(term))) score += 6;
    else if (otherWords.includes(term)) score += 5;
    else if (otherWords.some((word) => word.startsWith(term))) score += 4;
    else if (label.includes(term)) score += 2;
    else return 0;
  }
  return score;
}

/** Matching items only, best group first; with no query, the given order. */
export function rankCommandGroups<
  G extends { items: readonly SearchableItem[] },
>(
  groups: readonly G[],
  query: string,
): (Omit<G, "items"> & { items: G["items"][number][] })[] {
  if (!query.trim())
    return groups
      .filter((group) => group.items.length > 0)
      .map((group) => ({ ...group, items: [...group.items] }));
  return groups
    .map((group, index) => {
      const scored = group.items
        .map((item, order) => ({
          item,
          order,
          score: commandScore(item, query),
        }))
        .filter((entry) => entry.score > 0)
        .sort((a, b) => b.score - a.score || a.order - b.order);
      return {
        group: { ...group, items: scored.map((entry) => entry.item) },
        best: scored[0]?.score ?? 0,
        index,
      };
    })
    .filter((entry) => entry.best > 0)
    .sort((a, b) => b.best - a.best || a.index - b.index)
    .map((entry) => entry.group);
}
