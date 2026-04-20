// Minimal bash / sh syntax highlighter.
//
// Returns an HTML string of the input code with recognized tokens wrapped
// in <span class="sh-…"> tags (see style.css for the palette). Every token
// is HTML-escaped before insertion so the output is safe to feed into
// element.innerHTML.
//
// The grammar deliberately under-highlights: comments, single-/double-
// quoted strings, variables ($NAME, ${NAME}, $(…)), numbers, and a small
// keyword/builtin list. That covers the typical task-card script body
// without the maintenance cost of a full bash parser.

const SHELL_KEYWORDS = new Set<string>([
  'if', 'then', 'else', 'elif', 'fi',
  'for', 'while', 'until', 'do', 'done',
  'case', 'esac', 'function', 'return', 'in', 'select',
  'break', 'continue', 'time',
  'export', 'local', 'readonly', 'declare', 'typeset', 'unset', 'shift',
  'source', 'alias', 'eval', 'exec', 'set', 'trap',
  'true', 'false', 'test',
]);

const SHELL_BUILTINS = new Set<string>([
  'echo', 'printf', 'read', 'cd', 'pwd', 'ls', 'cat', 'grep', 'sed', 'awk',
  'sort', 'uniq', 'head', 'tail', 'wc', 'find', 'xargs', 'curl', 'wget',
  'git', 'make', 'npm', 'node', 'python', 'python3', 'pip', 'bash', 'sh',
  'mkdir', 'rm', 'cp', 'mv', 'touch', 'chmod', 'chown', 'ln',
  'date', 'sleep', 'kill', 'ps', 'tr', 'cut', 'tee',
]);

function escapeHtmlInline(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function isIdentStart(ch: string): boolean {
  return /[A-Za-z_]/.test(ch);
}

function isIdentPart(ch: string): boolean {
  return /[A-Za-z0-9_-]/.test(ch);
}

export function highlightShell(code: string): string {
  const out: string[] = [];
  const n = code.length;
  let i = 0;
  // "At the start of a command" — reset after newlines, pipes, semicolons,
  // and &&/||. Used to decide whether an identifier might be a command
  // name (and therefore eligible for builtin highlighting).
  let atCommandStart = true;

  while (i < n) {
    const ch = code[i];

    // Comments: # only when it's at line start or follows whitespace
    if (ch === '#' && (i === 0 || /\s/.test(code[i - 1]))) {
      let j = i;
      while (j < n && code[j] !== '\n') j++;
      out.push(`<span class="sh-comment">${escapeHtmlInline(code.slice(i, j))}</span>`);
      i = j;
      continue;
    }

    // Shebang on the very first line
    if (i === 0 && ch === '#' && code[1] === '!') {
      let j = 0;
      while (j < n && code[j] !== '\n') j++;
      out.push(`<span class="sh-comment">${escapeHtmlInline(code.slice(0, j))}</span>`);
      i = j;
      continue;
    }

    // Single-quoted (literal) string
    if (ch === "'") {
      let j = i + 1;
      while (j < n && code[j] !== "'") j++;
      if (j < n) j++;
      out.push(`<span class="sh-string">${escapeHtmlInline(code.slice(i, j))}</span>`);
      i = j;
      atCommandStart = false;
      continue;
    }

    // Double-quoted string — tolerate \" escapes; don't try to re-highlight
    // interpolated $VAR inside, just colour the whole thing as a string.
    if (ch === '"') {
      let j = i + 1;
      while (j < n && code[j] !== '"') {
        if (code[j] === '\\' && j + 1 < n) j += 2;
        else j++;
      }
      if (j < n) j++;
      out.push(`<span class="sh-string">${escapeHtmlInline(code.slice(i, j))}</span>`);
      i = j;
      atCommandStart = false;
      continue;
    }

    // Variables: $NAME, ${…}, and the leading $ of $(…)
    if (ch === '$') {
      if (code[i + 1] === '{') {
        let j = i + 2;
        while (j < n && code[j] !== '}') j++;
        if (j < n) j++;
        out.push(`<span class="sh-variable">${escapeHtmlInline(code.slice(i, j))}</span>`);
        i = j;
        atCommandStart = false;
        continue;
      }
      if (code[i + 1] === '(') {
        // Only colour the leading `$(`; let the inner tokens flow so
        // nested keywords / builtins still pick up their own highlight.
        out.push(`<span class="sh-variable">$(</span>`);
        i += 2;
        atCommandStart = true;
        continue;
      }
      if (isIdentStart(code[i + 1]) || /[0-9?!@*#$]/.test(code[i + 1] ?? '')) {
        let j = i + 1;
        if (/[A-Za-z_]/.test(code[j])) {
          while (j < n && /[A-Za-z0-9_]/.test(code[j])) j++;
        } else {
          j++;  // single-char special: $0 $? $! etc.
        }
        out.push(`<span class="sh-variable">${escapeHtmlInline(code.slice(i, j))}</span>`);
        i = j;
        atCommandStart = false;
        continue;
      }
      // Lone `$` — just emit it.
      out.push('$');
      i++;
      continue;
    }

    // Numbers (very loose)
    if (/\d/.test(ch) && (i === 0 || /[\s=(\[:,]/.test(code[i - 1]))) {
      let j = i;
      while (j < n && /[\d.]/.test(code[j])) j++;
      out.push(`<span class="sh-number">${escapeHtmlInline(code.slice(i, j))}</span>`);
      i = j;
      atCommandStart = false;
      continue;
    }

    // Identifiers / keywords / builtins
    if (isIdentStart(ch)) {
      let j = i;
      while (j < n && isIdentPart(code[j])) j++;
      const word = code.slice(i, j);
      if (SHELL_KEYWORDS.has(word)) {
        out.push(`<span class="sh-keyword">${escapeHtmlInline(word)}</span>`);
        // `if`/`then`/`;` etc. all reset command-start; keep it true.
        atCommandStart = true;
      } else if (atCommandStart && SHELL_BUILTINS.has(word)) {
        out.push(`<span class="sh-builtin">${escapeHtmlInline(word)}</span>`);
        atCommandStart = false;
      } else {
        out.push(escapeHtmlInline(word));
        atCommandStart = false;
      }
      i = j;
      continue;
    }

    // Command separators reset command-start so the next identifier is
    // considered a fresh command head.
    if (ch === '\n' || ch === ';' || ch === '|' || ch === '&') {
      out.push(escapeHtmlInline(ch));
      i++;
      atCommandStart = true;
      continue;
    }

    if (/\s/.test(ch)) {
      out.push(ch);
      i++;
      continue;
    }

    // Operator / punctuation fallback — emit as plain text.
    out.push(escapeHtmlInline(ch));
    i++;
  }
  return out.join('');
}
