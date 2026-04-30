// Minimal line-diff renderer for the tool-cell diff view.
//
// Uses plain LCS on split lines. O(m*n) memory — edits rarely exceed a
// few hundred lines on either side, so the tradeoff is fine and we avoid
// a dependency on any diff library. Returns an array of tagged lines
// that the caller (chat.ts) renders with + / − / context prefixes.

export type DiffLine = { type: 'add' | 'del' | 'ctx'; line: string };

export function lineDiff(oldText: string, newText: string): DiffLine[] {
  const A = oldText.length === 0 ? [] : oldText.split('\n');
  const B = newText.length === 0 ? [] : newText.split('\n');
  const m = A.length;
  const n = B.length;

  // LCS length matrix.
  const L: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      L[i][j] = A[i - 1] === B[j - 1] ? L[i - 1][j - 1] + 1 : Math.max(L[i - 1][j], L[i][j - 1]);
    }
  }

  const out: DiffLine[] = [];
  let i = m;
  let j = n;
  while (i > 0 && j > 0) {
    if (A[i - 1] === B[j - 1]) {
      out.push({ type: 'ctx', line: A[i - 1] });
      i--;
      j--;
    } else if (L[i - 1][j] >= L[i][j - 1]) {
      out.push({ type: 'del', line: A[i - 1] });
      i--;
    } else {
      out.push({ type: 'add', line: B[j - 1] });
      j--;
    }
  }
  while (i > 0) {
    out.push({ type: 'del', line: A[i - 1] });
    i--;
  }
  while (j > 0) {
    out.push({ type: 'add', line: B[j - 1] });
    j--;
  }
  return out.reverse();
}

function escape(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Render a list of diff lines as an HTML block. Each line is its own
 *  row so CSS can colour add / del / context independently and wrapping
 *  stays per-line. */
export function renderDiffHtml(lines: DiffLine[]): string {
  const rows = lines.map(({ type, line }) => {
    const prefix = type === 'add' ? '+' : type === 'del' ? '-' : ' ';
    return `<div class="diff-line diff-${type}"><span class="diff-prefix">${prefix}</span><span class="diff-text">${escape(line)}</span></div>`;
  });
  return `<pre class="diff-block"><code>${rows.join('')}</code></pre>`;
}

/** Render a whole-addition block (for `write` / `task_update` when there
 *  is no "before" state on the client). Every line gets the `+` prefix
 *  + green tint so it reads as "all added" rather than a noisy diff. */
export function renderAddOnlyHtml(text: string): string {
  const body = text.length === 0 ? [] : text.split('\n');
  const rows = body.map(line =>
    `<div class="diff-line diff-add"><span class="diff-prefix">+</span><span class="diff-text">${escape(line)}</span></div>`,
  );
  return `<pre class="diff-block"><code>${rows.join('')}</code></pre>`;
}
