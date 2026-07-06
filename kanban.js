// ── User-tunable UI config ──────────────────────────────────────
// Defaults below can be overridden two ways (later wins):
//   1. a window.KANBAN_UI_CONFIG object injected into the page before this script
//   2. a "kanbanUiConfig" localStorage key holding a JSON object, e.g.
//      localStorage.kanbanUiConfig = JSON.stringify({pollMs:3000})
const UI_CONFIG_DEFAULTS = {
  pollMs: 1500,            // board refresh poll interval (ms)
  perfRefreshMs: 3000,     // Performance tab sampling refresh (ms)
  attentionPollMs: 4000,   // notification-bell (needs-attention) refresh interval (ms)
  newProfileModel: "claude-opus-4-8",                          // default model for a new profile
  newProfileTools: ["Read","Edit","Write","Bash","Grep","Glob"], // default allowed tools for a new profile
};
function loadUiConfig(){
  let cfg = Object.assign({}, UI_CONFIG_DEFAULTS, window.KANBAN_UI_CONFIG||{});
  try{
    const stored = JSON.parse(localStorage.getItem("kanbanUiConfig")||"{}");
    if(stored && typeof stored==="object") cfg = Object.assign(cfg, stored);
  }catch(e){}
  return cfg;
}
const UI_CONFIG = loadUiConfig();
const POLL_MS = UI_CONFIG.pollMs;
const COL_KEYS = ["todo","ready","in_progress","blocked","done"];
const COL_LABELS = {todo:"Todo",ready:"Ready",in_progress:"In Progress",blocked:"Blocked",done:"Done"};
// Ticket model picklist (size of model). Loaded from the server at runtime
// (GET /api/models, populated by kanban_server.py's dynamic discovery) so it
// reflects what's actually available rather than a hand-maintained mirror.
// The entries below are the fallback shown until that fetch resolves, or if
// it fails. Empty value clears the override so dispatch falls back to
// triage/profile default.
const MODEL_OPTIONS = [
  {value:"", label:"(default)"},
  {value:"claude-haiku-4-5-20251001", label:"Haiku (small)"},
  {value:"claude-sonnet-4-6", label:"Sonnet (medium)"},
  {value:"claude-opus-4-8", label:"Opus (large)"},
];
(async function loadModelOptions(){
  try{
    const data = await apiFetch("/api/models");
    if(data && Array.isArray(data.models) && data.models.length){
      MODEL_OPTIONS.length = 1; // keep the "(default)" entry
      data.models.forEach(m=>MODEL_OPTIONS.push({value:m.value, label:m.label}));
      // Repopulate the Create Task modal's fModel select so it reflects the
      // live catalog rather than the static HTML fallback (ticket #86).
      const sel = document.getElementById("fModel");
      if(sel){
        const cur = sel.value;
        sel.innerHTML = MODEL_OPTIONS.map(m=>'<option value="'+m.value+'">'+m.label+'</option>').join("");
        sel.value = cur; // restore selection if still valid
      }
    }
  }catch(e){}
})();
function getModelName(modelId){
  const m=MODEL_OPTIONS.find(x=>x.value===modelId);
  if(m)return m.label;
  if(!modelId)return "(default)";
  const match=modelId.match(/claude-(\w+)/);
  return match?match[1].charAt(0).toUpperCase()+match[1].slice(1):modelId;
}
let currentFile=null, lastMtime=0, pollTimer=null, dragTaskId=null, dragFile=null, dragEl=null;
let currentTasks=[], selectedTaskKey=null, currentBoardData=null;
// Ticket #61: while an optimistic drag-drop is reconciling, remember the moved
// card's key + target column so a poll firing mid-move re-renders it into the
// target column (not its stale source) and never duplicates or drops it.
let pendingMove=null;
// Ticket #3: blocked tickets awaiting a human decision, refreshed independently of
// the board poll so the topbar bell stays accurate on every view. _lastAttention
// count drives the badge "bump" pop when a new item arrives.
let attentionTasks=[], _lastAttentionCount=0;

// Unique identifier for a ticket across all boards. The numeric `id` alone
// collides in the "All" view (each board has its own #1, #2, ...), so use the
// per-ticket file path, which the server always sets. Falls back to id.
function taskKey(task){return task._filePath||String(task.id);}

// Returns the ISO timestamp of the most recent agent-authored change on a ticket,
// or null if no agent has touched it. Checks comments (writer==='Claude') and
// history entries that carry a sessionId (always written by an agent).
function lastAgentUpdate(task){
  let latest=null;
  (task.comments||[]).forEach(c=>{
    if(c.writer==="Claude"&&c.timestamp){
      if(!latest||c.timestamp>latest)latest=c.timestamp;
    }
  });
  (task.history||[]).forEach(h=>{
    if(h.sessionId&&h.timestamp){
      if(!latest||h.timestamp>latest)latest=h.timestamp;
    }
  });
  return latest;
}

// Returns the ISO timestamp of the most recent activity of any kind on a ticket
// (history entries, comments, or createdAt), used as the sort key within a column.
function lastUpdate(task){
  let latest=task.createdAt||null;
  (task.history||[]).forEach(h=>{
    if(h.timestamp&&(!latest||h.timestamp>latest))latest=h.timestamp;
  });
  (task.comments||[]).forEach(c=>{
    if(c.timestamp&&(!latest||c.timestamp>latest))latest=c.timestamp;
  });
  return latest;
}

// Column ordering: unread tickets bubble to the top, then everything sorts by
// most recent update (newest first). Stable for equal keys.
function compareCards(a,b){
  const ua=isUnreviewed(a),ub=isUnreviewed(b);
  if(ua!==ub)return ua?-1:1;
  const la=lastUpdate(a)||"",lb=lastUpdate(b)||"";
  if(la>lb)return -1;
  if(la<lb)return 1;
  return 0;
}

// Ready column ordering: sort by explicit `order` field (ascending).
// Tickets without an order sort after those with one, then by numeric id.
// Ready column: explicit `order` field (ascending) takes priority; unordered tickets
// sort last and fall back to lastUpdate desc (most recently active first).
function compareReadyCards(a,b){
  const oa=a.order!=null?Number(a.order):Infinity;
  const ob=b.order!=null?Number(b.order):Infinity;
  if(oa!==ob)return oa<ob?-1:1;
  return compareCards(a,b);
}

function reviewedKey(task){
  return "kanban-reviewed:"+(task._filePath||task.id);
}

function isUnreviewed(task){
  const lu=lastAgentUpdate(task);
  if(!lu)return false;
  const seen=localStorage.getItem(reviewedKey(task));
  return !seen||lu>seen;
}

function markReviewed(task){
  const lu=lastAgentUpdate(task);
  if(lu)localStorage.setItem(reviewedKey(task),lu);
}

function updateMarkAllReadBtn(){
  const btn=$("markAllReadBtn");
  if(!btn)return;
  const hasUnread=currentTasks.some(isUnreviewed);
  btn.style.display=hasUnread?"":"none";
}

function $(id){return document.getElementById(id);}
function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML.replace(/"/g,"&quot;").replace(/'/g,"&#39;");}
// Allow only http/https/mailto and relative URLs as link hrefs; anything with
// another scheme (javascript:, data:, etc.) becomes "#". u is already esc()'d.
function safeUrl(u){var s=(""+u);if(/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(s)){return /^(?:https?|mailto):/i.test(s)?s:String.fromCharCode(35);}return s;}
function showToast(msg,err){const t=$("toast");t.textContent=msg;t.className="show"+(err?" err":"");clearTimeout(t._t);t._t=setTimeout(()=>{t.className="";},2400);}

async function apiFetch(url,opts){opts=opts||{};if(window.KANBAN_TOKEN){opts.headers=Object.assign({"X-Kanban-Token":window.KANBAN_TOKEN},opts.headers||{});}const r=await fetch(url,opts);if(!r.ok)throw new Error("HTTP "+r.status);return r.json();}

// Ticket #65: all top-bar status now flows through one compact pill. setServerDown and
// checkOrchStatus update this shared state; renderStatusPill() collapses it into a single
// dot color + short label, with the full detail (offline reason, orch state, last-updated)
// moved into the pill's title tooltip. No state source was dropped, only the presentation.
const pillState={serverDown:false,orch:null,lastUpdated:""};

function renderStatusPill(){
  const pill=$("statusPill"),dot=$("dot"),text=$("statusText");
  if(!pill)return;
  let kind,label,title;
  if(pillState.serverDown){
    kind="offline";label="offline";
    title="Cannot reach the kanban server. Is kanban_server.py running?";
  } else {
    const o=pillState.orch;
    if(o&&o.stopAllRequested){
      kind="degraded";label="agents stopped";
      title="Stop-all was requested — all agents halted";
    } else if(o&&o.usagePause){
      // Ticket #60: parked by a Claude usage limit; resumes automatically.
      const mins=Math.max(1,Math.round((o.usagePause.remainingSeconds||0)/60));
      kind="degraded";label="usage limited";
      title="Claude usage limit reached — dispatch is paused and will resume automatically when the limit resets (~"+mins+"m).";
    } else if(o&&!o.enabled){
      kind="degraded";label="orch paused";
      title="Orchestrator is paused — not picking up new tickets.";
    } else {
      kind="healthy";label="live";
      title="Connected — orchestrator running.";
    }
    if(pillState.lastUpdated)title+="\n"+pillState.lastUpdated;
  }
  pill.classList.remove("degraded","offline");
  if(kind!=="healthy")pill.classList.add(kind);
  text.textContent=label;
  pill.title=title;
}

function setServerDown(down){
  pillState.serverDown=down;
  if(down)pillState.lastUpdated="";
  renderStatusPill();
}

async function checkOrchStatus(){
  try{
    pillState.orch=await apiFetch("/api/orchestrator/state");
  }catch(e){
    // server is down — pill already reflects offline; clear orch detail.
    pillState.orch=null;
  }
  renderStatusPill();
}

async function loadFiles(){
  try{
    const files=await apiFetch("/api/files");
    setServerDown(false);
    const sel=$("boardSelect");sel.innerHTML="";
    if(!files.length){sel.innerHTML='<option value="">No boards found</option>';return;}
    const total=files.reduce((sum,f)=>sum+f.taskCount,0);
    const allOpt=document.createElement("option");allOpt.value="__all__";allOpt.textContent="All ("+total+")";sel.appendChild(allOpt);
    files.forEach(f=>{const o=document.createElement("option");o.value=f.filename;o.textContent=f.project+" ("+f.taskCount+")";sel.appendChild(o);});
    const prevFile=currentFile;
    const exists=prevFile&&(prevFile==="__all__"||files.some(f=>f.filename===prevFile));
    currentFile=exists?prevFile:allOpt.value;sel.value=currentFile;updateBoardSettingsBtn();startPolling();
  }catch(e){setServerDown(true);}
}

async function poll(){
  if(!currentFile)return;
  try{
    const data=await apiFetch("/api/board/"+encodeURIComponent(currentFile));
    currentBoardData=data;
    if(data.mtime!==lastMtime){lastMtime=data.mtime;currentTasks=data.tasks||[];renderBoard(data);if(selectedTaskKey)refreshPanel();}
    pillState.lastUpdated="Updated "+new Date().toLocaleTimeString();
    setServerDown(false);
    checkOrchStatus();
  }catch(e){setServerDown(true);}
}
function startPolling(){if(pollTimer)clearInterval(pollTimer);lastMtime=0;if(!$("board").querySelector(".column"))showBoardSkeleton();poll();pollTimer=setInterval(poll,POLL_MS);}
function showBoardSkeleton(){
  const board=$("board");board.innerHTML="";
  for(let i=0;i<5;i++){
    const col=document.createElement("div");col.className="skeleton-col";
    const hdr=document.createElement("div");hdr.className="skeleton-hdr";
    const body=document.createElement("div");body.className="skeleton-body";
    [48,32,56].forEach((h,j)=>{
      const c=document.createElement("div");c.className="skeleton-card";
      c.style.height=h+"px";c.style.animationDelay=(i*.08+j*.04)+"s";
      body.appendChild(c);
    });
    col.appendChild(hdr);col.appendChild(body);board.appendChild(col);
  }
}

async function moveTask(file,taskId,newCol){
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({column:newCol})});lastMtime=0;showToast("Task #"+taskId+" → "+COL_LABELS[newCol]);}
  catch(e){showToast("Failed to move task #"+taskId,true);}
}
// Ticket #61: optimistic drag-drop. Relocate the dragged card into the target
// column immediately so the move feels instant, then PATCH in the background:
// on success reconcile with server truth via poll() (sorting may legitimately
// Ticket #64: insertion-point helpers ─────────────────────────────────────────
// showDropPlaceholder inserts (or moves) a thin coloured line in the col-body at
// the position the card would land if dropped now, computed by comparing clientY
// against the midpoint of each non-dragging card. clearDropPlaceholder removes all
// placeholders across the board (called on dragend and cross-column dragleave).
function showDropPlaceholder(body,clientY){
  if(!body)return;
  const col=body.closest(".column");
  let ph=body.querySelector(".drop-placeholder");
  if(!ph){
    ph=document.createElement("div");ph.className="drop-placeholder";
    if(col)ph.style.setProperty("--col-color",col._color||"#3b82f6");
  }
  const cards=[...body.querySelectorAll(".card:not(.dragging)")];
  let insertBefore=null;
  for(const card of cards){
    const r=card.getBoundingClientRect();
    if(clientY<r.top+r.height/2){insertBefore=card;break;}
  }
  if(ph.parentNode)ph.remove();
  if(insertBefore)body.insertBefore(ph,insertBefore);
  else body.appendChild(ph);
}
function clearDropPlaceholder(){
  document.querySelectorAll(".drop-placeholder").forEach(ph=>ph.remove());
}
// ──────────────────────────────────────────────────────────────────────────────

