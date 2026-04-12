/**
 * Shared token-count formatting used across the Settings provider
 * editor and the canvas node cards.
 *
 * Rule (per user spec):
 *   n >= 1M  → "X.XM" / "XM"   (M = 1024 * 1024)
 *   n <  1M  → "X.Xk" / "Xk"   (k = 1024)
 *
 * The conversation panel keeps its own raw `↑prompt ↓completion` display
 * and intentionally does not use this helper.
 */

const K = 1024;
const M = 1024 * 1024;

/** Format a token count in the "big numbers only" k/M convention.
 *  Returns an empty string for null/undefined. */
export function formatTokensKM(n: number | null | undefined): string {
  if (n == null) return "";
  if (n >= M) {
    const v = n / M;
    return v >= 10 || v % 1 === 0 ? `${Math.round(v)}M` : `${v.toFixed(1)}M`;
  }
  const v = n / K;
  return v >= 10 || v % 1 === 0 ? `${Math.round(v)}k` : `${v.toFixed(1)}k`;
}
