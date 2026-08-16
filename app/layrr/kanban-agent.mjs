/**
 * [b2react] layrr → kanban sink
 *
 * Copied over layrr's `dist/agents/claude.js` by scripts/dev-layrr.mjs. layrr
 * imports `{ ClaudeAgent, checkClaude }` from that path, so matching those two
 * exports is the whole contract — no changes to layrr's registry required.
 *
 * Instead of spawning Claude Code inline with --dangerously-skip-permissions on
 * a 60-second timeout, an edit request becomes a ticket on the kanban board. The
 * orchestrator picks it up, works it in its own worktree under the board's
 * commit gate, and a human reviews the branch.
 *
 * The ticket hands the worker everything it would otherwise spend minutes
 * rediscovering — see toDetail(). Measured on ticket #13, a worker burned ~4
 * minutes on `git worktree add`, hunting for node_modules and `npm ci` before
 * reading a line of code; a warm spare from layrr-prepare-worktree.mjs removes
 * all of it.
 *
 * Env, passed by dev-layrr.mjs to the layrr child:
 *   LAYRR_KANBAN_URL      base url of kanban_server.py  (default 127.0.0.1:8745)
 *   LAYRR_KANBAN_BOARD    board slug                    (default b2react-local-dev)
 *   LAYRR_KANBAN_COLUMN   column to file into           (default ready)
 *   LAYRR_TICKET_MODEL    model override stamped on each ticket (default none —
 *                         the orchestrator's triage picks per ticket)
 *   LAYRR_POOL_STATE      warm-worktree state json
 *   LAYRR_PREPARE_SCRIPT  script that refills the spare
 *
 * Kept in the repo rather than as a string inside dev-layrr.mjs so it stays
 * readable, lintable and reviewable.
 */
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { buildPrompt } from './prompt.js';

const BASE = process.env.LAYRR_KANBAN_URL || 'http://127.0.0.1:8745';
const BOARD = process.env.LAYRR_KANBAN_BOARD || 'b2react-local-dev';
const COLUMN = process.env.LAYRR_KANBAN_COLUMN || 'ready';
const MODEL = process.env.LAYRR_TICKET_MODEL || '';
const POOL_STATE = process.env.LAYRR_POOL_STATE || '';
const PREPARE_SCRIPT = process.env.LAYRR_PREPARE_SCRIPT || '';
const REPO_ROOT = process.env.LAYRR_REPO_ROOT || '';
const BASE_BRANCH = process.env.LAYRR_BASE_BRANCH || 'working';