// re-order the card); on failure restore the node to its original column AND its
// original sibling position, then show the error toast. The pendingMove guard
// keeps an in-flight/mid-move poll from bouncing the card back before the server
// reflects the move (see renderBoard).
async function dropTask(file,taskId,newCol,cardEl,targetBody){
  const origParent=cardEl.parentNode;
  const origNext=cardEl.nextSibling;
  pendingMove={key:cardEl.dataset.key,targetCol:newCol};
  targetBody.appendChild(cardEl);
  syncColEmpty(targetBody);updateColCount(targetBody);
  if(origParent&&origParent!==targetBody){syncColEmpty(origParent);updateColCount(origParent);}
  try{
    await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({column:newCol})});
    lastMtime=0;showToast("Task #"+taskId+" → "+COL_LABELS[newCol]);poll();
  }catch(e){
    pendingMove=null;
    if(origParent){
      if(origNext&&origNext.parentNode===origParent)origParent.insertBefore(cardEl,origNext);
      else origParent.appendChild(cardEl);
      syncColEmpty(origParent);updateColCount(origParent);
    }
    syncColEmpty(targetBody);updateColCount(targetBody);
    showToast("Failed to move task #"+taskId,true);
  }
}
async function updateTaskOrder(file,taskId,order){
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({order})});lastMtime=0;}
  catch(e){showToast("Failed to reorder task #"+taskId,true);}
}

// Reorder within Ready: assigns dense order values across ALL boards, swaps the
// clicked card with its neighbour, then PATCHes both. Optimistic update plays the
// FLIP animation immediately so the reorder feels instant.
async function reorderReady(file,taskId,direction){
  const readyTasks=currentTasks
    .filter(t=>t._column==="ready")
    .slice().sort(compareReadyCards);
  const idx=readyTasks.findIndex(t=>String(t.id)===String(taskId)&&(t._board||file)===file);
  if(idx<0)return;
  const swapIdx=direction==="up"?idx-1:idx+1;
  if(swapIdx<0||swapIdx>=readyTasks.length)return;
  // Assign dense order values 0,1,2,... then swap the two positions.
  readyTasks.forEach((t,i)=>{t.order=i;});
  const tmp=readyTasks[idx].order;readyTasks[idx].order=readyTasks[swapIdx].order;readyTasks[swapIdx].order=tmp;
  // Optimistic: mutate currentTasks order values and re-render so FLIP plays now.
  readyTasks.forEach(t=>{
    const live=currentTasks.find(x=>taskKey(x)===taskKey(t));
    if(live)live.order=t.order;
  });
  if(currentBoardData)renderBoard(currentBoardData);
  // PATCH the two swapped tickets (may be on different boards).
  const a=readyTasks[idx],b=readyTasks[swapIdx];
  try{
    await Promise.all([
      apiFetch("/api/board/"+encodeURIComponent(a._board||file)+"/task/"+encodeURIComponent(String(a.id)),
        {method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({order:a.order})}),
      apiFetch("/api/board/"+encodeURIComponent(b._board||file)+"/task/"+encodeURIComponent(String(b.id)),
        {method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({order:b.order})}),
    ]);
    lastMtime=0;poll();
  }catch(e){showToast("Failed to reorder",true);lastMtime=0;poll();}
}

async function updateTaskModel(file,taskId,model){
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({model})});lastMtime=0;showToast("Task #"+taskId+" model → "+(model||"default"));}
  catch(e){showToast("Failed to set model for #"+taskId,true);}
}
async function updateTaskFields(file,taskId,fields){
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)});lastMtime=0;poll();}
  catch(e){showToast("Failed to save changes",true);throw e;}
}
async function deleteTask(file,taskId,title){
  if(!confirm("Delete task #"+taskId+": "+title+"?"))return;
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId),{method:"DELETE"});lastMtime=0;closePanel();showToast("Deleted #"+taskId);poll();}
  catch(e){showToast("Failed to delete",true);}
}
async function addComment(file,taskId,writer,message){
  try{await apiFetch("/api/board/"+encodeURIComponent(file)+"/task/"+encodeURIComponent(taskId)+"/comment",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({writer,message})});lastMtime=0;showToast("Comment added");poll();return true;}
  catch(e){showToast("Failed to add comment",true);return false;}
}

// ── Side Panel ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
function openPanel(task){
  selectedTaskKey=taskKey(task);
  $("sidePanel").classList.add("open");
  $("board").classList.add("panel-open");
  renderPanel(task);
  document.querySelectorAll(".card.selected").forEach(c=>c.classList.remove("selected"));
  const sel=document.querySelector('.card[data-key="'+CSS.escape(taskKey(task))+'"]');
  if(sel){
    sel.classList.add("selected");
    sel.classList.remove("unreviewed");
  }
  markReviewed(task);
}
function closePanel(){
  if(dockMode){undock();return;} // panel is showing the docked Create Task form
  hideToolResult(); // a bubble portaled to <body> must not outlive the panel
  stopLogPoll();logPoll.openKey=null;
  selectedTaskKey=null;
  $("sidePanel").classList.remove("open");
  $("board").classList.remove("panel-open");
  document.querySelectorAll(".card.selected").forEach(c=>c.classList.remove("selected"));
}
function refreshPanel(){
  const t=currentTasks.find(x=>taskKey(x)===selectedTaskKey);
  if(!t){closePanel();return;}
  // Preserve comment draft across re-renders
  const draftMsg=$("spCMsg"),draftWriter=$("spCWriter");
  const savedMsg=draftMsg?draftMsg.value:"",savedWriter=draftWriter?draftWriter.value:"";
  const wasFocused=document.activeElement===draftMsg||document.activeElement===draftWriter;
  const focusedId=wasFocused?document.activeElement.id:null;
  renderPanel(t);
  if(savedMsg||savedWriter){
    const newMsg=$("spCMsg"),newWriter=$("spCWriter");
    if(newMsg)newMsg.value=savedMsg;
    if(newWriter)newWriter.value=savedWriter;
    if(focusedId)$(focusedId).focus();
  }
}
// ── Live logs poll ──────────────────────────────────────────────────────
// A single active poll at a time. `logPoll.openKey` remembers which task's log
// the user had expanded so the inline view re-opens across panel re-renders
// (refreshPanel rebuilds the DOM every poll tick).
const logPoll={timer:null,openKey:null};
function stopLogPoll(){ if(logPoll.timer){clearInterval(logPoll.timer);logPoll.timer=null;} }
function renderLogTurns(turns,running){
  const box=$("spLog");
  if(!box)return;
  // innerHTML below detaches any shown result bubble; drop stale hover state so a
  // dangling node reference can't keep a ghost bubble "open".
  hideToolResult();
  let h='<div class="sp-log-status'+(running?' live':'')+'"><span class="sp-log-dot"></span><span>'+(running?'live — implementer agent':'stream ended')+'</span></div>';
  if(!turns||!turns.length){
    h+='<div class="sp-log-empty">'+(running?'Waiting for output…':'No output captured for this run.')+'</div>';
  }else{
    // Every turn is the implementer's own — thoughts plus the commands/reads it
    // ran. Tool results aren't echoed inline; hovering a chip with a result pops
    // the full output in a bubble, and clicking pins it open (see bindLogBox).
    turns.forEach(t=>{
      h+='<div class="sp-turn assistant"><div class="sp-turn-role">🤖 implementer';
      if(t.timestamp){
        const ts=new Date(t.timestamp);
        h+=' <span class="sp-turn-time">'+esc(ts.toLocaleTimeString())+'</span>';
      }
      h+='</div>';
      if(t.text) h+='<div class="sp-turn-text">'+esc(t.text)+'</div>';
      if(t.tools&&t.tools.length){
        h+='<div class="sp-turn-tools">';
        t.tools.forEach(tool=>{
          const hasRes=!!(tool.result&&tool.result.length);
          h+='<span class="sp-tool-chip'+(hasRes?' has-result':'')+'" tabindex="'+(hasRes?'0':'-1')+'">'
            +'<span class="sp-tool-name">'+esc(tool.name)+'</span>'
            +(tool.summary?'<span class="sp-tool-arg">'+esc(tool.summary)+'</span>':'')
            +(hasRes?'<span class="sp-tool-result"><span class="sp-tool-result-head">'+esc(tool.name)+' result</span><pre>'+esc(tool.result)+'</pre></span>':'')
          +'</span>';
        });
        h+='</div>';
      }
      h+='</div>';
    });
    // While the agent is still running, the newest turn we have is usually a
    // tool_use awaiting its result — show a live "working…" cue so the stream
    // doesn't look frozen between polls.
    if(running){
      const last=turns[turns.length-1];
      if(last&&last.tools&&last.tools.length)
        h+='<div class="sp-log-thinking"><span class="sp-log-dot"></span><span>working…</span></div>';
    }
  }
  // Preserve "user is reading scrollback" — only auto-scroll if already near bottom.
  const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
  box.innerHTML=h;
  if(nearBottom)box.scrollTop=box.scrollHeight;
}
// Result bubbles open on hover (with a short leave-grace so the mouse can cross
// the gap into the bubble) and pin open on click; click-away or a second click
// closes a pinned bubble. The bubble is lifted to fixed/viewport coords to the
// LEFT of the chip so it escapes the scrollable log container. One delegated
// listener set survives the 2s innerHTML re-renders. Keyboard focus shows the
// bubble instantly (a11y parity with hover).
const toolHover={chip:null,bub:null,pinned:false,hideTimer:null};
function placeToolResult(chip,bub){
  bub.classList.add("show");      // must be displayed to measure its width/height
  const r=chip.getBoundingClientRect();
  const w=bub.offsetWidth, gap=8;
  bub.style.position="fixed";
  // Prefer LEFT of the chip; flip to the right only if it would overflow the left edge.
  let left=r.left-gap-w;
  if(left<8) left=Math.min(r.right+gap, window.innerWidth-w-8);
  bub.style.left=Math.max(8,left)+"px";
  // Vertically: align the bubble's top with the chip, clamped into the viewport so a
  // tall result never spills off-screen (it scrolls internally, capped at 90vh by CSS).
  const h=Math.min(bub.offsetHeight,window.innerHeight-16);
  let top=r.top;
  if(top+h>window.innerHeight-8) top=window.innerHeight-h-8;
  bub.style.bottom="auto";bub.style.top=Math.max(8,top)+"px";
}
function clearToolTimers(){
  if(toolHover.hideTimer){clearTimeout(toolHover.hideTimer);toolHover.hideTimer=null;}
}
function hideToolResult(){
  clearToolTimers();
  // Return a portaled bubble to its chip so :scope > .sp-tool-result finds it
  // next time (and so it dies with the chip on the next innerHTML re-render).
  if(toolHover.bub){
    toolHover.bub.classList.remove("show");
    if(toolHover.chip&&toolHover.bub.parentNode!==toolHover.chip)
      toolHover.chip.appendChild(toolHover.bub);
  }
  toolHover.chip=null;toolHover.bub=null;toolHover.pinned=false;
}
function showToolResult(chip){
  // While the panel slides shut, chips sweep under the (stationary) cursor and
  // Chromium synthesizes mouseover — without this guard that resurrects a
  // bubble on <body> after the panel is gone.
  if(!$("sidePanel").classList.contains("open"))return;
  const bub=chip.querySelector(":scope > .sp-tool-result");
  if(!bub)return;
  if(toolHover.chip===chip){clearToolTimers();return;} // already open — just cancel any pending hide
  hideToolResult();
  toolHover.chip=chip;toolHover.bub=bub;
  // PORTAL: the side panel is CSS-transformed (.side-panel.open translateX),
  // which makes it the containing block for position:fixed descendants — the
  // bubble's viewport coords would resolve panel-relative and be clipped by the
  // panel's overflow:hidden (visible in the DOM, zero pixels on screen). Lift
  // the bubble to <body> while shown so fixed really means the viewport.
  document.body.appendChild(bub);
  // The bubble is outside the log box now, so box delegation can't keep it
  // alive — cancel/schedule the hide from the bubble itself.
  if(!bub._hoverBound){
    bub._hoverBound=true;
    bub.addEventListener("mouseenter",clearToolTimers);
    bub.addEventListener("mouseleave",()=>{if(!toolHover.pinned)scheduleHideToolResult();});
  }
  placeToolResult(chip,bub);
}
// Leave-grace: the bubble sits ~8px away from the chip, so the mouse crosses a
// dead zone on the way over. Hide on a short delay, cancelled if the mouse
// arrives on the bubble (a DOM child of the chip) or back on the chip.
function scheduleHideToolResult(){
  clearToolTimers();
  toolHover.hideTimer=setTimeout(()=>{toolHover.hideTimer=null;hideToolResult();},300);
}
// Click-away: close the open bubble when clicking anywhere outside it or its chip.
document.addEventListener("click",e=>{
  if(!toolHover.bub)return;
  if(toolHover.chip&&toolHover.chip.contains(e.target))return; // chip click handled by bindLogBox
  if(toolHover.bub.contains(e.target))return;                  // click inside bubble — keep open
  hideToolResult();
},true);
function bindLogBox(box){
  if(box._resultBound)return; box._resultBound=true;
  // Hover over a chip (or its bubble, a DOM child) → show; cancel pending hide.
  box.addEventListener("mouseover",e=>{
    const chip=e.target.closest&&e.target.closest(".sp-tool-chip.has-result");
    if(!chip)return;
    if(chip===toolHover.chip){clearToolTimers();return;}
    if(toolHover.pinned)return; // don't steal a clicked-open bubble on hover
    showToolResult(chip);
  });
  box.addEventListener("mouseout",e=>{
    if(toolHover.pinned||!toolHover.chip)return;
    const from=e.target.closest&&e.target.closest(".sp-tool-chip.has-result");
    if(from!==toolHover.chip)return;
    const to=e.relatedTarget;
    if(to&&toolHover.chip.contains(to))return; // still within chip/bubble
    scheduleHideToolResult();
  });
  // Click pins the bubble open so it survives the mouse leaving; a second click
  // (or click-away, above) closes it. NOTE: mousedown focuses the chip and
  // focusin may have just opened the bubble — pin rather than toggle-close in
  // that case, or the click instantly closes what the focus opened.
  box.addEventListener("click",e=>{
    const chip=e.target.closest&&e.target.closest(".sp-tool-chip.has-result");
    if(!chip)return;
    if(e.target.closest&&e.target.closest(".sp-tool-result"))return; // clicks inside the bubble never toggle
    if(toolHover.chip===chip){
      if(toolHover.pinned){hideToolResult();}
      else{toolHover.pinned=true;clearToolTimers();}
      return;
    }
    showToolResult(chip);
    toolHover.pinned=true;
  });
  // Keyboard focus shows the result immediately (a11y).
  box.addEventListener("focusin",e=>{
    const chip=e.target.closest&&e.target.closest(".sp-tool-chip.has-result");
    if(!chip||chip===toolHover.chip||toolHover.pinned)return;
    showToolResult(chip);
  });
  box.addEventListener("focusout",e=>{
    const chip=e.target.closest&&e.target.closest(".sp-tool-chip.has-result");
    if(chip&&chip===toolHover.chip&&!toolHover.pinned)scheduleHideToolResult();
  });
}
async function pollLog(board,taskId){
  if(!$("spLog"))return; // panel re-rendered/closed without our log open
  try{
    const data=await apiFetch("/api/board/"+encodeURIComponent(board)+"/task/"+encodeURIComponent(taskId)+"/log?n=25");
    if(logPoll.openKey===null||!$("spLog"))return; // closed mid-flight
    renderLogTurns(data.turns,data.running);
    if(!data.running)stopLogPoll(); // agent finished — stop hammering the endpoint
  }catch(e){ /* transient; next tick retries */ }
}
function startLogPoll(board,taskId){
  stopLogPoll();
  pollLog(board,taskId);
  logPoll.timer=setInterval(()=>pollLog(board,taskId),2000);
}
function bindLogToggle(task,srcFile){
  const btn=$("spLogToggle"),box=$("spLog");
  if(!btn||!box)return;
  const key=taskKey(task);
  function open(){
    btn.classList.add("open");box.style.display="block";logPoll.openKey=key;
    box.innerHTML='<div class="sp-log-empty">Loading…</div>';
    bindLogBox(box);
    startLogPoll(srcFile,String(task.id));
  }
  function close(){
    btn.classList.remove("open");box.style.display="none";logPoll.openKey=null;stopLogPoll();
  }
  btn.addEventListener("click",()=>{ logPoll.openKey===key?close():open(); });
  // Re-open automatically if this task's log was open before the re-render.
  if(logPoll.openKey===key)open();
}

