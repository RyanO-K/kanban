/**
 * Kanban ticket widget, injected into layrr-proxied pages.
 *
 * layrr_launcher.py patches layrr's proxy so every proxied HTML page loads
 * this script from the kanban server, with the server url and board slug on
 * the tag's data attributes. It renders a floating panel of the tickets layrr
 * has filed on that board (recognised by the "Raised from **layrr**" marker
 * the kanban-sink agent writes into every detail) and their live status, so
 * the person clicking around the overlay can watch their edits get worked
 * without switching to the board.
 *
 * Fetches are plain cross-origin GETs: the kanban server grants CORS to any
 * loopback origin and its reads are unauthenticated, so no token is needed.
 * Everything is inline-styled and self-contained — this runs inside an
 * arbitrary app's DOM and must neither depend on nor leak styles.
 */
(() => {
  "use strict";
  const tag = document.currentScript
    || document.querySelector("script[data-kanban][data-board]");
  if (!tag) return;
  const base = (tag.dataset.kanban || "").replace(/\/+$/, "");
  const board = tag.dataset.board || "";
  if (!base || !board) return;
  if (window.__kanbanLayrrWidget) return; // one per page, however many injections
  window.__kanbanLayrrWidget = true;

  const LAYRR_MARK = "Raised from **layrr**";
  const DOT = {
    todo: "#94a3b8", ready: "#a78bfa", in_progress: "#3b82f6",
    blocked: "#f59e0b", pending: "#f59e0b", done: "#22c55e",
  };
  const LABEL = {
    todo: "todo", ready: "queued", in_progress: "working",
    blocked: "blocked", pending: "pending", done: "done",
  };
  const STORE_KEY = "kanbanLayrrWidgetOpen";

  // ── DOM ────────────────────────────────────────────────────────────────
  const box = document.createElement("div");
  box.id = "kanban-layrr-widget";
  box.style.cssText =
    "position:fixed;left:16px;bottom:16px;z-index:2147483000;" +
    "font:12px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;" +
    "color:#f1f5f9;user-select:none;";

  const panel = document.createElement("div");
  panel.style.cssText =
    "display:none;flex-direction:column;width:340px;max-height:45vh;" +
    "margin-bottom:8px;background:#0f172a;border:1px solid #334155;" +
    "border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.45);overflow:hidden;";

  const list = document.createElement("div");
  list.style.cssText = "overflow-y:auto;padding:6px;";
  const head = document.createElement("div");
  head.style.cssText =
    "padding:8px 12px;border-bottom:1px solid #1e293b;display:flex;" +
    "justify-content:space-between;align-items:center;gap:8px;";
  head.innerHTML =
    '<span style="font-weight:600;">Layrr tickets — ' + esc(board) + "</span>";
  const boardLink = document.createElement("a");
  boardLink.href = base + "/";
  boardLink.target = "_blank";
  boardLink.rel = "noopener";
  boardLink.textContent = "open board ↗";
  boardLink.style.cssText = "color:#7dd3fc;text-decoration:none;white-space:nowrap;";
  head.appendChild(boardLink);
  panel.appendChild(head);
  panel.appendChild(list);

  const pill = document.createElement("button");
  pill.type = "button";
  pill.style.cssText =
    "display:flex;align-items:center;gap:7px;padding:7px 13px;border:1px solid #334155;" +
    "border-radius:999px;background:#0f172a;color:#f1f5f9;cursor:pointer;" +
    "font:inherit;box-shadow:0 6px 18px rgba(0,0,0,.4);";
  pill.title = "Tickets filed from this layrr session, worked by the kanban";

  box.appendChild(panel);
  box.appendChild(pill);
  document.body.appendChild(box);

  let open = false;
  try { open = localStorage.getItem(STORE_KEY) === "1"; } catch (e) { /* private mode */ }
  const setOpen = (v) => {
    open = v;
    panel.style.display = v ? "flex" : "none";
    try { localStorage.setItem(STORE_KEY, v ? "1" : "0"); } catch (e) { /* ignore */ }
  };
  pill.addEventListener("click", () => setOpen(!open));
  setOpen(open);

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // ── data ───────────────────────────────────────────────────────────────
  let mtime = 0;
  let lastTickets = [];
  let offline = false;

  function renderPill() {
    const active = lastTickets.filter((t) => t._column !== "done").length;
    const total = lastTickets.length;
    pill.innerHTML =
      '<span style="font-size:13px;">⚡</span>' +
      '<span style="font-weight:600;">' +
      (offline ? "kanban offline"
        : total === 0 ? "no tickets yet"
        : active === 0 ? total + " ticket" + (total === 1 ? "" : "s") + " ✓"
        : active + " in flight") +
      "</span>" +
      '<span style="color:#64748b;">' + (open ? "▾" : "▴") + "</span>";
    pill.style.borderColor = offline ? "#7f1d1d" : "#334155";
  }

  function renderList() {
    list.innerHTML = "";
    if (!lastTickets.length) {
      list.innerHTML =
        '<div style="padding:14px;color:#64748b;">Select an element in the ' +
        "overlay and describe a change — it lands here as a ticket.</div>";
      return;
    }
    for (const t of lastTickets) {
      const col = t._column || "todo";
      const row = document.createElement("a");
      row.href = base + "/";
      row.target = "_blank";
      row.rel = "noopener";
      row.style.cssText =
        "display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:7px;" +
        "text-decoration:none;color:inherit;";
      row.onmouseenter = () => { row.style.background = "#1e293b"; };
      row.onmouseleave = () => { row.style.background = ""; };
      row.innerHTML =
        '<span style="width:8px;height:8px;border-radius:50%;flex:none;' +
        "background:" + (DOT[col] || "#94a3b8") +
        (col === "in_progress" ? ";box-shadow:0 0 6px " + DOT[col] : "") +
        '"></span>' +
        '<span style="color:#64748b;flex:none;">#' + esc(t.id) + "</span>" +
        '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' +
        esc(t.title) + "</span>" +
        '<span style="color:' + (DOT[col] || "#94a3b8") + ';flex:none;">' +
        (LABEL[col] || col) + "</span>";
      list.appendChild(row);
    }
  }

  async function tick() {
    try {
      const url = base + "/api/board/" + encodeURIComponent(board) +
        (mtime ? "?since=" + encodeURIComponent(mtime) : "");
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      offline = false;
      if (data.unchanged) { renderPill(); return; }
      mtime = data.mtime || 0;
      lastTickets = (data.tasks || [])
        .filter((t) => typeof t.detail === "string" && t.detail.indexOf(LAYRR_MARK) !== -1)
        .sort((a, b) => (parseInt(b.id, 10) || 0) - (parseInt(a.id, 10) || 0));
      renderPill();
      renderList();
    } catch (e) {
      offline = true;
      renderPill();
    }
  }

  tick();
  setInterval(tick, 4000);
})();