/** Preflight. layrr aborts before binding the proxy if this is not ok. */
export async function checkClaude() {
  try {
    const res = await fetch(`${BASE}/api/files`, {
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok)
      return { ok: false, error: `kanban server returned ${res.status}` };
    const boards = await res.json();
    const names = (Array.isArray(boards) ? boards : []).map(b => b.filename);
    if (!names.includes(BOARD)) {
      return {
        ok: false,
        error: `board "${BOARD}" not found. Available: ${names.join(', ') || '(none)'}`,
      };
    }
    return { ok: true };
  } catch (err) {
    return {
      ok: false,
      error: `kanban server unreachable at ${BASE} (${err.message})`,
    };
  }
}

// ── warm worktree ───────────────────────────────────────────────────────────

function readPool() {
  if (!POOL_STATE) return {};
  try {
    return JSON.parse(readFileSync(POOL_STATE, 'utf-8'));
  } catch {
    return {};
  }
}

/**
 * Fast-forward a spare onto the current tip of the base branch.
 *
 * A spare is a branch pointer at `working` as it stood when warming began, and
 * `working` moves every time an agent merges. Without this the second ticket
 * onwards would branch from a stale base and have its push rejected as
 * non-fast-forward. The spare carries no commits of its own, so a hard reset is
 * both safe and instant — and node_modules is untouched by it.
 *
 * Returns whether package-lock.json moved, because that is the one case where
 * the pre-installed node_modules is no longer trustworthy.
 */
function freshenSpare(spare) {
  const at = args =>
    execFileSync('git', args, {
      cwd: spare.path,
      encoding: 'utf-8',
      windowsHide: true,
    }).trim();
  try {
    const before = at(['rev-parse', 'HEAD']);
    const target = at(['rev-parse', BASE_BRANCH]);
    if (before === target) return { moved: false, lockChanged: false };
    const changed = at([
      'diff',
      '--name-only',
      before,
      target,
      '--',
      '*package-lock.json',
    ]);
    at(['reset', '--hard', target]);
    return { moved: true, lockChanged: Boolean(changed) };
  } catch {
    return { moved: false, lockChanged: false, failed: true };
  }
}

/**
 * The main checkout, which is what an agent pushes into. Prefer what the
 * launcher passed, but derive it from the spare when that is missing so a
 * ticket never ships a `git push ""` that cannot possibly work.
 */
function repoRootFor(spare) {
  if (REPO_ROOT) return REPO_ROOT;
  try {
    const common = execFileSync(
      'git',
      ['rev-parse', '--path-format=absolute', '--git-common-dir'],
      { cwd: spare.path, encoding: 'utf-8', windowsHide: true }
    ).trim();
    return resolve(dirname(common));
  } catch {
    return '<path to the b2-react checkout>';
  }
}

/** Take the warm spare if it is genuinely usable, and start brewing the next. */
function claimSpare() {
  const state = readPool();
  const ready = state.ready;
  const usable =
    ready?.path &&
    ready?.bundlePath &&
    existsSync(ready.path) &&
    existsSync(join(ready.bundlePath, 'node_modules'));

  if (usable) {
    ready.freshened = freshenSpare(ready);
    try {
      // Record it as busy rather than discarding it. Once its branch lands in
      // the base branch the prepare script recycles the worktree instead of
      // building another — reset+clean costs seconds, `npm ci` costs minutes.
      const busy = Array.isArray(state.busy) ? state.busy : [];
      busy.push({ ...ready, claimedAt: new Date().toISOString() });
      writeFileSync(
        POOL_STATE,
        JSON.stringify({ ...state, ready: null, busy }, null, 2)
      );
    } catch {
      /* a stale spare is better than no ticket */
    }
  }
  refillSpare();
  return usable ? ready : null;
}

/** Detached so the browser gets its answer immediately. */
function refillSpare() {
  if (!PREPARE_SCRIPT) return;
  try {
    spawn(process.execPath, [PREPARE_SCRIPT], {
      detached: true,
      stdio: 'ignore',
      // Without this a detached child gets its own console window, which pops
      // up on screen every time someone submits an edit from the overlay.
      windowsHide: true,
    }).unref();
  } catch {
    /* the worker can still make its own worktree */
  }
}

/**
 * Keep the pool's record of this worktree pointing at its real branch.
 *
 * The rename happens after the claim (the ticket id is only known once the POST
 * returns), so without this the pool would remember a branch name that no
 * longer exists — and the reclaim check, which asks whether that branch has
 * merged, would answer "no" forever and the worktree would never be recycled.
 */
function recordBranch(spare, branch) {
  try {
    const state = readPool();
    const busy = (state.busy || []).map(e =>
      e.path === spare.path ? { ...e, branch } : e
    );
    writeFileSync(POOL_STATE, JSON.stringify({ ...state, busy }, null, 2));
  } catch {
    /* the worktree still works; it just may not get recycled */
  }
}

function renameBranch(spare, id, title) {
  const slug =
    String(title)
      .replace(/[^a-zA-Z0-9]+/g, '-')
      .replace(/^-|-$/g, '')
      .split('-')
      .slice(0, 5)
      .join('-') || 'Layrr';
  const branch = `${id}-Layrr-${slug}`;
  try {
    execFileSync('git', ['branch', '-m', branch], {
      cwd: spare.path,
      stdio: 'ignore',
      windowsHide: true,
    });
    return branch;
  } catch {
    return spare.branch;
  }
}

// ── ticket content ──────────────────────────────────────────────────────────

function toTitle(request) {
  const words = String(request.instruction || 'layrr edit')
    .replace(/\s+/g, ' ')
    .trim();
  const short = words.length > 72 ? `${words.slice(0, 69).trimEnd()}…` : words;
  const loc = request.sourceLocation;
  if (loc?.filePath) {
    const base = loc.filePath.split(/[\\/]/).pop();
    return `${short} (${base}:${loc.line})`;
  }
  return short;
}

/** Paths from layrr are absolute inside the capture tree — useless in another
 *  worktree. Re-express them relative to the bundle so they resolve anywhere. */
function relToBundle(filePath, projectRoot) {
  try {
    const rel = relative(projectRoot, filePath);
    return rel.startsWith('..') ? filePath : rel.replace(/\\/g, '/');
  } catch {
    return filePath;
  }
}

/**
 * buildPrompt() bakes the capture tree's absolute paths into its "Source
 * location found" block. Those point at the worktree serving the browser, not
 * the one the worker was given — on ticket #13 that cost 45 seconds of cd-ing
 * around the wrong tree. Strip the prefix so every path in the ticket is
 * bundle-relative and resolves wherever the worker actually is.
 */
function stripCaptureRoot(text, projectRoot) {
  let out = text;
  for (const root of [projectRoot, projectRoot.replace(/\\/g, '/')]) {
    for (const sep of ['\\', '/', '']) {
      out = out.split(root + sep).join('');
    }
  }
  return out;
}

/**
 * A dev-server port unique to this worktree, in 5300-5399.
 *
 * Every worktree otherwise defaults to 5173 and they fight. Derived from the
 * path so it is stable across restarts of the same ticket and different between
 * concurrent ones.
 */
function devPortFor(spare) {
  let h = 0;
  for (const ch of spare.path) h = (h * 31 + ch.charCodeAt(0)) % 100;
  return 5300 + h;
}

function toDetail(request, projectRoot, spare) {
  const locs = [];
  if (request.sourceLocation?.filePath) {
    locs.push(request.sourceLocation);
  }
  for (const el of request.elements || []) {
    if (el.sourceLocation?.filePath) locs.push(el.sourceLocation);
  }
  const rels = [
    ...new Set(
      locs.map(l => `${relToBundle(l.filePath, projectRoot)}:${l.line}`)
    ),
  ];

  const setup = spare
    ? [
        '## Your workspace is already prepared',
        '',
        `A git worktree was created and \`npm ci\`'d **before this ticket existed**,`,
        'precisely so you do not have to.',
        '',
        `- **Worktree:** \`${spare.path}\``,
        `- **Bundle (run all npm commands here):** \`${spare.bundlePath}\``,
        `- **Branch:** already checked out — \`git -C "${spare.path}" branch --show-current\``,
        `- **Base:** fast-forwarded to \`${BASE_BRANCH}\` when this ticket was filed.`,
        spare.freshened?.lockChanged
          ? '- ⚠️ **`package-lock.json` changed in that fast-forward — run `npm ci` once before the gate.**'
          : '- **`node_modules` is installed and current.**',
        '',
        '**Do NOT run `git worktree add`. Do NOT run `npm ci` or `npm install`.**',
        'Doing so re-pays four minutes this ticket already paid for you.',
        '',
        '### If you need to run the app',
        '',
        `\`\`\`\nSF_UIBUNDLE_PORT=${devPortFor(spare)} npm run dev\n\`\`\``,
        '',
        '`@salesforce/vite-plugin-ui-bundle` returns `server.port` from its',
        "`config()` hook, and a plugin's config beats the CLI flag — so `--port`",
        'is silently ignored and every worktree lands on 5173 and fights every',
        'other one. `SF_UIBUNDLE_PORT` is the only thing that moves it. The port',
        'above is derived from this worktree, so concurrent tickets do not collide.',
        'Vite binds `::1`, not `127.0.0.1` — probe `localhost` when checking it is up.',
        '',
        '### When you are finished: merge into `working`',
        '',
        'This worktree was branched from `working`, the integration branch a human',
        'reviews. Do **not** merge into `main`, and do not push to a remote — there',
        'is none. After committing, from your worktree:',
        '',
        `\`\`\`\ngit push "${repoRootFor(spare)}" HEAD:working\n\`\`\``,
        '',
        '`working` is checked out in the layrr worktree, so the usual',
        'checkout-and-merge is refused by git. The repo sets',
        '`receive.denyCurrentBranch=updateInstead`, so this push updates the branch',
        '**and** that working tree in place — which is the point: your change shows',
        'up live in the app the reviewer is looking at.',
        '',
        'Rejected as non-fast-forward? `git merge working` first, resolve, re-run',
        'the gate, then push again.',
      ].join('\n')
    : [
        '## No prepared workspace',
        '',
        'The warm-worktree pool was empty, so follow the board default: create a',
        'worktree and install dependencies yourself.',
      ].join('\n');

  return [
    'Raised from **layrr** — someone selected this element in the running B2 app',
    'and described the change they wanted.',
    '',
    setup,
    '',
    '## The request',
    '',
    stripCaptureRoot(buildPrompt(request), projectRoot),
    '',
    '---',
    '',
    rels.length
      ? `**Target (relative to the bundle):**\n${rels.map(r => `- \`${r}\``).join('\n')}`
      : '**Target:** not resolved.',
    '',
    '> ⚠️ **Verify the location before editing.** layrr maps a click back to source',
    '> through React fiber and falls back to a scored text search, and it does get',
    '> this wrong — on ticket #13 it named a line in an unrelated component. Match',
    '> the element by its **class list and selector above**, not by the line number.',
    '> If they disagree, trust the class list.',
  ].join('\n');
}

// ── entry point ─────────────────────────────────────────────────────────────

export class ClaudeAgent {
  name = 'claude';
  displayName = 'Kanban ticket';
  projectRoot;

  constructor(opts) {
    this.projectRoot = opts.projectRoot;
  }

  async applyEdit(request) {
    const spare = claimSpare();
    const title = toTitle(request);
    const body = {
      title,
      detail: toDetail(request, this.projectRoot, spare),
      column: COLUMN,
    };
    if (MODEL) body.model = MODEL;

    try {
      const res = await fetch(
        `${BASE}/api/board/${encodeURIComponent(BOARD)}/task`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(15000),
        }
      );

      if (!res.ok) {
        const text = await res.text().catch(() => '');
        return {
          success: false,
          message: `kanban rejected the ticket (${res.status}) ${text.slice(0, 160)}`,
        };
      }

      const created = await res.json().catch(() => ({}));
      const id = created.id ?? created.task?.id ?? '?';
      const branch = spare ? renameBranch(spare, id, title) : null;
      if (spare && branch) recordBranch(spare, branch);

      return {
        success: true,
        message: branch
          ? `Filed #${id} on ${BOARD} — worktree ready on ${branch}`
          : `Filed ticket #${id} on ${BOARD} (${COLUMN}, no warm worktree)`,
      };
    } catch (err) {
      return {
        success: false,
        message: `could not reach kanban at ${BASE}: ${err.message}`,
      };
    }
  }
}