function startTitleEdit(task,srcFile){
  const titleEl=$("spTitle");
  if(titleEl.querySelector("input"))return; // already editing
  const current=task.title;
  const inp=document.createElement("input");
  inp.type="text";inp.value=current;inp.className="sp-title-input";
  inp.setAttribute("aria-label","Edit ticket title");
  const save=async()=>{
    const val=inp.value.trim();
    if(!val){showToast("Title cannot be empty",true);inp.focus();return;}
    if(val===current){restore();return;}
    try{
      await updateTaskFields(srcFile,String(task.id),{title:val});
      task.title=val;
      restore();
    }catch(e){inp.focus();}
  };
  const restore=()=>{titleEl.textContent="#"+task.id+" "+task.title;};
  titleEl.textContent="";
  titleEl.appendChild(inp);
  inp.focus();inp.select();
  inp.addEventListener("keydown",e=>{
    if(e.key==="Enter"){e.preventDefault();save();}
    else if(e.key==="Escape"){restore();}
  });
  inp.addEventListener("blur",save);
}

function startDetailEdit(task,srcFile){
  const view=$("spDetailView");
  const field=$("spDetailField");
  if(!view||!field)return;
  if(field.querySelector("textarea"))return; // already editing
  const current=task.detail||"";
  const ta=document.createElement("textarea");
  ta.className="form-input form-textarea sp-detail-edit";
  ta.value=current;ta.rows=5;
  ta.setAttribute("aria-label","Edit description");
  const btnRow=document.createElement("div");
  btnRow.className="sp-detail-edit-actions";
  const saveBtn=document.createElement("button");saveBtn.type="button";saveBtn.className="btn btn-create";saveBtn.style.fontSize="12px";saveBtn.style.padding="4px 12px";saveBtn.textContent="Save";
  const cancelBtn=document.createElement("button");cancelBtn.type="button";cancelBtn.className="btn btn-cancel";cancelBtn.style.fontSize="12px";cancelBtn.style.padding="4px 12px";cancelBtn.textContent="Cancel";
  btnRow.appendChild(saveBtn);btnRow.appendChild(cancelBtn);
  const restore=()=>{
    ta.remove();btnRow.remove();
    view.style.display="";
    const editBtn=field.querySelector(".sp-edit-detail-btn");
    if(editBtn)editBtn.style.display="";
  };
  const save=async()=>{
    const val=ta.value.trim();
    if(val===current){restore();return;}
    saveBtn.disabled=true;cancelBtn.disabled=true;
    try{
      await updateTaskFields(srcFile,String(task.id),{detail:val});
      task.detail=val||undefined;
      // Update the view text in place
      if(val){view.textContent=val;view.className="sp-detail";view.style.cssText="";}
      else{view.textContent="No description";view.className="sp-detail sp-detail-empty";view.style.color="var(--text-muted)";view.style.fontStyle="italic";}
      restore();
    }catch(e){saveBtn.disabled=false;cancelBtn.disabled=false;}
  };
  view.style.display="none";
  const editBtn=field.querySelector(".sp-edit-detail-btn");
  if(editBtn)editBtn.style.display="none";
  view.parentNode.insertBefore(ta,view.nextSibling);
  view.parentNode.insertBefore(btnRow,ta.nextSibling);
  ta.focus();
  saveBtn.addEventListener("click",save);
  cancelBtn.addEventListener("click",restore);
  ta.addEventListener("keydown",e=>{
    if(e.key==="Escape"){restore();}
    if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){save();}
  });
}

function renderPanel(task){
  stopLogPoll(); // clear any prior poll; bindLogToggle re-arms it if still open
  hideToolResult(); // a bubble portaled to <body> must not outlive its chip
  $("spTitle").textContent="#"+task.id+" "+task.title;
  const body=$("spBody");body.innerHTML="";

  // Details section
  let html='<div class="sp-section"><div class="sp-section-title">Details</div>';
  html+='<div class="sp-field"><div class="sp-field-label">Status</div><select class="sp-status-select" id="spStatusSel">';
  COL_KEYS.forEach(k=>{html+='<option value="'+k+'"'+(k===task._column?' selected':'')+'>'+COL_LABELS[k]+'</option>';});
  html+='</select></div>';
  html+='<div class="sp-field"><div class="sp-field-label">Model</div><select class="sp-status-select" id="spModelSel">';
  MODEL_OPTIONS.forEach(m=>{html+='<option value="'+esc(m.value)+'"'+((task.model||"")===m.value?' selected':'')+'>'+esc(m.label)+'</option>';});
  html+='</select></div>';
  if(task.createdAt) html+='<div class="sp-field"><div class="sp-field-label">Created</div><div class="sp-field-value">'+esc(new Date(task.createdAt).toLocaleString())+'</div></div>';
  if(task._filePath) html+='<div class="sp-field"><div class="sp-field-label">File Path</div><div class="sp-field-value sp-session-row"><button type="button" class="sp-copy-btn" id="spCopyPath" title="Copy ticket file path to clipboard">📋 Copy path</button></div></div>';
  if(task.claudeSessionId) html+='<div class="sp-field"><div class="sp-field-label">Claude Session</div><div class="sp-field-value sp-session-row"><code class="sp-session-id">'+esc(task.claudeSessionId)+'</code><button type="button" class="sp-copy-btn" id="spCopySession" title="Copy resume command">Copy resume cmd</button></div></div>';
  html+='<div class="sp-field" id="spDetailField"><div class="sp-field-label">Description <button type="button" class="sp-copy-btn sp-edit-detail-btn" id="spEditDetailBtn" title="Edit description" style="padding:2px 6px;margin-left:4px;">&#x270E;</button></div>'+(task.detail?'<div class="sp-detail" id="spDetailView">'+esc(task.detail)+'</div>':'<div class="sp-detail sp-detail-empty" id="spDetailView" style="color:var(--text-muted);font-style:italic;">No description</div>')+'</div>';

  const deps=task.dependsOn?(Array.isArray(task.dependsOn)?task.dependsOn:[task.dependsOn]):[];
  if(deps.length||task.optional){
    html+='<div class="sp-field"><div class="sp-field-label">Tags</div><div class="sp-tags">';
    deps.forEach(d=>{html+='<span class="sp-tag dep">↳ needs #'+d+'</span>';});
    if(task.optional) html+='<span class="sp-tag opt">⚑ optional</span>';
    html+='</div></div>';
  }
  html+='</div>';

  // Spec / plan section — surfaces any associated design docs. Each is an
  // openable new tab (link to the raw markdown) plus an inline, lazy-loaded
  // preview rendered from markdown.
  const specs=task._specs||[];
  if(specs.length){
    html+='<div class="sp-section"><div class="sp-section-title">Spec ('+specs.length+')</div>';
    specs.forEach((s,i)=>{
      const kind=(s.kind||"doc").toLowerCase();
      const url="/api/doc/"+s.path.split("/").map(encodeURIComponent).join("/");
      html+='<div class="sp-spec" data-spec-idx="'+i+'" data-spec-url="'+esc(url)+'">'
        +'<div class="sp-spec-head">'
          +'<span class="sp-spec-caret">▶</span>'
          +'<span class="sp-spec-kind '+esc(kind)+'">'+esc(kind)+'</span>'
          +'<span class="sp-spec-title" title="'+esc(s.path)+'">'+esc(s.title||s.path)+'</span>'
          +'<a class="sp-spec-open" href="'+esc(url)+'" target="_blank" rel="noopener" title="Open in a new tab">Open ↗</a>'
        +'</div>'
        +'<div class="sp-spec-body"><div class="sp-spec-loading">Loading…</div></div>'
      +'</div>';
    });
    html+='</div>';
  }

  // Comments section
  const comments=task.comments||[];
  html+='<div class="sp-section"><div class="sp-section-title">Comments ('+comments.length+')</div>';
  comments.slice().reverse().forEach(c=>{
    const ts=c.timestamp?new Date(c.timestamp).toLocaleString():"";
    html+='<div class="sp-comment"><div class="sp-comment-head"><span class="sp-comment-writer">'+esc(c.writer)+'</span><span class="sp-comment-time">'+esc(ts)+'</span></div><div class="sp-comment-msg sp-md">'+renderMarkdown(c.message)+'</div></div>';
  });
  html+='<div class="sp-comment-form"><input type="text" id="spCWriter" placeholder="Your name"><textarea id="spCMsg" placeholder="Write a comment..."></textarea><button type="button" id="spCPost">Post Comment</button></div></div>';

  // Logs section — present whenever a sub-agent has been dispatched for this
  // ticket (it has an orchestrator run-log), and stays available after the
  // ticket is done so you can read back what the implementer did. Expands
  // inline; polls only while the agent is still running ("live" badge).
  // The orchestrator block is cleared on reap (clear_marker), so also accept
  // the top-level runLogFile pointer (survives the clear; ticket #74) and
  // completedLog (turns saved at completion; ticket #58) as signals.
  const hasLog=(task.orchestrator&&task.orchestrator.logFile)||task.runLogFile||
               (Array.isArray(task.completedLog)&&task.completedLog.length>0);
  if(hasLog){
    const agentLive=task.orchestrator&&task.orchestrator.state==="dispatched"&&task._column==="in_progress";
    html+='<div class="sp-section"><div class="sp-section-title">Logs</div>';
    html+='<button type="button" class="sp-log-toggle" id="spLogToggle"><span class="sp-log-caret">▶</span><span>📡 Logs</span><span class="sp-log-spacer"></span>'+(agentLive?'<span class="sp-log-livebadge"><span class="sp-log-dot"></span>live</span>':'')+'</button>';
    html+='<div class="sp-log" id="spLog" style="display:none;"></div>';
    html+='</div>';
  }

  // History section
  const history=task.history||[];
  html+='<div class="sp-section"><div class="sp-section-title">History ('+history.length+')</div>';
  if(!history.length) html+='<div style="font-size:11px;color:#475569;">No history yet.</div>';
  history.slice().reverse().forEach(h=>{
    const ts=h.timestamp?new Date(h.timestamp).toLocaleString():"";
    if(h.action==="status_change") html+='<div class="sp-history-item">Status changed: <strong>'+esc(h.from)+'</strong> → <strong>'+esc(h.to)+'</strong><br><span class="sp-history-time">'+esc(ts)+'</span></div>';
    else html+='<div class="sp-history-item">'+esc(h.action)+'<br><span class="sp-history-time">'+esc(ts)+'</span></div>';
  });
  html+='</div>';

  // Actions
  html+='<div class="sp-actions"><button class="sp-del-btn" id="spDelBtn">Delete Task</button></div>';
  body.innerHTML=html;

  // Bind events
  const srcFile=task._board||currentFile;
  $("spStatusSel").addEventListener("change",e=>{moveTask(srcFile,String(task.id),e.target.value);});
  $("spModelSel").addEventListener("change",e=>{updateTaskModel(srcFile,String(task.id),e.target.value);});
  $("spCPost").addEventListener("click",async ()=>{
    const btn=$("spCPost"),area=$("spCMsg");
    if(btn.disabled)return;
    const msg=area.value.trim();if(!msg)return;
    btn.disabled=true;area.disabled=true;
    const ok=await addComment(srcFile,String(task.id),$("spCWriter").value.trim(),msg);
    if(ok)area.value="";
    btn.disabled=false;area.disabled=false;
  });
  $("spDelBtn").addEventListener("click",()=>{deleteTask(srcFile,String(task.id),task.title);});
  const copyPathBtn=$("spCopyPath");
  if(copyPathBtn) copyPathBtn.addEventListener("click",()=>{
    navigator.clipboard.writeText(task._filePath)
      .then(()=>showToast("Copied file path"))
      .catch(()=>showToast("Copy failed",true));
  });
  const copyBtn=$("spCopySession");
  if(copyBtn) copyBtn.addEventListener("click",()=>{
    const dir=task.claudeSessionDir;
    const cmd=dir?"cd '"+dir+"'; claude --resume "+task.claudeSessionId:"claude --resume "+task.claudeSessionId;
    navigator.clipboard.writeText(cmd)
      .then(()=>showToast("Copied resume command"))
      .catch(()=>showToast("Copy failed",true));
  });
  bindLogToggle(task,srcFile);

  // Title edit button
  const titleEditBtn=$("spTitleEditBtn");
  if(titleEditBtn) titleEditBtn.addEventListener("click",()=>startTitleEdit(task,srcFile));

  // Description inline edit
  const editDetailBtn=$("spEditDetailBtn");
  if(editDetailBtn) editDetailBtn.addEventListener("click",()=>startDetailEdit(task,srcFile));

  // Spec rows: toggle inline preview; fetch + render markdown on first open.
  // The "Open ↗" link is a normal anchor (new tab) — stop it bubbling so the
  // click doesn't also toggle the row.
  body.querySelectorAll(".sp-spec").forEach(row=>{
    const head=row.querySelector(".sp-spec-head");
    const bodyEl=row.querySelector(".sp-spec-body");
    head.querySelector(".sp-spec-open").addEventListener("click",e=>e.stopPropagation());
    head.addEventListener("click",async ()=>{
      const opening=!row.classList.contains("open");
      row.classList.toggle("open");
      if(opening && !row.dataset.loaded){
        row.dataset.loaded="1";
        try{
          const res=await fetch(row.dataset.specUrl);
          if(!res.ok)throw new Error("HTTP "+res.status);
          const md=await res.text();
          bodyEl.innerHTML='<div class="sp-md">'+renderMarkdown(md)+'</div>';
        }catch(err){
          row.dataset.loaded=""; // allow retry on next open
          bodyEl.innerHTML='<div class="sp-spec-loading">Failed to load spec.</div>';
        }
      }
    });
  });
}

// Minimal, dependency-free markdown → HTML for spec previews. Handles the
// constructs design docs actually use (headings, lists, fenced/inline code,
// bold, links, hr, paragraphs). esc()'d first so raw markdown can't inject HTML.
function renderMarkdown(src){
  const lines=String(src).replace(/\r\n/g,"\n").split("\n");
  let out="",inUl=false,inOl=false,inCode=false;
  const closeLists=()=>{if(inUl){out+="</ul>";inUl=false;}if(inOl){out+="</ol>";inOl=false;}};
  const inline=s=>{
    s=esc(s);
    s=s.replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>");
    s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(m,txt,url)=>'<a href="'+safeUrl(url)+'" target="_blank" rel="noopener">'+txt+'</a>');
    return s;
  };
  for(const raw of lines){
    const fence=raw.match(/^\s*```/);
    if(fence){
      if(inCode){out+="</code></pre>";inCode=false;}
      else{closeLists();out+="<pre><code>";inCode=true;}
      continue;
    }
    if(inCode){out+=esc(raw)+"\n";continue;}
    const line=raw.replace(/\s+$/,"");
    if(!line.trim()){closeLists();continue;}
    let m;
    if((m=line.match(/^(#{1,6})\s+(.*)$/))){
      closeLists();const lvl=Math.min(m[1].length,3);
      out+="<h"+lvl+">"+inline(m[2].replace(/#+\s*$/,""))+"</h"+lvl+">";continue;
    }
    if(/^\s*(?:---|===|\*\*\*)\s*$/.test(line)){closeLists();out+="<hr>";continue;}
    if((m=line.match(/^\s*[-*+]\s+(.*)$/))){
      if(inOl){out+="</ol>";inOl=false;}
      if(!inUl){out+="<ul>";inUl=true;}
      out+="<li>"+inline(m[1])+"</li>";continue;
    }
    if((m=line.match(/^\s*\d+\.\s+(.*)$/))){
      if(inUl){out+="</ul>";inUl=false;}
      if(!inOl){out+="<ol>";inOl=true;}
      out+="<li>"+inline(m[1])+"</li>";continue;
    }
    closeLists();out+="<p>"+inline(line)+"</p>";
  }
  if(inCode)out+="</code></pre>";
  closeLists();
  return out;
}
$("spClose").addEventListener("click",closePanel);

// ── Board Render ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
// Ticket #62: keyed reconciliation. Instead of tearing the board down with
// board.innerHTML="" every poll, diff incoming tasks against the existing DOM by
// data-key: reuse the persistent column containers, create only new cards, remove
// only departed cards, MOVE existing card nodes when their _column changed, and
// update reused cards in place. This preserves each .col-body's scrollTop, keeps
// hover/selection/drag state, binds listeners once per node, and is the foundation
// the FLIP/enter-exit animations (#63/#64) build on.

// ── Ticket #63: card motion (FLIP + enter/exit) ───────────────────────────────
// FLIP = First, Last, Invert, Play. renderBoard captures each card's rect BEFORE it
// mutates the DOM (First); after the keyed diff settles we measure the new rect
// (Last), apply an inverted transform so the card visually starts where it was, then
// transition that transform back to identity so it slides to its new slot. We only
// animate "same board, incremental update" renders — first paint and board switches
// rebuild everything and are left to paint instantly. Honors prefers-reduced-motion
// (motionOn stays false → every helper is a clean no-op). Transforms are always
// reset to a clean state at the start of each FLIP, so a mid-slide node never leaves
// stale geometry behind to desync the #62 keyed diff on the next poll.
let flipReady=false;     // false until the first board has painted
let flipBoard=null;      // identity of the board we last animated
let motionOn=false;      // true only on an incremental, same-board, motion-allowed render
const _reduceMQ=window.matchMedia("(prefers-reduced-motion: reduce)");
function reduceMotion(){return _reduceMQ.matches;}

// Cards that should never be FLIPped: the one under active drag (its position is the
// user's pointer, not ours), and any card mid-exit (handled by its own animation).
function flipSkip(card){return card.classList.contains("dragging")||card===dragEl||card._exiting;}

// First: snapshot the viewport rect (and column) of every animatable card. Reading
// getBoundingClientRect picks up any in-flight transform, so an interrupted slide is
// re-FLIPped from where it visually is — motion stays interruptible.
function captureRects(){
  const rects={};
  $("board").querySelectorAll(".card").forEach(c=>{
    if(flipSkip(c))return;
    const r=c.getBoundingClientRect();
    const col=c.closest(".column");
    rects[c.dataset.key]={left:r.left,top:r.top,width:r.width,height:r.height,col:col?col.dataset.col:null};
  });
  return rects;
}

// Cross-column travel can't be done with an in-flow translate: .col-body scrolls, so
// it clips overflow, and a card translated toward another column would be clipped to
// nothing for the first half of the slide. Instead fly a fixed-position ghost (on
// <body>, outside any clipper) from First to Last while the real card waits invisibly
// in its destination slot, then swap back. Self-cleaning and interrupt-safe.
function flipGhost(card,first,last){
  const ghost=card.cloneNode(true);
  ghost.classList.remove("selected","unreviewed","flipping","card-enter","dragging");
  ghost.removeAttribute("draggable");
  ghost.style.cssText="position:fixed;margin:0;left:"+first.left+"px;top:"+first.top+"px;width:"+first.width+"px;height:"+first.height+"px;z-index:90;pointer-events:none;will-change:transform;transition:transform 180ms ease-out;";
  ghost.style.setProperty("--col-color",card._color||"");
  document.body.appendChild(ghost);
  card.style.opacity="0";
  card._ghost=ghost;
  ghost.getBoundingClientRect(); // commit start position before playing
  ghost.style.transform="translate("+(last.left-first.left)+"px,"+(last.top-first.top)+"px)";
  const done=()=>{ if(card._ghost===ghost){card._ghost=null;card.style.opacity="";} ghost.remove(); };
  ghost.addEventListener("transitionend",e=>{if(e.propertyName==="transform")done();},{once:true});
  setTimeout(done,400); // safety net if transitionend is missed
}

// Reset a card to a clean, untransformed, listener-free state. Called before each
// measurement and on slide completion so geometry is never left mid-transform.
function clearFlip(card){
  if(card._flipEnd){card.removeEventListener("transitionend",card._flipEnd);card._flipEnd=null;}
  if(card._ghost){card._ghost.remove();card._ghost=null;card.style.opacity="";}
  card.classList.remove("flipping"); // removing this first makes transform clears instant
  card.style.transform="";           // (base .card transition has no transform, so no slide)
  card.style.transition="";          // restore base transition — never leave it pinned to none
}

// Last + Invert + Play. firstRects holds the pre-mutation snapshot.
function playFlip(firstRects){
  if(reduceMotion())return;
  const inFlow=[],cross=[];
  $("board").querySelectorAll(".card").forEach(card=>{
    if(flipSkip(card))return;
    const first=firstRects[card.dataset.key];
    if(!first)return; // brand-new card → handled by its enter animation
    clearFlip(card); // wipe any in-flight slide before measuring the true layout rect
    const last=card.getBoundingClientRect();
    const dx=first.left-last.left, dy=first.top-last.top;
    if(Math.abs(dx)<1&&Math.abs(dy)<1)return; // didn't move (clearFlip already reset it)
    const nowCol=card.closest(".column");
    if(first.col&&nowCol&&first.col!==nowCol.dataset.col){
      card.style.opacity="0"; // hide at destination until the ghost takes over
      cross.push({card,first,last});
    }else{
      // Invert: no .flipping class yet, so this applies instantly (base transition
      // has no transform), seating the card at its old spot before we Play.
      card.style.transform="translate("+dx+"px,"+dy+"px)";
      inFlow.push(card);
    }
  });
  if(!inFlow.length&&!cross.length)return;
  $("board").offsetHeight; // force a reflow so the inverted positions are committed
  inFlow.forEach(card=>{
    card.classList.add("flipping");
    card.style.transition=""; // fall back to the .flipping CSS transition (transform 180ms)
    card.style.transform="";  // Play → slides from inverted offset to identity
    card._flipEnd=e=>{if(e.propertyName==="transform")clearFlip(card);};
    card.addEventListener("transitionend",card._flipEnd);
  });
  cross.forEach(({card,first,last})=>flipGhost(card,first,last));
}

// Exit: fade/scale a departed card out, THEN remove it. The node is pinned to its
// current viewport spot and lifted onto <body> immediately so it leaves the column
// flow at once (siblings collapse now and FLIP-slide to fill the gap) and so it can
// never be matched by the #62 keyed diff while it animates out.
function animateExit(card){
  if(card._exiting)return;
  if(!motionOn){card.remove();return;}
  const r=card.getBoundingClientRect();
  card._exiting=true;
  clearFlip(card);
  card.classList.remove("selected");
  card.style.cssText+=";position:fixed;left:"+r.left+"px;top:"+r.top+"px;width:"+r.width+"px;margin:0;z-index:50;pointer-events:none;";
  document.body.appendChild(card);
  card.classList.add("card-exit");
  const done=()=>card.remove();
  card.addEventListener("animationend",e=>{if(e.animationName==="cardExit")done();},{once:true});
  setTimeout(done,400); // safety net
}

function renderBoard(data){
  const board=$("board");
  // Ticket #65: clear skeleton/placeholder nodes before keyed reconciliation runs.
  Array.from(board.children).forEach(c=>{if(!c.classList.contains("column"))c.remove();});
  // Ticket #63: only animate incremental updates of the SAME board. First paint and
  // board switches rebuild every card, so motion stays off (motionOn=false ⇒ no FLIP,
  // no enter, instant exits). reduceMotion() also forces it off for a graceful no-op.
  const boardId=(data.filename||"")+"|"+((data.columns||[]).length);
  motionOn = flipReady && boardId===flipBoard && !reduceMotion();
  flipReady=true; flipBoard=boardId;
  const firstRects = motionOn ? captureRects() : {};
  const buckets={};COL_KEYS.forEach(k=>{buckets[k]=[];});
  (data.tasks||[]).forEach(t=>{
    let k=t._column||"todo";
    // Ticket #61: honor an in-flight optimistic move. If the server data already
    // reflects the move, release the guard; otherwise hold the card in its target
    // column so this reconciling/mid-move render doesn't bounce it back.
    if(pendingMove&&taskKey(t)===pendingMove.key){
      if(k===pendingMove.targetCol)pendingMove=null;
      else k=pendingMove.targetCol;
    }
    if(!buckets[k])buckets[k]=[];buckets[k].push(t);
  });

  // Snapshot each column's scroll offset so it survives the reconcile (#3 made the
  // col-body scrollable; rebuilding it used to snap scrollTop back to 0 each poll).
  const savedScroll={};
  board.querySelectorAll(".col-body").forEach(b=>{const c=b.closest(".column");if(c)savedScroll[c.dataset.col]=b.scrollTop;});

  // Index existing columns by key, and every existing card board-wide by data-key,
  // so a card whose _column changed can be MOVED into its new column (not rebuilt).
  const existingCols={};
  board.querySelectorAll(".column").forEach(c=>{existingCols[c.dataset.col]=c;});
  const existingCards={};
  board.querySelectorAll(".card").forEach(c=>{existingCards[c.dataset.key]=c;});

  const liveCols={};
  data.columns.forEach((col,colIdx)=>{
    const tasks=(buckets[col.key]||[]).slice().sort(col.key==="ready"?compareReadyCards:compareCards);
    let colEl=existingCols[col.key],hdr,body;
    if(!colEl){
      colEl=document.createElement("div");colEl.className="column";colEl.dataset.col=col.key;colEl.style.setProperty("--col-color",col.color);colEl._color=col.color;
      hdr=document.createElement("div");hdr.className="col-header";hdr.innerHTML=col.label+' <span class="col-count">'+tasks.length+"</span>";colEl._label=col.label;colEl.appendChild(hdr);
      body=document.createElement("div");body.className="col-body";colEl.appendChild(body);
      // Bound once on the persistent column node (no per-poll rebinding).
      colEl.addEventListener("dragover",e=>{
        e.preventDefault();
        colEl.classList.add("drag-over");
        showDropPlaceholder(colEl.querySelector(".col-body"),e.clientY);
      });
      colEl.addEventListener("dragleave",e=>{
        if(colEl.contains(e.relatedTarget))return;
        colEl.classList.remove("drag-over");
        const ph=colEl.querySelector(".drop-placeholder");if(ph)ph.remove();
      });
      colEl.addEventListener("drop",e=>{
        e.preventDefault();
        colEl.classList.remove("drag-over");
        const ph=colEl.querySelector(".drop-placeholder");if(ph)ph.remove();
        if(dragTaskId&&dragFile&&dragEl)dropTask(dragFile,dragTaskId,col.key,dragEl,colEl.querySelector(".col-body"));
      });
    }else{
      hdr=colEl.querySelector(".col-header");body=colEl.querySelector(".col-body");
      if(colEl._color!==col.color){colEl._color=col.color;colEl.style.setProperty("--col-color",col.color);}
      if(colEl._label!==col.label){colEl._label=col.label;hdr.innerHTML=col.label+' <span class="col-count">'+tasks.length+"</span>";}
      else{const b=colEl.querySelector(".col-count");const n=String(tasks.length);if(b.textContent!==n){b.textContent=n;popBadge(b);}}
    }
    liveCols[col.key]=colEl;
    // Place the column at its data-defined position (reuses the node when already there).
    if(board.children[colIdx]!==colEl)board.insertBefore(colEl,board.children[colIdx]||null);

    // Build the desired ordered card list: create new cards, update reused ones in
    // place, and pull each reused card out of existingCards so leftovers = departed.
    const desired=[];
    tasks.forEach(t=>{
      const key=taskKey(t);
      let card=existingCards[key];
      if(card){updateCard(card,t,col.color,data.filename);delete existingCards[key];}
      else card=makeCard(t,col.color,data.filename);
      desired.push(card);
    });
    syncColBody(body,desired);
  });

  // Drop columns no longer present in the board data.
  board.querySelectorAll(".column").forEach(c=>{if(!liveCols[c.dataset.col])c.remove();});
  // Any card still in existingCards exists in no column anymore — it has truly
  // departed (not just moved columns). Animate it out (Ticket #63), still in its
  // col-body so its exit rect is valid. animateExit detaches it from the flow at
  // once so survivors collapse and FLIP-slide into the gap below.
  Object.keys(existingCards).forEach(k=>animateExit(existingCards[k]));

  // Restore scroll offsets last, after all moves/removals have settled.
  data.columns.forEach(col=>{
    if(savedScroll[col.key]!=null&&liveCols[col.key]){
      const body=liveCols[col.key].querySelector(".col-body");
      if(body)body.scrollTop=savedScroll[col.key];
    }
  });

  // Ticket #63: with the keyed diff settled and scroll restored, play the FLIP slides
  // (Last/Invert/Play) for every card that changed position this render.
  playFlip(firstRects);
  // Ticket #67: refresh the "Mark all read" button state after the board re-renders.
  updateMarkAllReadBtn();
}

// Ticket #62: reconcile a column body's children to exactly `desired` (ordered card
// nodes), moving existing nodes into place (including in from another column) rather
// than recreating them, and managing the "— empty —" placeholder. Minimal churn: a
// node already in the right slot is left untouched.
function syncColBody(body,desired){
  // Position the desired cards in order (pulling a moved card in from another column
  // as needed). Ticket #63: do NOT remove the non-desired cards left over here — each
  // is either moving to a column rendered later (it'll be re-parented then) or truly
  // departed (renderBoard's leftover pass animates it out, and needs its live rect to
  // do so). Removing them here would teleport survivors and break the exit animation.
  const empty=body.querySelector(".col-empty");if(empty)empty.remove();
  desired.forEach((node,i)=>{
    if(body.children[i]!==node)body.insertBefore(node,body.children[i]||null);
  });
  if(!desired.length){
    const e=document.createElement("div");e.className="col-empty";e.textContent="— empty —";body.appendChild(e);
  }
}

// Ticket #61 helpers: keep a column's count badge and "— empty —" placeholder in
// sync after an optimistic card relocation (between renders).
function updateColCount(body){
  const col=body.closest(".column");if(!col)return;
  const badge=col.querySelector(".col-count");if(!badge)return;
  const n=String(body.querySelectorAll(".card").length);
  if(badge.textContent!==n){badge.textContent=n;popBadge(badge);}
}
function popBadge(badge){
  badge.classList.remove("pop");badge.offsetWidth;badge.classList.add("pop");
  badge.addEventListener("animationend",()=>badge.classList.remove("pop"),{once:true});
}
function syncColEmpty(body){
  const hasCard=body.querySelector(".card");
  const empty=body.querySelector(".col-empty");
  if(hasCard){if(empty)empty.remove();}
  else if(!empty){const e=document.createElement("div");e.className="col-empty";e.textContent="— empty —";body.appendChild(e);}
}

function makeCard(task,color,filename){
  const done=task._column==="done";
  const el=document.createElement("div");
  el.className="card"+(done?" done":"")+(taskKey(task)===selectedTaskKey?" selected":"")+(isUnreviewed(task)?" unreviewed":"");
  el.style.setProperty("--col-color",color);el._color=color;
  el.draggable=true;
  el.dataset.id=task.id;
  el.dataset.key=taskKey(task);
  // Ticket #62: listeners are bound ONCE here and read el._task/el._filename so the
  // node can be reused across polls (updateCard refreshes these) without rebinding,
  // and a click/drag always acts on the latest task data rather than a stale closure.
  el._task=task;el._filename=filename;

  el.addEventListener("dragstart",e=>{dragTaskId=String(el._task.id);dragFile=el._task._board||el._filename;dragEl=el;el.classList.add("dragging");$("board").classList.add("drag-active");e.dataTransfer.effectAllowed="move";});
  el.addEventListener("dragend",()=>{el.classList.remove("dragging");dragEl=null;$("board").classList.remove("drag-active");clearDropPlaceholder();document.querySelectorAll(".column.drag-over").forEach(c=>c.classList.remove("drag-over"));});
  el.addEventListener("click",e=>{if(e.defaultPrevented)return;openPanel(el._task);});

  const titleEl=document.createElement("div");titleEl.className="card-title";
  const thtml='<span class="card-id">#'+esc(String(task.id))+"</span>"+esc(task.title);
  titleEl.innerHTML=thtml;el._titleHtml=thtml;
  el.appendChild(titleEl);

  if(task._project){const p=document.createElement("div");p.className="card-project";p.textContent=task._project;el.appendChild(p);}

  if(task.detail){const d=document.createElement("div");d.className="card-detail";d.textContent=task.detail;el.appendChild(d);}

  const footer=document.createElement("div");footer.className="card-footer";
  const comments=task.comments||[];
  if(comments.length){const badge=document.createElement("span");badge.className="card-comments-badge";badge.textContent="💬 "+comments.length;footer.appendChild(badge);}
  else{footer.appendChild(document.createElement("span"));}
  const right=document.createElement("span");right.className="card-footer-right";
  if(task._filePath){
    const copyPath=document.createElement("button");
    copyPath.type="button";copyPath.className="card-copy-path";copyPath.title="Copy ticket file path to clipboard";copyPath.textContent="📋";
    copyPath.addEventListener("click",e=>{
      e.preventDefault();e.stopPropagation();
      navigator.clipboard.writeText(el._task._filePath)
        .then(()=>showToast("Copied file path"))
        .catch(()=>showToast("Copy failed",true));
    });
    right.appendChild(copyPath);
  }
  const statusBadge=document.createElement("span");statusBadge.className="card-status-badge";statusBadge.textContent=COL_LABELS[task._column]||task.status;right.appendChild(statusBadge);
  footer.appendChild(right);
  el.appendChild(footer);

  // Ready-column order buttons: ▲ / ▼ appear on hover to move the card up/down.
  if(task._column==="ready"){
    const btns=document.createElement("div");btns.className="card-order-btns";
    const upBtn=document.createElement("button");upBtn.type="button";upBtn.className="card-order-btn";upBtn.title="Move up in Ready";upBtn.textContent="▲";
    const dnBtn=document.createElement("button");dnBtn.type="button";dnBtn.className="card-order-btn";dnBtn.title="Move down in Ready";dnBtn.textContent="▼";
    btns.appendChild(upBtn);btns.appendChild(dnBtn);
    el.appendChild(btns);
    upBtn.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();if(!upBtn.disabled)reorderReady(el._task._board||el._filename,String(el._task.id),"up");});
    dnBtn.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();if(!dnBtn.disabled)reorderReady(el._task._board||el._filename,String(el._task.id),"down");});
    el._upBtn=upBtn;el._dnBtn=dnBtn;
    updateOrderBtnState(el,task);
  }

  // Ticket #63: fade/scale a freshly created card in. Only on incremental same-board
  // renders (motionOn) — first paint and board switches make every card "new" and
  // must not all animate. The class self-removes after the keyframes finish.
  if(motionOn){
    el.classList.add("card-enter");
    el.addEventListener("animationend",e=>{if(e.animationName==="cardEnter")el.classList.remove("card-enter");},{once:true});
  }
  return el;
}

// Enable/disable up/down buttons based on the card's position across ALL ready tasks.
function updateOrderBtnState(el,task){
  if(!el._upBtn||!el._dnBtn)return;
  const file=task._board||el._filename;
  const readyTasks=currentTasks
    .filter(t=>t._column==="ready")
    .slice().sort(compareReadyCards);
  const idx=readyTasks.findIndex(t=>String(t.id)===String(task.id)&&(t._board||file)===(task._board||file));
  el._upBtn.disabled=(idx<=0);
  el._dnBtn.disabled=(idx<0||idx>=readyTasks.length-1);
}

// Ticket #62: update a reused card's in-place fields (title, project, detail,
// comment badge, status badge) and toggled state classes (done/selected/unreviewed)
// without replacing the node — so its bound listeners and transient state (hover,
// .dragging) survive. _filePath == data-key, so the copy-path button's presence is
// constant for a persistent card and needs no add/remove here.
function updateCard(el,task,color,filename){
  el._task=task;el._filename=filename;
  el.classList.toggle("done",task._column==="done");
  el.classList.toggle("selected",taskKey(task)===selectedTaskKey);
  el.classList.toggle("unreviewed",isUnreviewed(task));
  if(el._color!==color){el._color=color;el.style.setProperty("--col-color",color);}

  const titleEl=el.querySelector(".card-title");
  const thtml='<span class="card-id">#'+esc(String(task.id))+"</span>"+esc(task.title);
  if(el._titleHtml!==thtml){el._titleHtml=thtml;titleEl.innerHTML=thtml;}

  // project (optional) — sits between the title and the detail/footer.
  let p=el.querySelector(".card-project");
  if(task._project){
    if(!p){p=document.createElement("div");p.className="card-project";el.insertBefore(p,el.querySelector(".card-detail")||el.querySelector(".card-footer"));}
    if(p.textContent!==task._project)p.textContent=task._project;
  }else if(p)p.remove();

  // detail (optional) — sits just above the footer.
  let d=el.querySelector(".card-detail");
  if(task.detail){
    if(!d){d=document.createElement("div");d.className="card-detail";el.insertBefore(d,el.querySelector(".card-footer"));}
    if(d.textContent!==task.detail)d.textContent=task.detail;
  }else if(d)d.remove();

  // comment badge — footer's first child is either the badge or a placeholder span.
  const footer=el.querySelector(".card-footer");
  const comments=task.comments||[];
  let badge=footer.querySelector(".card-comments-badge");
  if(comments.length){
    if(!badge){badge=document.createElement("span");badge.className="card-comments-badge";footer.replaceChild(badge,footer.firstChild);}
    const ctxt="💬 "+comments.length;
    if(badge.textContent!==ctxt)badge.textContent=ctxt;
  }else if(badge){footer.replaceChild(document.createElement("span"),badge);}

  // status badge
  const sb=el.querySelector(".card-status-badge");
  const stxt=COL_LABELS[task._column]||task.status;
  if(sb&&sb.textContent!==stxt)sb.textContent=stxt;

  // Ready order buttons: refresh disabled state (position may have changed).
  if(el._upBtn)updateOrderBtnState(el,task);
}

// ── Create Task Modal ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
let dockMode=false; // true when the Create Task form is docked into the side panel
function populateBoardSelect(preselect){
  const sel=$("fBoard");sel.innerHTML="";
  const placeholder=document.createElement("option");placeholder.value="";placeholder.textContent="— pick a board —";sel.appendChild(placeholder);
  const src=$("boardSelect");
  Array.from(src.options).forEach(o=>{if(o.value&&o.value!=="__all__"){const n=document.createElement("option");n.value=o.value;n.textContent=o.textContent;sel.appendChild(n);}});
  sel.value=(preselect&&preselect!=="__all__")?preselect:"";
}
function resetTaskForm(){$("fBoard").value=currentFile&&currentFile!=="__all__"?currentFile:"";$("fTitle").value="";$("fDetail").value="";$("fColumn").value="todo";$("fDepends").value="";$("fOptional").checked=false;$("fModel").value="";}
function openModal(){undock();populateBoardSelect(currentFile);$("modal").classList.add("open");resetTaskForm();setTimeout(()=>$("fTitle").focus(),50);}
function closeModal(){if(dockMode){undock();return;}$("modal").classList.remove("open");}
// Move the live <form> node into the side panel (keeps typed values + the single
// submit handler) so the board stays visible/scrollable while creating a task.
function dock(){
  if(dockMode)return;
  $("modal").classList.remove("open");
  closePanel(); // hide any task-detail view; the panel is reused for the form
  const form=$("taskForm"),dst=$("spDockBody");
  dst.appendChild(form);
  dst.style.display="block";$("spBody").style.display="none";
  $("spTitle").textContent="Create Task";
  $("dockBtn").textContent="← Pop out";
  $("sidePanel").classList.add("open");
  $("board").classList.add("panel-open");
  dockMode=true;
  setTimeout(()=>$("fTitle").focus(),50);
}
function undock(){
  if(!dockMode)return;
  const form=$("taskForm");
  $("modal").querySelector(".modal").appendChild(form); // restore into modal
  $("spDockBody").style.display="none";$("spBody").style.display="";
  $("dockBtn").textContent="Dock to sidebar →";
  $("sidePanel").classList.remove("open");
  $("board").classList.remove("panel-open");
  dockMode=false;
}
$("addBtn").addEventListener("click",openModal);
$("nudgeBoardBtn").addEventListener("click",async ()=>{
  try{await apiFetch("/api/orchestrator/nudge",{method:"POST"});
  showToast("Tick queued — refreshing in 2s…");
  setTimeout(poll, 2000);}
  catch(e){showToast("Failed to nudge orchestrator",true);}
});
$("markAllReadBtn").addEventListener("click",()=>{
  currentTasks.forEach(t=>{if(isUnreviewed(t))markReviewed(t);});
  if(currentBoardData)renderBoard(currentBoardData);
});
$("modalClose").addEventListener("click",closeModal);
$("cancelBtn").addEventListener("click",()=>{if(dockMode)undock();else closeModal();});
$("dockBtn").addEventListener("click",()=>{dockMode?(undock(),$("modal").classList.add("open")):dock();});
$("modal").addEventListener("click",e=>{if(e.target===$("modal"))closeModal();});
// Notification bell (ticket #3): open/close the Needs-attention modal.
$("bellBtn").addEventListener("click",openBellModal);
$("bellModalClose").addEventListener("click",closeBellModal);
$("bellModal").addEventListener("click",e=>{if(e.target===$("bellModal"))closeBellModal();});
document.addEventListener("keydown",e=>{if(e.key==="Escape"){closeModal();closeBellModal();closePanel();}});

let taskSubmitInFlight=false;
$("taskForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const targetBoard=$("fBoard").value;
  if(!targetBoard){showToast("Select a board first",true);return;}
  const payload={title:$("fTitle").value.trim(),detail:$("fDetail").value.trim(),column:$("fColumn").value,dependsOn:$("fDepends").value.trim(),optional:$("fOptional").checked,model:$("fModel").value.trim()};
  if(!payload.title){showToast("Title is required",true);return;}
  if(taskSubmitInFlight)return;
  taskSubmitInFlight=true;
  const btn=e.submitter||$("taskForm").querySelector('button[type="submit"]');
  if(btn)btn.disabled=true;
  try{const res=await apiFetch("/api/board/"+encodeURIComponent(targetBoard)+"/task",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});showToast("Created #"+res.task.id);resetTaskForm();if(dockMode)undock();else closeModal();lastMtime=0;poll();}
  catch(err){showToast("Failed to create task",true);}
  finally{taskSubmitInFlight=false;if(btn)btn.disabled=false;}
});

// ── Init ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
$("boardSelect").addEventListener("change",e=>{currentFile=e.target.value;currentBoardData=null;closePanel();updateBoardSettingsBtn();if(currentFile)startPolling();});

// ── Board Settings modal ──────────────────────────────────────────────────────────────────────────────────────────────────────
// The "__all__" virtual board has no real _meta.json, so settings only apply
// to a concrete board.
function updateBoardSettingsBtn(){
  $("boardSettingsBtn").style.display=(currentFile&&currentFile!=="__all__")?"":"none";
}
function envMapToText(m){
  if(!m||typeof m!=="object")return"";
  return Object.keys(m).map(k=>k+"="+m[k]).join("\n");
}
function envTextToMap(text){
  const out={};
  (text||"").split(/\r?\n/).forEach(line=>{
    const s=line.trim();
    if(!s||s[0]==="#")return;      // skip blanks and comments
    const eq=line.indexOf("=");
    if(eq<1)return;                 // need a non-empty key before '='
    const key=line.slice(0,eq).trim();
    if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key))return;  // valid env-var names only
    out[key]=line.slice(eq+1).trim();
  });
  return out;
}
// passthroughEnv is a list of secret env-var NAMES (values live only in the
// orchestrator env, never on disk). Textarea is one name per line.
function namesToText(list){
  return Array.isArray(list)?list.join("\n"):"";
}
function textToNames(text){
  const out=[],seen={};
  (text||"").split(/\r?\n/).forEach(line=>{
    const s=line.trim();
    if(!s||s[0]==="#")return;                             // skip blanks/comments
    if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(s))return;        // valid names only
    if(seen[s])return;                                    // de-dupe
    seen[s]=1;out.push(s);
  });
  return out;
}
function openBoardModal(){
  const d=currentBoardData||{};
  $("bProject").value=d.project||"";
  $("bDirectory").value=d.directory||"";
  // `description` is surfaced flat by the server from context.description.
  $("bDescription").value=d.description||(d.context&&d.context.description)||"";
  $("bCommitReq").value=d.commitRequirements||"";
  $("bUseWorktrees").checked=d.useWorktrees===true;
  $("bUseDocker").checked=d.useDocker===true;
  $("bEnvVars").value=envMapToText(d.envVars);
  $("bPassthroughEnv").value=namesToText(d.passthroughEnv);
  $("boardModal").classList.add("open");
  setTimeout(()=>$("bProject").focus(),50);
}
function closeBoardModal(){$("boardModal").classList.remove("open");}
$("boardSettingsBtn").addEventListener("click",openBoardModal);
$("boardModalClose").addEventListener("click",closeBoardModal);
$("boardCancelBtn").addEventListener("click",closeBoardModal);
$("boardModal").addEventListener("click",e=>{if(e.target===$("boardModal"))closeBoardModal();});
$("boardForm").addEventListener("submit",async e=>{
  e.preventDefault();
  if(!currentFile||currentFile==="__all__")return;
  const payload={project:$("bProject").value.trim(),directory:$("bDirectory").value.trim(),description:$("bDescription").value.trim(),commitRequirements:$("bCommitReq").value.trim(),useWorktrees:$("bUseWorktrees").checked,useDocker:$("bUseDocker").checked,envVars:envTextToMap($("bEnvVars").value),passthroughEnv:textToNames($("bPassthroughEnv").value)};
  try{
    await apiFetch("/api/board/"+encodeURIComponent(currentFile)+"/meta",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    showToast("Project settings saved");closeBoardModal();lastMtime=0;loadFiles();poll();
  }catch(err){showToast("Failed to save project settings",true);}
});

updateBoardSettingsBtn();
loadFiles();
// Ticket #3: keep the topbar bell current independently of which board is selected.
refreshAttention();
setInterval(refreshAttention, UI_CONFIG.attentionPollMs);

// ── View switching ──────────────────────────────────────────────
let currentView = "boards";
function switchView(view){
  if(currentView === view) return;
  currentView = view;
  document.querySelectorAll(".view-tab").forEach(b=>b.classList.toggle("active", b.dataset.view===view));
  const mainWrap=$("board").parentElement;
  mainWrap.style.display = view==="boards" ? "flex" : "none";
  $("view-setup").style.display = view==="setup" ? "block" : "none";
  $("view-performance").style.display = view==="performance" ? "block" : "none";
  // Fade the newly visible panel in
  const active=view==="boards"?mainWrap:$("view-"+view);
  active.classList.add("view-fade-in");
  active.addEventListener("animationend",()=>active.classList.remove("view-fade-in"),{once:true});
  if(view==="setup"){ if(window.renderOrchestrator) renderOrchestrator(); renderProfiles(); }
  if(view==="performance"){ perfActive=true; renderPerformance(); } else { perfActive=false; }
}
document.querySelectorAll(".view-tab").forEach(b=>b.addEventListener("click",()=>switchView(b.dataset.view)));

// ── Profiles tab ────────────────────────────────────────────────
async function renderProfiles(){
  const wrap = $("view-profiles");
  wrap.innerHTML = "<div style='color:#64748b'>Loading…</div>";
  let profiles=[], state={};
  try{ profiles = (await apiFetch("/api/profiles")).profiles||[]; }catch(e){}
  try{ state = await apiFetch("/api/orchestrator/state"); }catch(e){}
  let html = '<p class="setup-section">Concurrency</p>';
  html += '<div class="setup-grid">';
  html += '<div class="setup-field"><label>Max agents in flight</label>'
        + '<input type="number" id="capInput" min="0" max="20" value="'+(state.concurrencyCap??3)+'">'
        + '<button class="add-btn" id="capSave">Save</button></div>';
  html += '<div class="setup-field"><label>Idle reap (s)</label>'
        + '<input type="number" id="idleInput" min="60" max="7200" value="'+(state.idleSeconds??600)+'">'
        + '<button class="add-btn" id="idleSave">Save</button></div>';
  html += '<div class="setup-field"><label>Tick interval (s)</label>'
        + '<input type="number" id="tickSecondsInput" min="5" max="3600" value="'+(state.tickSeconds??60)+'">'
        + '<button class="add-btn" id="tickSecondsSave">Save</button></div>';
  html += '<div class="setup-field"><label>Max agent wall-clock (s, 0=off)</label>'
        + '<input type="number" id="maxAgentSecondsInput" min="0" max="86400" value="'+(state.maxAgentSeconds??0)+'">'
        + '<button class="add-btn" id="maxAgentSecondsSave">Save</button></div>';
  html += '<div class="setup-field"><label>LLM subprocess timeout (s)</label>'
        + '<input type="number" id="triageTimeoutSecondsInput" min="30" max="600" value="'+(state.triageTimeoutSeconds??120)+'">'
        + '<button class="add-btn" id="triageTimeoutSecondsSave">Save</button></div>';
  html += '</div>';
  html += '<hr class="setup-divider">';
  html += '<p class="setup-section">Loop models</p>';
  html += '<div style="color:var(--text-muted);font-size:11px;margin-bottom:10px;max-width:520px;">'
        + "Models for the orchestrator's background calls (triage, pre-kill summarizer). Leave blank for the Opus default.</div>";
  html += '<div class="setup-field" style="margin-bottom:12px;"><label style="min-width:130px;">Triage model</label>'
        + '<input type="text" id="triageModelInput" placeholder="claude-opus-4-8" value="'+(state.triageModel??"")
        + '" style="width:240px;">'
        + '<button class="add-btn" id="triageModelSave">Save</button></div>';
  html += '<div class="setup-field" style="margin-bottom:20px;"><label style="min-width:130px;">Summarizer model</label>'
        + '<input type="text" id="summarizerModelInput" placeholder="claude-opus-4-8" value="'+(state.summarizerModel??"")
        + '" style="width:240px;">'
        + '<button class="add-btn" id="summarizerModelSave">Save</button></div>';
  html += '<hr class="setup-divider">';
  html += '<p class="setup-section">Profiles</p>';
  html += '<button class="add-btn" id="newProfileBtn">+ New Profile</button><div id="profileList" style="margin-top:14px;"></div>';
  wrap.innerHTML = html;
  $("capSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({concurrencyCap:parseInt($("capInput").value,10)||0})});
    showToast("Concurrency saved");}
    catch(e){showToast("Failed to save concurrency",true);}
  });
  $("idleSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({idleSeconds:parseInt($("idleInput").value,10)||600})});
    showToast("Idle timeout saved");}
    catch(e){showToast("Failed to save idle timeout",true);}
  });
  $("tickSecondsSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tickSeconds:parseInt($("tickSecondsInput").value,10)||60})});
    showToast("Tick interval saved");}
    catch(e){showToast("Failed to save tick interval",true);}
  });
  $("maxAgentSecondsSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({maxAgentSeconds:parseInt($("maxAgentSecondsInput").value,10)||0})});
    showToast("Max agent cap saved");}
    catch(e){showToast("Failed to save max agent cap",true);}
  });
  $("triageTimeoutSecondsSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({triageTimeoutSeconds:parseInt($("triageTimeoutSecondsInput").value,10)||120})});
    showToast("LLM timeout saved");}
    catch(e){showToast("Failed to save LLM timeout",true);}
  });
  $("triageModelSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({triageModel:$("triageModelInput").value.trim()||"claude-opus-4-8"})});
    showToast("Triage model saved");}
    catch(e){showToast("Failed to save triage model",true);}
  });
  $("summarizerModelSave").addEventListener("click", async ()=>{
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({summarizerModel:$("summarizerModelInput").value.trim()||"claude-opus-4-8"})});
    showToast("Summarizer model saved");}
    catch(e){showToast("Failed to save summarizer model",true);}
  });
  $("newProfileBtn").addEventListener("click",()=>editProfile({name:"",whenToUse:"",model:UI_CONFIG.newProfileModel,systemPrompt:"",allowedTools:UI_CONFIG.newProfileTools.slice(),enabled:true}));
  const list = $("profileList"); list.innerHTML="";
  profiles.forEach(p=>{
    const card = document.createElement("div");
    card.style.cssText="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:10px;";
    card.innerHTML = '<div style="font-weight:700;">'+esc(p.displayName||p.name)+' <span style="color:#64748b;font-size:11px;">'+esc(p.model||"")+'</span></div>'
      + '<div style="color:var(--text-muted);font-size:12px;margin:4px 0;">'+esc(p.whenToUse||"")+'</div>';
    const editBtn=document.createElement("button");editBtn.className="add-btn";editBtn.textContent="Edit";
    editBtn.addEventListener("click",()=>editProfile(p));
    const delBtn=document.createElement("button");delBtn.className="sp-del-btn";delBtn.style.marginLeft="8px";delBtn.textContent="Delete";
    delBtn.addEventListener("click",async ()=>{ if(!confirm("Delete profile "+p.name+"?"))return;
      try{ await apiFetch("/api/profiles/"+encodeURIComponent(p.name),{method:"DELETE"}); showToast("Profile deleted"); renderProfiles(); }catch(e){ showToast("Failed to delete profile",true); } });
    card.appendChild(editBtn);card.appendChild(delBtn);list.appendChild(card);
  });
}

function editProfile(p){
  const wrap=$("view-profiles");
  const f=document.createElement("div");
  f.style.cssText="background:var(--surface);border-radius:8px;padding:16px;margin-top:14px;max-width:640px;";
  f.innerHTML =
    '<label class="form-label">Name (lowercase, no spaces)<input class="form-input" id="pName" value="'+esc(p.name||"")+'"'+(p.name?" disabled":"")+'></label>'
   +'<label class="form-label">Display name<input class="form-input" id="pDisplay" value="'+esc(p.displayName||"")+'"></label>'
   +'<label class="form-label">When to use<textarea class="form-input form-textarea" id="pWhen">'+esc(p.whenToUse||"")+'</textarea></label>'
   +'<label class="form-label">Default model<input class="form-input" id="pModel" value="'+esc(p.model||"")+'"></label>'
   +'<label class="form-label">Allowed tools (comma-sep)<input class="form-input" id="pTools" value="'+esc((p.allowedTools||[]).join(","))+'"></label>'
   +'<label class="form-label">System prompt<textarea class="form-input form-textarea" id="pPrompt">'+esc(p.systemPrompt||"")+'</textarea></label>'
   +'<button class="btn btn-create" id="pSave">Save Profile</button>';
  wrap.appendChild(f);
  f.scrollIntoView({behavior:"smooth"});
  $("pSave").addEventListener("click", async ()=>{
    const name=(p.name||$("pName").value.trim());
    if(!name){showToast("Name required",true);return;}
    const body={name,displayName:$("pDisplay").value.trim(),whenToUse:$("pWhen").value.trim(),
      model:$("pModel").value.trim(),
      allowedTools:$("pTools").value.split(",").map(s=>s.trim()).filter(Boolean),
      systemPrompt:$("pPrompt").value, enabled:true};
    try{await apiFetch("/api/profiles/"+encodeURIComponent(name),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    showToast("Profile saved"); renderProfiles();}
    catch(e){showToast("Failed to save profile",true);}
  });
}

// ── Performance tab ──────────────────────────────────────────────
let perfActive = false;
const perfExpanded = new Set();   // pids whose graph is open

function drawGraph(canvas, history){
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);
  if(!history || history.length < 2){
    ctx.fillStyle = "#888"; ctx.font = "11px sans-serif";
    ctx.fillText("collecting…", 8, H/2); return;
  }
  const cpu = history.map(p=>p.cpu), mem = history.map(p=>p.mem);
  const maxCpu = Math.max(10, ...cpu), maxMem = Math.max(1, ...mem);
  const plot = (vals, max, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    vals.forEach((v,i)=>{
      const x = (i/(vals.length-1))*(W-8)+4;
      const y = H-4 - (v/max)*(H-12);
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);
    });
    ctx.stroke();
  };
  plot(cpu, maxCpu, "#3b82f6");   // CPU% — blue
  plot(mem, maxMem, "#10b981");   // mem  — green
}

async function renderPerformance(){
  const wrap = $("view-performance");
  let snap;
  try { snap = await apiFetch("/api/performance"); }
  catch(e){ wrap.innerHTML = "<p>Failed to load performance data.</p>"; return; }
  if(!snap.available){
    wrap.innerHTML = "<p>Process monitoring needs <code>psutil</code>. Run "
      + "<code>pip install psutil</code> and restart the server.</p>";
    return;
  }
  const t = snap.totals || {};
  let html = "<div style='margin-bottom:12px;font-size:13px;color:var(--text-muted)'>"
    + "Sessions: <b>"+(t.sessionCount||0)+"</b> &nbsp; CPU: <b>"+(t.cpuPercent||0)
    + "%</b> &nbsp; Mem: <b>"+(t.memoryMB||0)+" MB</b> &nbsp; @ "+(snap.sampledAt||"")
    + "</div>";
  html += "<table style='width:100%;border-collapse:collapse;font-size:13px'>";
  for(const s of (snap.sessions||[])){
    const open = perfExpanded.has(s.pid);
    const badge = (txt,bg)=>"<span style='background:"+bg+";color:#fff;border-radius:3px;"
      +"padding:1px 6px;margin-left:6px;font-size:11px'>"+txt+"</span>";
    html += "<tr data-pid='"+s.pid+"' class='perf-row' style='border-top:1px solid var(--surface-alt);cursor:pointer'>"
      + "<td style='padding:6px 4px'>"+(open?"▾":"▸")+" PID "+s.pid
      +   badge(s.kind, s.kind==="headless"?"#a855f7":"#0ea5e9")
      +   badge(s.owned?"owned":"external", s.owned?"#64748b":"#ef4444")
      +   (s.model?badge(getModelName(s.model),"#1f2937"):"")
      +   (s.ticket?(" "+s.board+"#"+s.ticket):"")
      + "</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.cpuPercent+"%</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.memoryMB+" MB</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.childCount+" sub</td>"
      + "<td style='padding:6px 4px;text-align:right'>"
      +   "<button class='perf-kill' data-pid='"+s.pid+"'>Kill</button></td>"
      + "</tr>";
    if(open){
      let kids = (s.children||[]).map(c=>"PID "+c.pid+" "+c.name+" ("+c.cpuPercent
        +"%, "+c.memoryMB+"MB)").join("<br>") || "<i>no subprocesses</i>";
      let modelInfo = s.model?("<div style='font-size:12px;color:var(--text-muted);margin-bottom:6px'>Model: <b>"+esc(s.model)+"</b></div>"):"";
      html += "<tr><td colspan='5' style='padding:8px 16px;background:var(--bg)'>"
        + modelInfo
        + "<canvas width='520' height='90' data-pid='"+s.pid
        +   "' style='display:block;margin-bottom:8px;border:1px solid var(--surface-alt)'></canvas>"
        + "<div style='font-size:11px;color:#3b82f6'>■ CPU%</div>"
        + "<div style='font-size:11px;color:#10b981'>■ Memory</div>"
        + "<div style='margin-top:6px;font-size:12px;color:var(--text-muted)'>"+kids+"</div>"
        + "</td></tr>";
    }
  }
  html += "</table>";
  wrap.innerHTML = html;

  // draw graphs for expanded rows
  wrap.querySelectorAll("canvas[data-pid]").forEach(c=>{
    const s = (snap.sessions||[]).find(x=>String(x.pid)===c.dataset.pid);
    if(s) drawGraph(c, s.history);
  });
  // expand/collapse
  wrap.querySelectorAll(".perf-row").forEach(r=>{
    r.addEventListener("click", e=>{
      if(e.target.classList.contains("perf-kill")) return;
      const pid = +r.dataset.pid;
      perfExpanded.has(pid) ? perfExpanded.delete(pid) : perfExpanded.add(pid);
      renderPerformance();
    });
  });
  // kill
  wrap.querySelectorAll(".perf-kill").forEach(b=>{
    b.addEventListener("click", async e=>{
      e.stopPropagation();
      try{
        await apiFetch("/api/performance/kill/"+encodeURIComponent(b.dataset.pid), {method:"POST"});
        showToast("Kill sent to PID "+b.dataset.pid);
        renderPerformance();
      }catch(err){ showToast("Failed to kill PID "+b.dataset.pid,true); }
    });
  });
}

setInterval(()=>{ if(perfActive) renderPerformance(); }, UI_CONFIG.perfRefreshMs);

// ── Orchestrator tab ────────────────────────────────────────────
async function renderOrchestrator(){
  const wrap = $("view-orchestrator");
  let state={}, activity={entries:[]}, all={tasks:[]};
  try{ state = await apiFetch("/api/orchestrator/state"); }catch(e){}
  try{ activity = await apiFetch("/api/orchestrator/activity"); }catch(e){}
  try{ all = await apiFetch("/api/board/__all__"); }catch(e){}

  // Auto-commit / auto-push kill switch (ticket #55). Both default on; turning
  // auto-commit off makes completed tickets leave their diff uncommitted for review.
  const acOn = state.autoCommit!==false, apOn = state.autoPush!==false;

  let html = '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:20px;">';
  html += '<button class="sp-del-btn" id="stopAll">Stop all agents</button>';
  html += '<button class="sp-del-btn" id="restartServerBtn" style="background:#b45309;">Restart server</button>';
  html += '</div>';

  html += '<div class="sw-row"><label class="sw"><input type="checkbox" id="orchToggle"'+(state.enabled?" checked":"")+'><span class="sw-track"></span><span class="sw-thumb"></span></label>'
        + '<span class="sw-label">Orchestrator running<small>'+(state.enabled?"Picking up new tickets":"Paused — not picking up tickets")+'</small></span></div>';
  html += '<div class="sw-row"><label class="sw"><input type="checkbox" id="autoCommitToggle"'+(acOn?" checked":"")+'><span class="sw-track"></span><span class="sw-thumb"></span></label>'
        + '<span class="sw-label">Auto-commit<small>'+(acOn?"Commits completed ticket diffs automatically":"Off — review diffs before committing")+'</small></span></div>';
  html += '<div class="sw-row"><label class="sw"><input type="checkbox" id="autoPushToggle"'+(apOn?" checked":"")+(acOn?"":" disabled")+'><span class="sw-track"></span><span class="sw-thumb"></span></label>'
        + '<span class="sw-label" style="'+(acOn?"":"opacity:0.45")+'">Auto-push<small>'+(apOn?"Pushes branch after committing":"Off — commits locally only")+'</small></span></div>';

  // Human-attention inbox moved to the topbar notification bell (ticket #3);
  // answer blocked-ticket questions there. This tab keeps in-flight + activity.

  // In-flight with kill buttons.
  const inFlight = (all.tasks||[]).filter(t=>t.orchestrator&&t.orchestrator.state==="dispatched");
  html += '<h2 style="font-size:16px;margin:18px 0 10px;">In flight ('+inFlight.length+')</h2><div id="inflight"></div>';

  // Activity feed.
  html += '<h2 style="font-size:16px;margin:18px 0 10px;">Activity</h2><div id="feed"></div>';
  wrap.innerHTML = html;

  $("orchToggle").addEventListener("change", async (ev)=>{
    const val=ev.target.checked;
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({enabled:val})});
    renderOrchestrator();}
    catch(e){ev.target.checked=!val;showToast("Failed to toggle orchestrator",true);}
  });
  $("stopAll").addEventListener("click", async ()=>{
    if(!confirm("Kill all in-flight agents?"))return;
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({stopAllRequested:true})});
    showToast("Stop-all requested"); renderOrchestrator();}
    catch(e){showToast("Failed to request stop-all",true);}
  });
  $("restartServerBtn").addEventListener("click", async ()=>{
    if(!confirm("Restart server? In-flight agents keep running and will be re-adopted.")) return;
    const btn=$("restartServerBtn"); btn.disabled=true;
    try{ await apiFetch("/api/server/restart",{method:"POST"}); }catch(e){}
    showToast("Restarting server…");
    const t0=Date.now();
    const waitBack=async ()=>{
      try{
        await apiFetch("/api/files");
        showToast("Server back up");
        renderOrchestrator();
      }catch(e){
        if(Date.now()-t0 < 15000) setTimeout(waitBack, 500);
        else { showToast("Server did not come back", true); btn.disabled=false; }
      }
    };
    setTimeout(waitBack, 800);
  });
  $("autoCommitToggle").addEventListener("change", async (ev)=>{
    const val=ev.target.checked;
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({autoCommit:val})});
    showToast(val?"Auto-commit enabled":"Auto-commit disabled"); renderOrchestrator();}
    catch(e){ev.target.checked=!val;showToast("Failed to toggle auto-commit",true);}
  });
  const apChk=$("autoPushToggle");
  if(apChk) apChk.addEventListener("change", async (ev)=>{
    const val=ev.target.checked;
    try{await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({autoPush:val})});
    showToast(val?"Auto-push enabled":"Auto-push disabled"); renderOrchestrator();}
    catch(e){ev.target.checked=!val;showToast("Failed to toggle auto-push",true);}
  });

  const infl=$("inflight");
  if(!inFlight.length) infl.innerHTML='<div style="color:#475569;font-size:12px;">No agents running.</div>';
  inFlight.forEach(t=>{
    const d=document.createElement("div");
    d.style.cssText="background:var(--surface);border-radius:6px;padding:10px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;";
    d.innerHTML='<span>#'+esc(t.id)+' '+esc(t.title)+' <span style="color:#64748b;font-size:11px;">'+esc(t.orchestrator.profile||"")+' · pid '+esc(String(t.orchestrator.pid||""))+'</span></span>';
    const k=document.createElement("button");k.className="sp-del-btn";k.textContent="Kill";
    k.addEventListener("click",async ()=>{ try{ await apiFetch("/api/orchestrator/kill/"+encodeURIComponent(t._board)+"/"+encodeURIComponent(t.id),{method:"POST"}); showToast("Kill sent"); renderOrchestrator(); }catch(e){ showToast("Failed to send kill",true); } });
    d.appendChild(k); infl.appendChild(d);
  });

  const feed=$("feed");
  (activity.entries||[]).slice().reverse().slice(0,80).forEach(e=>{
    const row=document.createElement("div");
    row.style.cssText="font-size:12px;color:var(--text-muted);padding:4px 0;border-bottom:1px solid var(--surface-alt);";
    row.textContent="["+(e.kind||"")+"] #"+(e.ticket||"")+" "+(e.reason||e.message||"")+"  "+(e.ts||"");
    feed.appendChild(row);
  });
}

function questionCard(t){
  const q=t.orchestrator.question;
  const card=document.createElement("div");
  card.style.cssText="background:var(--surface);border-left:3px solid #ef4444;border-radius:6px;padding:12px;margin-bottom:10px;";
  card.innerHTML='<div style="font-weight:600;">#'+esc(t.id)+' '+esc(t.title)+'</div>'
    +'<div style="margin:6px 0;color:var(--text-muted);font-size:13px;">'+esc(q.prompt)+'</div>';
  const ctrl=document.createElement("div");
  let getValue=()=>null;
  if(q.type==="choice"){
    (q.options||[]).forEach(opt=>{
      const id="opt_"+t.id+"_"+opt.replace(/\W/g,"");
      const lbl=document.createElement("label");lbl.style.cssText="display:block;font-size:13px;margin:3px 0;";
      lbl.innerHTML='<input type="'+(q.multi?"checkbox":"radio")+'" name="q_'+esc(t.id)+'" value="'+esc(opt)+'"> '+esc(opt);
      ctrl.appendChild(lbl);
    });
    getValue=()=>{
      const checked=[...ctrl.querySelectorAll("input:checked")].map(i=>i.value);
      return q.multi?checked:(checked[0]??null);
    };
  } else {
    const inp=document.createElement("input");
    inp.className="form-input"; inp.type=(q.format==="number"?"number":"text");
    ctrl.appendChild(inp);
    getValue=()=>inp.value.trim()||null;
  }
  card.appendChild(ctrl);
  const notes=document.createElement("textarea");
  notes.className="form-input form-textarea"; notes.placeholder="Notes (override if the question is off-base)…";
  notes.style.marginTop="8px"; card.appendChild(notes);
  const submit=document.createElement("button");submit.className="btn btn-create";submit.style.marginTop="8px";submit.textContent="Answer & resume";
  submit.addEventListener("click",async ()=>{
    if(submit.disabled)return;
    submit.disabled=true;
    try{
      await apiFetch("/api/orchestrator/answer/"+encodeURIComponent(t._board)+"/"+encodeURIComponent(t.id),
        {method:"POST",headers:{"Content-Type":"application/json"},
         body:JSON.stringify({value:getValue(),notes:notes.value.trim()})});
      showToast("Answer sent"); onQuestionAnswered();
    }catch(e){ showToast("Failed to send answer",true); submit.disabled=false; }
  });
  card.appendChild(submit);
  return card;
}

// ── Notification bell (ticket #3) ───────────────────────────────
// A blocked ticket needs human input when it carries an unanswered
// orchestrator.question. refreshAttention() polls all boards for those, updates the
// topbar badge, and (if open) re-renders the modal. The bell is the single entry
// point for answering — the same questionCard() the Setup tab used to host inline.
async function refreshAttention(){
  let all;
  try{ all = await apiFetch("/api/board/__all__"); }
  catch(e){ return; }  // server down — keep last-known count; the status pill shows offline
  attentionTasks=(all.tasks||[]).filter(t=>t.orchestrator&&t.orchestrator.question&&!t.orchestrator.question.answer);
  updateBell();
  if($("bellModal").classList.contains("open")) renderBellInbox();
}

function updateBell(){
  const btn=$("bellBtn"), badge=$("bellBadge");
  if(!btn||!badge) return;
  const n=attentionTasks.length;
  btn.classList.toggle("has-alerts", n>0);
  if(n>0){
    badge.textContent = n>99 ? "99+" : String(n);
    badge.hidden=false;
    if(n>_lastAttentionCount){ badge.classList.remove("bump"); void badge.offsetWidth; badge.classList.add("bump"); }
    btn.title = n+" blocked ticket"+(n===1?"":"s")+" awaiting your input";
  } else {
    badge.hidden=true;
    btn.title="No tickets need your input";
  }
  _lastAttentionCount=n;
}

function renderBellInbox(){
  const inbox=$("bellInbox");
  if(!inbox) return;
  inbox.innerHTML="";
  if(!attentionTasks.length){
    inbox.innerHTML='<div style="color:var(--text-muted);font-size:13px;text-align:center;padding:28px 12px;">&#x2713; All caught up — nothing needs your input.</div>';
    return;
  }
  attentionTasks.forEach(t=>inbox.appendChild(questionCard(t)));
}

function openBellModal(){ renderBellInbox(); $("bellModal").classList.add("open"); }
function closeBellModal(){ $("bellModal").classList.remove("open"); }

// After an answer is submitted (from the bell modal), re-poll attention and, if the
// Setup tab is showing, refresh its in-flight/activity view too.
function onQuestionAnswered(){
  refreshAttention();
  if(currentView==="setup" && window.renderOrchestrator) renderOrchestrator();
}

// Ticket #66: keep the fixed-height layout in sync with the real top-bar height.
// The bar is now allowed to wrap to a second row at narrow widths, so it is no
// longer a constant 52px. Publish its measured height into --topbar-h; every
// height/offset that used to hard-code 52px reads var(--topbar-h, 52px), so this
// is a no-op enhancement (the 52px fallback holds if JS never runs).
(function syncTopbarHeight(){
  const bar = document.querySelector(".topbar");
  if(!bar) return;
  const apply = () => document.documentElement.style.setProperty(
    "--topbar-h", Math.round(bar.getBoundingClientRect().height) + "px");
  apply();
  if(window.ResizeObserver){ new ResizeObserver(apply).observe(bar); }
  else { window.addEventListener("resize", apply); }
})();
