/* Repertoire Creator - browser interface.
 *
 * The server owns the data; this file owns what you are looking at. Every
 * edit is a request that returns the whole tree again, so there is no tree
 * surgery here and no way for the screen to drift out of step with the PGN
 * on disk.
 *
 * Two workspaces share almost all of this code, because they are the same
 * object wearing different labels: a repertoire chapter and a universal
 * recording are both a move tree in a PGN file. What differs is where they
 * are saved, whether they know which colour you play, and what the side
 * panel offers. `state.mode` is the only switch.
 *
 * Three modes share the board. Editing plays a move into the tree, drill
 * treats the same click as an answer, and assist adds an overlay saying what
 * your book plays here without changing anything.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const FILES = "abcdefgh";

const state = {
  mode: "rep",           // "rep" | "uni"
  health: null,
  list: [],
  rep: null,
  chapterId: null,
  uni: null,             // { recordings, book }
  recordingId: null,
  doc: null,             // the tree payload, whichever mode we are in
  path: "",
  flipped: false,
  pick: null,
  dests: [],
  tab: "moves",
  evalCache: new Map(),
  evalToken: 0,
  linesToken: 0,
  linesTimer: null,
  lines: null,
  assist: false,
  assistInfo: null,
  scan: null,
  report: null,
  drill: null,
  git: null,
};

const TABS_BY_MODE = {
  rep: ["moves", "gaps", "transpositions", "drill"],
  uni: ["moves", "book", "gaps"],
};

// --------------------------------------------------------------- plumbing

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) message = payload.detail;
    } catch (_) { /* not JSON; keep the status line */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

let statusTimer = null;
function say(message, kind) {
  const box = $("status");
  box.textContent = message || "";
  box.className = "status" + (message ? "" : " hidden") + (kind ? " " + kind : "");
  clearTimeout(statusTimer);
  if (message && kind !== "error") {
    statusTimer = setTimeout(() => { box.className = "status hidden"; }, 4200);
  }
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function guard(work) {
  try { return await work(); }
  catch (error) { say(error.message, "error"); return null; }
}

const isUni = () => state.mode === "uni";

// ------------------------------------------------------------------ nodes

const nodes = () => (state.doc ? state.doc.tree.nodes : {});
const node = (path) => nodes()[path === undefined ? state.path : path] || null;

function parentPath(path) {
  const current = node(path);
  return current ? current.parent : null;
}

function firstChild(path) {
  const current = node(path);
  return current && current.children.length ? current.children[0] : null;
}

function lastOfLine(path) {
  let here = path;
  for (;;) {
    const next = firstChild(here);
    if (next === null) return here;
    here = next;
  }
}

function moveLabel(item, { force } = {}) {
  if (!item || !item.san) return "start";
  if (item.whiteMoved) return `${item.moveNumber}.${item.san}${item.nagText}`;
  return force ? `${item.moveNumber}...${item.san}${item.nagText}`
               : `${item.san}${item.nagText}`;
}

function evalLabel(payload) {
  if (!payload) return "";
  if (payload.mate !== null && payload.mate !== undefined) {
    return (payload.mate > 0 ? "+M" : "-M") + Math.abs(payload.mate);
  }
  if (payload.cp === null || payload.cp === undefined) return "";
  return (payload.cp / 100 >= 0 ? "+" : "") + (payload.cp / 100).toFixed(2);
}

/* Lichess's own centipawn-to-winning-chances curve, so the bar moves the
 * way the bar on lichess.org moves. */
function evalFraction(payload) {
  if (!payload) return 0.5;
  if (payload.mate !== null && payload.mate !== undefined) {
    return payload.mate > 0 ? 1 : 0;
  }
  if (payload.cp === null || payload.cp === undefined) return 0.5;
  const capped = Math.max(-1500, Math.min(1500, payload.cp));
  const chances = 2 / (1 + Math.exp(-0.00368208 * capped)) - 1;
  return Math.max(0, Math.min(1, (chances + 1) / 2));
}

// ------------------------------------------------------------------ board

function boardUrl(fen, options = {}) {
  const params = new URLSearchParams({ fen, size: "640" });
  if (state.flipped) params.set("flip", "1");
  if (options.lastMove) params.set("lastmove", options.lastMove);
  if (options.circles && options.circles.length) {
    params.set("circles", options.circles.map((c) => c.join(":")).join(","));
  }
  if (options.arrows && options.arrows.length) {
    params.set("arrows", options.arrows.map((a) => a.join(":")).join(","));
  }
  return "/api/board?" + params.toString();
}

function buildSquares() {
  const holder = $("squares");
  if (holder.childElementCount === 64) return;
  holder.innerHTML = "";
  for (let index = 0; index < 64; index += 1) {
    const cell = document.createElement("div");
    cell.addEventListener("click", () => onSquare(squareAt(index)));
    holder.appendChild(cell);
  }
}

function squareAt(index) {
  const row = Math.floor(index / 8);
  const column = index % 8;
  const file = state.flipped ? 7 - column : column;
  const rank = state.flipped ? row + 1 : 8 - row;
  return FILES[file] + rank;
}

function paintSquares() {
  const cells = $("squares").children;
  for (let index = 0; index < cells.length; index += 1) {
    const name = squareAt(index);
    cells[index].className =
      (state.pick === name ? "pick " : "") +
      (state.dests.includes(name) ? "dest" : "");
  }
}

function currentFen() {
  if (state.drill && state.drill.card) return state.drill.card.fen;
  const item = node();
  return item ? item.fen : "";
}

async function onSquare(name) {
  const fen = currentFen();
  if (!fen) return;

  if (state.pick && state.dests.includes(name)) {
    const from = state.pick;
    state.pick = null;
    state.dests = [];
    paintSquares();
    if (state.drill && state.drill.card) await answerDrill(from + name);
    else await playMove(from + name);
    return;
  }

  if (state.pick === name) {
    state.pick = null;
    state.dests = [];
    paintSquares();
    return;
  }

  const data = await guard(() =>
    api("GET", `/api/legal?fen=${encodeURIComponent(fen)}&square=${name}`));
  const dests = data && data.moves ? data.moves[name] || [] : [];
  state.pick = dests.length ? name : null;
  state.dests = dests;
  paintSquares();
}

async function playMove(uci) {
  if (!state.doc) return;
  const payload = await guard(() =>
    api("POST", docUrl("play"), { path: state.path, uci }));
  if (!payload) return;
  applyDoc(payload);
  if (payload.result && payload.result.created === false) {
    say("You already had that move; walked to it instead of adding a copy.");
  }
}

// ------------------------------------------------------------------ render

function renderBoard() {
  const drillCard = state.drill && state.drill.card;
  const item = node();
  const fen = currentFen();
  if (!fen) {
    $("board").removeAttribute("src");
    return;
  }

  let options;
  if (drillCard) {
    options = {};
  } else {
    const arrows = item ? item.arrows.slice() : [];
    // The assist draws what your book plays here, without touching the tree.
    if (state.assist && state.assistInfo && state.assistInfo.status === "known") {
      for (const move of state.assistInfo.moves.slice(0, 2)) {
        arrows.push(["blue", move.uci.slice(0, 2), move.uci.slice(2, 4)]);
      }
    }
    options = {
      lastMove: item && item.lastMove ? item.lastMove.join("") : "",
      circles: item ? item.circles : [],
      arrows,
    };
  }
  $("board").src = boardUrl(fen, options);
  paintSquares();

  const banner = $("turn-banner");
  if (drillCard) {
    $("turn-text").innerHTML =
      `<span class="mine">Your move</span> &mdash; play your repertoire`;
    $("node-eval").textContent = "";
    banner.classList.remove("hidden");
    return;
  }

  if (!item) return;
  if (item.myTurnNext === null || item.myTurnNext === undefined) {
    $("turn-text").textContent =
      item.fen.includes(" w ") ? "White to move" : "Black to move";
  } else {
    $("turn-text").innerHTML = item.myTurnNext
      ? `<span class="mine">Your move</span> &mdash; what do you play here?`
      : `<span class="theirs">Their move</span> &mdash; cover their tries`;
  }
  $("node-eval").textContent =
    item.eval ? `${evalLabel(item.eval)} saved in the PGN` : "no saved eval";
}

function renderEvalBar(payload) {
  const fraction = evalFraction(payload);
  $("evalfill").style.height = `${(fraction * 100).toFixed(1)}%`;
  $("evaltext").textContent = evalLabel(payload);
  $("evalbar").classList.toggle("flipped", state.flipped);
}

async function refreshEval() {
  const fen = currentFen();
  if (!fen) return;

  const item = node();
  if (item && item.eval) renderEvalBar(item.eval);   // instant, from the PGN
  if (state.evalCache.has(fen)) {
    renderEvalBar(state.evalCache.get(fen));
    return;
  }
  if (!state.health || !state.health.stockfish) {
    if (!item || !item.eval) renderEvalBar(null);
    return;
  }

  const token = ++state.evalToken;
  try {
    const payload = await api("POST", "/api/eval", { fen });
    if (token !== state.evalToken) return;             // a later position won
    if (payload && payload.eval) {
      state.evalCache.set(fen, payload.eval);
      renderEvalBar(payload.eval);
    }
  } catch (_) { /* the bar is a nicety; never let it raise a banner */ }
}

// --------------------------------------------------------- engine lines

function linesCount() {
  const value = parseInt($("lines-count").value, 10);
  return Math.max(1, Math.min(state.health ? state.health.maxLines || 5 : 5,
                              Number.isFinite(value) ? value : 2));
}

function scheduleLines() {
  clearTimeout(state.linesTimer);
  // A short wait keeps arrow-key scrubbing from queueing an analysis per
  // position; only where you stop gets analysed.
  state.linesTimer = setTimeout(refreshLines, 260);
}

async function refreshLines() {
  const fen = currentFen();
  const body = $("lines-body");
  if (!fen) { body.innerHTML = ""; return; }
  if (!state.health || !state.health.sibling) {
    body.innerHTML = `<p class="empty">Engine suggestions need the sibling app.</p>`;
    return;
  }

  const token = ++state.linesToken;
  if (!state.lines) body.innerHTML = `<p class="empty">Thinking...</p>`;
  let payload = null;
  try {
    payload = await api("POST", "/api/lines", { fen, count: linesCount() });
  } catch (error) {
    if (token === state.linesToken) {
      body.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
    }
    return;
  }
  if (token !== state.linesToken) return;
  state.lines = payload;
  renderLines();
}

function renderLines() {
  const body = $("lines-body");
  const payload = state.lines;
  $("lines-source").textContent = payload && payload.lines.length
    ? (payload.source === "cloud" ? "from the Lichess cloud" : "")
    : "";
  if (!payload || !payload.lines.length) {
    body.innerHTML = `<p class="empty">${
      payload && payload.gameOver ? "The game is over here."
        : "No suggestions for this position."}</p>`;
    return;
  }

  // Mark the moves you already have, so a suggestion you have covered is not
  // mistaken for one you have missed.
  const item = node();
  const have = new Set();
  if (item) {
    for (const childPath of item.children) {
      const child = nodes()[childPath];
      if (child) have.add(child.uci);
    }
  }

  body.innerHTML = "";
  for (const line of payload.lines) {
    const row = document.createElement("div");
    row.className = "eline";
    const positive = (line.mate !== null && line.mate !== undefined)
      ? line.mate > 0 : (line.cp || 0) >= 0;
    row.innerHTML =
      `<span class="score ${positive ? "plus" : "minus"}">${escapeHtml(line.text)}</span>` +
      (have.has(line.first.uci) ? `<span class="have" title="already in your tree">&#10003;</span>` : "") +
      `<span class="pv">${escapeHtml(line.line)}</span>`;
    row.title = `Depth ${line.depth} - click to play ${line.first.san}`;
    row.addEventListener("click", () => playMove(line.first.uci));
    body.appendChild(row);
  }
}

// -------------------------------------------------------------- assist

async function refreshAssist() {
  const box = $("assist-line");
  if (!isUni() || !state.assist) {
    box.classList.add("hidden");
    state.assistInfo = null;
    return;
  }
  const item = node();
  if (!item) { box.classList.add("hidden"); return; }

  const parent = item.parent === null ? null : nodes()[item.parent];
  const payload = await guard(() => api("POST", "/api/universal/lookup", {
    fen: item.fen,
    previousFen: parent ? parent.fen : null,
  }));
  if (!payload) return;
  state.assistInfo = payload;

  box.classList.remove("hidden");
  box.className = "assist-line " + payload.status;
  if (payload.status === "known") {
    const listed = payload.moves.slice(0, 3).map((move) => {
      const where = move.sources.map((s) => s.name).slice(0, 2).join(", ");
      return `<b>${escapeHtml(move.san)}</b> <span class="src">${escapeHtml(where)}</span>`;
    }).join(" &nbsp;|&nbsp; ");
    box.innerHTML = `Your book plays ${listed}`;
  } else if (payload.status === "gap") {
    box.innerHTML =
      `<b>Gap.</b> You have reached this position through your own lines and ` +
      `there is nothing recorded from here. Play the move you want and it is saved.`;
  } else {
    box.innerHTML =
      `Outside your book &mdash; no line of yours passes through this position.`;
  }
  renderBoard();
}

// ------------------------------------------------------------ annotations

function renderAnnotations() {
  const item = node();
  const editable = Boolean(item && item.san);
  $("comment").value = item ? item.comment : "";
  $("comment").disabled = !item;
  $("btn-delete").disabled = !editable;
  $("btn-promote").disabled = !editable || (item && item.isMainline);

  const row = $("nag-row");
  row.innerHTML = "";
  const choices = (state.health && state.health.nags) || [];
  for (const choice of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = choice.symbol;
    button.disabled = !editable;
    if (item && item.nags.includes(choice.code)) button.classList.add("on");
    button.addEventListener("click", () => toggleNag(choice.code));
    row.appendChild(button);
  }
}

function gapPaths() {
  const set = new Set();
  for (const gap of (state.doc ? state.doc.gaps : [])) {
    if (!gap.kind || gap.kind === "missing") set.add(gap.path);
  }
  return set;
}

function renderTree() {
  const holder = $("movetree");
  if (!state.doc) { holder.innerHTML = ""; return; }
  const gaps = gapPaths();
  const all = nodes();

  const moveHtml = (item, forceNumber) => {
    const classes = ["mv"];
    if (item.path === state.path) classes.push("current");
    if (item.mine) classes.push("mine");
    if (gaps.has(item.path)) classes.push("gap");
    const evalText = item.eval ? `<span class="ev">${escapeHtml(evalLabel(item.eval))}</span>` : "";
    const note = item.comment ? `<span class="note" title="${escapeHtml(item.comment)}">&#9998;</span>` : "";
    return `<span class="${classes.join(" ")}" data-path="${item.path}">` +
           `${escapeHtml(moveLabel(item, { force: forceNumber }))}${note}${evalText}</span> `;
  };

  // PGN reading order: the move, then its sidelines, then the line continues.
  const renderFrom = (startPath) => {
    let html = "";
    let here = startPath;
    let forceNumber = true;
    for (;;) {
      const parent = all[here];
      if (!parent || !parent.children.length) break;
      const main = all[parent.children[0]];
      html += moveHtml(main, forceNumber);
      forceNumber = false;

      if (main.comment) {
        html += `<div class="comment">${escapeHtml(main.comment)}</div>`;
        forceNumber = true;
      }

      for (const altPath of parent.children.slice(1)) {
        const alt = all[altPath];
        // A sideline's own first move carries a comment as often as any
        // other move does, and it has no parent chain to render it.
        const note = alt.comment
          ? `<div class="comment">${escapeHtml(alt.comment)}</div>` : "";
        html += `<div class="var">${moveHtml(alt, true)}${note}${renderFrom(altPath)}</div>`;
        forceNumber = true;
      }

      here = main.path;
    }
    return html;
  };

  const root = all[""];
  const intro = root && root.comment
    ? `<div class="comment">${escapeHtml(root.comment)}</div>` : "";
  const body = renderFrom("");
  holder.innerHTML = intro + `<div class="line">` +
    `<span class="mv ${state.path === "" ? "current" : ""}" data-path="">start</span> ` +
    (body || `<span class="dim">no moves yet &mdash; play one on the board or type a line below</span>`) +
    `</div>`;

  holder.querySelectorAll(".mv").forEach((element) => {
    element.addEventListener("click", () => goTo(element.dataset.path));
  });
  const current = holder.querySelector(".mv.current");
  if (current) current.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------ left panel

function iconButton(glyph, title, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = glyph;
  button.title = title;
  button.addEventListener("click", (event) => { event.stopPropagation(); handler(); });
  return button;
}

function renderLeftPanel() {
  const list = $("chapter-list");
  list.innerHTML = "";

  if (isUni()) {
    if (!state.uni) return;
    for (const recording of state.uni.recordings) {
      const li = document.createElement("li");
      if (recording.id === state.recordingId) li.classList.add("active");

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = recording.name;
      name.title = `${recording.moves} moves recorded`;
      name.addEventListener("click", () => openRecording(recording.id));
      li.appendChild(name);

      const tools = document.createElement("span");
      tools.className = "chapter-tools";
      tools.appendChild(iconButton("✎", "Rename", () => renameRecording(recording)));
      tools.appendChild(iconButton("✕", "Delete this recording",
        () => deleteRecording(recording)));
      li.appendChild(tools);
      list.appendChild(li);
    }
    return;
  }

  if (!state.rep) return;
  for (const chapter of state.rep.chapters) {
    const li = document.createElement("li");
    if (chapter.id === state.chapterId) li.classList.add("active");

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = chapter.name;
    name.title = `${chapter.stats.moves} moves, ${chapter.stats.branches} branches`;
    name.addEventListener("click", () => openChapter(chapter.id));
    li.appendChild(name);

    if (chapter.gaps) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = chapter.gaps;
      badge.title = `${chapter.gaps} positions with no reply of yours`;
      li.appendChild(badge);
    }
    if (chapter.dirty) {
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.title = chapter.lichessChapterId
        ? "changed since the last push"
        : "never pushed to Lichess";
      li.appendChild(dot);
    }

    const tools = document.createElement("span");
    tools.className = "chapter-tools";
    tools.appendChild(iconButton("✎", "Rename", () => renameChapter(chapter)));
    tools.appendChild(iconButton(
      chapter.orientation === "white" ? "⬜" : "⬛",
      `Board faces ${chapter.orientation} - click to flip`,
      () => flipChapter(chapter)));
    tools.appendChild(iconButton("✕", "Delete this chapter",
      () => deleteChapter(chapter)));
    li.appendChild(tools);

    list.appendChild(li);
  }
}

function renderHeader() {
  $("mode-rep").classList.toggle("active", !isUni());
  $("mode-uni").classList.toggle("active", isUni());
  $("picker-rep").classList.toggle("hidden", isUni());
  $("btn-assist").classList.toggle("hidden", !isUni());

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("hidden", !TABS_BY_MODE[state.mode].includes(tab.dataset.tab));
  });

  if (isUni()) {
    $("rep-name").textContent = "Universal book";
    const book = state.uni ? state.uni.book : null;
    const count = state.uni ? state.uni.recordings.length : 0;
    $("rep-meta").textContent = book
      ? `${count} ${count === 1 ? "recording" : "recordings"}, ` +
        `${book.positions} positions, ${book.moves} moves`
      : "";
    $("btn-add-chapter").textContent = "New recording";
    $("btn-add-chapter").disabled = false;
    $("pgn-link").href = "/api/universal/pgn";
    $("study-link").classList.add("hidden");
    $("dirty-flag").classList.add("hidden");
    $("btn-push").textContent = "Publish the book";
    $("btn-push").disabled = !count;
    $("btn-export").disabled = true;
    $("gap-count").textContent =
      state.doc && state.doc.gaps.length ? state.doc.gaps.length : "";
    return;
  }

  const select = $("rep-select");
  select.innerHTML = "";
  for (const item of state.list) {
    const option = document.createElement("option");
    option.value = item.slug;
    option.textContent = `${item.name} (${item.color}, ${item.chapterCount})`;
    if (state.rep && item.slug === state.rep.slug) option.selected = true;
    select.appendChild(option);
  }
  if (!state.list.length) {
    const option = document.createElement("option");
    option.textContent = "no repertoires yet";
    select.appendChild(option);
  }

  $("btn-add-chapter").textContent = "Add chapter";
  $("btn-push").textContent = "Push to Lichess";

  const badge = $("rep-color");
  if (state.rep) {
    badge.textContent = `you play ${state.rep.color}`;
    badge.className = "pill" + (state.rep.color === "black" ? " black" : "");
    $("rep-name").textContent = state.rep.name;
    const totals = state.rep.chapters.reduce((sum, c) => sum + c.stats.moves, 0);
    const gaps = state.rep.chapters.reduce((sum, c) => sum + c.gaps, 0);
    const count = state.rep.chapters.length;
    $("rep-meta").textContent =
      `${count} ${count === 1 ? "chapter" : "chapters"}, ${totals} moves` +
      (gaps ? `, ${gaps} ${gaps === 1 ? "gap" : "gaps"}` : "");
    $("pgn-link").href = `/api/repertoires/${state.rep.slug}/pgn`;
    const link = $("study-link");
    if (state.rep.lichess.url) {
      link.href = state.rep.lichess.url;
      link.classList.remove("hidden");
    } else {
      link.classList.add("hidden");
    }
    $("dirty-flag").classList.toggle("hidden", !state.rep.dirty);
    $("gap-count").textContent = gaps || "";
  } else {
    badge.textContent = "";
    $("rep-name").textContent = "No repertoire";
    $("rep-meta").textContent = "Create one to begin.";
    $("gap-count").textContent = "";
  }

  const ready = Boolean(state.rep && state.rep.chapters.length);
  $("btn-push").disabled = !ready;
  $("btn-export").disabled = !ready;
  $("btn-add-chapter").disabled = !state.rep;
}

function renderGit() {
  const pill = $("git-pill");
  const info = state.git;
  if (!info) { pill.className = "pill git off"; pill.textContent = "git"; return; }

  let kind = "off";
  let label = "git off";
  if (!info.inRepo) {
    kind = "off"; label = "no git repo";
  } else if (!info.enabled) {
    kind = "off"; label = "auto-commit off";
  } else if (info.running || info.pending) {
    kind = "busy"; label = info.running ? "committing" : "saving soon";
  } else if (info.lastError) {
    kind = "bad"; label = "git problem";
  } else if (info.lastAction === "pushed") {
    kind = "ok"; label = "pushed";
  } else if (info.lastAction === "committed") {
    kind = "ok"; label = "committed";
  } else {
    kind = "ok"; label = info.push ? "auto-push on" : "auto-commit on";
  }
  pill.className = "pill git " + kind;
  pill.textContent = label;
  pill.title = info.lastError
    || (info.lastMessage ? `Last: ${info.lastMessage}` : "Saving to git");
}

function renderAll() {
  renderHeader();
  renderLeftPanel();
  renderTree();
  renderBoard();
  renderAnnotations();
  renderGit();
  renderSide();
  refreshEval();
  scheduleLines();
  refreshAssist();
}

// ------------------------------------------------------------- navigation

function goTo(path) {
  if (!nodes()[path]) return;
  state.path = path;
  state.pick = null;
  state.dests = [];
  renderTree();
  renderBoard();
  renderAnnotations();
  refreshEval();
  scheduleLines();
  refreshAssist();
  if (state.tab === "book") renderBook();
}

function stepForward() {
  const next = firstChild(state.path);
  if (next !== null) goTo(next);
}

function stepBack() {
  const up = parentPath(state.path);
  if (up !== null) goTo(up);
}

// ---------------------------------------------------------------- editing

function docUrl(suffix) {
  const base = isUni()
    ? `/api/universal/recordings/${state.recordingId}`
    : `/api/repertoires/${state.rep.slug}/chapters/${state.chapterId}`;
  return base + (suffix ? "/" + suffix : "");
}

function applyDoc(payload) {
  state.doc = payload;
  if (payload.focus && nodes()[payload.focus]) state.path = payload.focus;
  else if (!nodes()[state.path]) state.path = "";

  if (!isUni() && state.rep && payload.chapter) {
    // The chapter summary changed (counts, gaps, dirty flag), so refresh the
    // repertoire view without a second round trip for the tree.
    const index = state.rep.chapters.findIndex((c) => c.id === state.chapterId);
    if (index >= 0) state.rep.chapters[index] = payload.chapter;
    state.rep.dirty = state.rep.chapters.some((c) => c.dirty);
  }
  if (isUni() && state.uni && payload.recording) {
    const index = state.uni.recordings.findIndex((r) => r.id === state.recordingId);
    if (index >= 0) state.uni.recordings[index] = payload.recording;
    // The book changed, so anything derived from it is stale.
    refreshBook();
  }
  state.report = null;
  renderAll();
}

async function addLine() {
  if (!state.doc) return;
  const input = $("line-input");
  const text = input.value.trim();
  if (!text) return;
  const payload = await guard(() =>
    api("POST", docUrl("line"), { path: state.path, text }));
  if (!payload) return;
  input.value = "";
  const result = payload.result || {};
  applyDoc(payload);
  say(`Added ${result.added || 0} moves` +
      (result.existing ? `, ${result.existing} were already there` : "") + ".", "ok");
}

async function saveComment() {
  const item = node();
  if (!item) return;
  const text = $("comment").value;
  if (text === item.comment) return;
  const payload = await guard(() =>
    api("POST", docUrl("comment"), { path: state.path, text }));
  if (payload) applyDoc(payload);
}

async function toggleNag(code) {
  const payload = await guard(() =>
    api("POST", docUrl("nag"), { path: state.path, nag: code }));
  if (payload) applyDoc(payload);
}

async function deleteCurrent() {
  const item = node();
  if (!item || !item.san) return;
  if (!confirm(`Delete ${moveLabel(item, { force: true })} and everything after it?`)) return;
  const payload = await guard(() =>
    api("POST", docUrl("delete-node"), { path: state.path }));
  if (payload) applyDoc(payload);
}

async function promoteCurrent() {
  const item = node();
  if (!item || !item.san) return;
  const payload = await guard(() =>
    api("POST", docUrl("promote"), { path: state.path, toMain: true }));
  if (payload) {
    applyDoc(payload);
    say("That is now the move you play here.", "ok");
  }
}

// -------------------------------------------------------------- workspace

async function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.doc = null;
  state.path = "";
  state.report = null;
  state.scan = null;
  state.drill = null;
  state.assistInfo = null;
  localStorage.setItem("rc.mode", mode);
  if (!TABS_BY_MODE[mode].includes(state.tab)) state.tab = "moves";

  if (mode === "uni") {
    await loadUniversal();
    const first = state.uni && state.uni.recordings[0];
    if (first) await openRecording(first.id);
    else { setTab(state.tab); renderAll(); }
  } else {
    const slug = state.rep ? state.rep.slug : (state.list[0] && state.list[0].slug);
    if (slug) await openRepertoire(slug, state.chapterId);
    else { setTab(state.tab); renderAll(); }
  }
  setTab(state.tab);
}

// -------------------------------------------------------------- chapters

async function openRepertoire(slug, keepChapter) {
  const payload = await guard(() => api("GET", `/api/repertoires/${slug}`));
  if (!payload) return;
  state.rep = payload;
  localStorage.setItem("rc.slug", slug);
  state.report = null;
  state.scan = null;
  state.drill = null;
  const wanted = keepChapter && payload.chapters.some((c) => c.id === keepChapter)
    ? keepChapter
    : (payload.chapters[0] && payload.chapters[0].id);
  if (wanted) await openChapter(wanted);
  else { state.doc = null; renderAll(); }
}

async function openChapter(chapterId) {
  const payload = await guard(() =>
    api("GET", `/api/repertoires/${state.rep.slug}/chapters/${chapterId}`));
  if (!payload) return;
  state.chapterId = chapterId;
  state.doc = payload;
  state.path = "";
  state.pick = null;
  state.dests = [];
  state.flipped = payload.chapter.orientation === "black";
  renderAll();
}

async function addChapter() {
  if (isUni()) return addRecording();
  const name = prompt("Name for the new chapter");
  if (!name) return;
  const payload = await guard(() =>
    api("POST", `/api/repertoires/${state.rep.slug}/chapters`, { name }));
  if (!payload) return;
  state.rep = payload.repertoire;
  await openChapter(payload.chapterId);
}

async function renameChapter(chapter) {
  const name = prompt("Rename chapter", chapter.name);
  if (!name || name === chapter.name) return;
  const payload = await guard(() =>
    api("PATCH", `/api/repertoires/${state.rep.slug}/chapters/${chapter.id}`, { name }));
  if (payload) { state.rep = payload; renderAll(); }
}

async function flipChapter(chapter) {
  const orientation = chapter.orientation === "white" ? "black" : "white";
  const payload = await guard(() =>
    api("PATCH", `/api/repertoires/${state.rep.slug}/chapters/${chapter.id}`,
        { orientation }));
  if (!payload) return;
  state.rep = payload;
  if (chapter.id === state.chapterId) state.flipped = orientation === "black";
  renderAll();
}

async function deleteChapter(chapter) {
  if (!confirm(`Delete the chapter "${chapter.name}"? This cannot be undone.`)) return;
  const payload = await guard(() =>
    api("DELETE", `/api/repertoires/${state.rep.slug}/chapters/${chapter.id}`));
  if (!payload) return;
  state.rep = payload;
  if (chapter.id === state.chapterId) {
    state.chapterId = payload.chapters[0] ? payload.chapters[0].id : null;
    if (state.chapterId) await openChapter(state.chapterId);
    else { state.doc = null; renderAll(); }
  } else {
    renderAll();
  }
}

// ------------------------------------------------------------- universal

async function loadUniversal() {
  const payload = await guard(() => api("GET", "/api/universal"));
  if (payload) state.uni = payload;
}

async function refreshBook() {
  const payload = await guard(() => api("GET", "/api/universal"));
  if (!payload) return;
  if (state.uni) {
    state.uni.book = payload.book;
    state.uni.recordings = payload.recordings;
  } else {
    state.uni = payload;
  }
  renderHeader();
  if (state.tab === "book") renderBook();
}

async function openRecording(recordingId) {
  const payload = await guard(() =>
    api("GET", `/api/universal/recordings/${recordingId}`));
  if (!payload) return;
  state.recordingId = recordingId;
  state.doc = payload;
  state.path = "";
  state.pick = null;
  state.dests = [];
  localStorage.setItem("rc.recording", recordingId);
  renderAll();
}

async function addRecording() {
  const name = prompt("What is this sequence? (a name for the recording)",
                      "Session " + new Date().toISOString().slice(0, 10));
  if (!name) return;
  const payload = await guard(() =>
    api("POST", "/api/universal/recordings", { name }));
  if (!payload) return;
  state.uni.recordings = payload.recordings;
  await openRecording(payload.recordingId);
  say("Recording started. Every move you play is saved as you make it.", "ok");
}

async function renameRecording(recording) {
  const name = prompt("Rename recording", recording.name);
  if (!name || name === recording.name) return;
  const payload = await guard(() =>
    api("PATCH", `/api/universal/recordings/${recording.id}`, { name }));
  if (payload) { state.uni.recordings = payload.recordings; renderAll(); }
}

async function deleteRecording(recording) {
  if (!confirm(`Delete the recording "${recording.name}"? This cannot be undone.`)) return;
  const payload = await guard(() =>
    api("DELETE", `/api/universal/recordings/${recording.id}`));
  if (!payload) return;
  state.uni.recordings = payload.recordings;
  if (recording.id === state.recordingId) {
    const first = payload.recordings[0];
    if (first) await openRecording(first.id);
    else { state.doc = null; state.recordingId = null; renderAll(); }
  } else {
    await refreshBook();
    renderAll();
  }
}

function toggleAssist() {
  state.assist = !state.assist;
  localStorage.setItem("rc.assist", state.assist ? "1" : "0");
  const button = $("btn-assist");
  button.classList.toggle("on", state.assist);
  button.textContent = state.assist ? "Assist: on" : "Assist: off";
  refreshAssist();
  if (!state.assist) renderBoard();
}

async function renderBook() {
  const body = $("book-body");
  const item = node();
  if (!item) { body.innerHTML = ""; return; }

  const parent = item.parent === null ? null : nodes()[item.parent];
  const payload = await guard(() => api("POST", "/api/universal/lookup", {
    fen: item.fen, previousFen: parent ? parent.fen : null,
  }));
  if (!payload) return;

  const stats = state.uni ? state.uni.book : null;
  let html = stats
    ? `<p class="why">The book holds ${stats.positions} positions and ` +
      `${stats.moves} moves, from your recordings and every chapter of every ` +
      `repertoire. ${stats.branchPoints} of them ` +
      `${stats.branchPoints === 1 ? "has" : "have"} more than one move.</p>`
    : "";

  if (!payload.moves.length) {
    html += `<p class="empty">${payload.status === "gap"
      ? "Nothing recorded from here, and you arrived through your own lines. This is a gap."
      : "Nothing recorded from here, and no line of yours passes through this position."
    }</p>`;
    body.innerHTML = html;
    return;
  }

  body.innerHTML = html;
  for (const move of payload.moves) {
    const where = move.sources.map((source) =>
      `${source.kind === "chapter" ? "chapter" : "recording"}: ${source.name}`);
    const item2 = document.createElement("div");
    item2.className = "item";
    item2.innerHTML =
      `<div class="head"><span class="tagline info">${escapeHtml(move.san)}</span></div>` +
      `<div class="why">${escapeHtml(where.join(" · "))}</div>`;
    item2.addEventListener("click", () => playMove(move.uci));
    body.appendChild(item2);
  }
}

// ------------------------------------------------------------------- side

function setTab(name) {
  if (!TABS_BY_MODE[state.mode].includes(name)) name = "moves";
  state.tab = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  ["moves", "book", "gaps", "transpositions", "drill"].forEach((key) => {
    $("tab-" + key).classList.toggle("hidden", key !== name);
  });
  renderSide();
}

function renderSide() {
  $("scan-row").classList.toggle("hidden", isUni());
  if (state.tab === "gaps") renderGaps();
  else if (state.tab === "book") renderBook();
  else if (state.tab === "transpositions") renderTranspositions();
  else if (state.tab === "drill") renderDrill();
}

function listItem({ tag, tagClass, line, why, onClick }) {
  const item = document.createElement("div");
  item.className = "item";
  item.innerHTML =
    `<div class="head"><span class="tagline ${tagClass || "info"}">${escapeHtml(tag)}</span></div>` +
    `<div class="line">${escapeHtml(line)}</div>` +
    (why ? `<div class="why">${escapeHtml(why)}</div>` : "");
  if (onClick) item.addEventListener("click", onClick);
  return item;
}

function renderGaps() {
  const body = $("gaps-body");
  body.innerHTML = "";
  if (!state.doc) return;

  if (isUni()) {
    const gaps = state.doc.gaps;
    if (!gaps.length) {
      body.innerHTML =
        `<p class="empty">Every line in this recording continues somewhere in ` +
        `your book.</p>`;
      return;
    }
    for (const gap of gaps) {
      body.appendChild(listItem({
        tag: "book runs out", tagClass: "missing",
        line: `after ${gap.ply} ${gap.ply === 1 ? "move" : "moves"}, ${gap.turn} to play`,
        why: "Nothing anywhere in your book continues from this position.",
        onClick: () => { goTo(gap.path); setTab("moves"); },
      }));
    }
    return;
  }

  const structural = state.doc.gaps;
  if (!structural.length && !state.scan) {
    body.innerHTML =
      `<p class="empty">No holes in this chapter: every position where it is ` +
      `your turn has a move, and every choice is made.</p>`;
  }

  for (const gap of structural) {
    const tag = gap.kind === "missing" ? "no reply"
      : gap.kind === "undecided" ? "undecided" : "line stops early";
    const why = gap.kind === "missing"
      ? "You can be forced into this position and have nothing written down."
      : gap.kind === "undecided"
        ? `You kept several moves here: ${gap.moves.join(", ")}. The first is what drill mode asks for.`
        : "The line ends after only a few moves.";
    body.appendChild(listItem({
      tag, tagClass: gap.kind, line: gap.line, why,
      onClick: () => { goTo(gap.path); setTab("moves"); },
    }));
  }

  if (state.scan) {
    const heading = document.createElement("p");
    heading.className = "why";
    heading.textContent =
      `Explorer: ${state.scan.checked} positions checked` +
      (state.scan.rateLimited ? " (stopped early, rate limited)" : "");
    body.appendChild(heading);

    if (!state.scan.findings.length) {
      body.appendChild(Object.assign(document.createElement("p"), {
        className: "empty",
        textContent: "Nothing popular is missing at that threshold.",
      }));
    }
    for (const finding of state.scan.findings) {
      const missing = finding.missing
        .map((m) => `${m.san} (${(m.share * 100).toFixed(1)}%)`).join(", ");
      body.appendChild(listItem({
        tag: "uncovered reply", tagClass: "undecided",
        line: `${finding.chapterName}: ${finding.line}`,
        why: `They also play ${missing}. You cover ${finding.covered.join(", ")}.`,
        onClick: async () => {
          if (finding.chapterId !== state.chapterId) await openChapter(finding.chapterId);
          goTo(finding.path);
          setTab("moves");
        },
      }));
    }
  }
}

async function runScan() {
  const share = Math.max(0.1, parseFloat($("scan-share").value) || 2) / 100;
  const button = $("btn-scan");
  button.disabled = true;
  button.textContent = "Scanning...";
  const payload = await guard(() =>
    api("POST", `/api/repertoires/${state.rep.slug}/scan`,
        { chapterId: state.chapterId, minShare: share }));
  button.disabled = false;
  button.textContent = "Scan with the opening explorer";
  if (payload) { state.scan = payload; renderGaps(); }
}

async function renderTranspositions() {
  const body = $("trans-body");
  if (!state.rep) return;
  if (!state.report) {
    body.innerHTML = `<p class="empty">Looking for repeated positions...</p>`;
    const payload = await guard(() =>
      api("GET", `/api/repertoires/${state.rep.slug}/report`));
    if (!payload) return;
    state.report = payload;
  }

  body.innerHTML = "";
  const items = state.report.transpositions;
  if (!items.length) {
    body.innerHTML = `<p class="empty">No position in this repertoire is reached twice.</p>`;
    return;
  }

  for (const item of items) {
    const routes = item.occurrences
      .map((o) => `${o.chapterName}: ${o.line} → ${o.reply || "nothing"}`)
      .join("\n");
    const tag = item.conflict ? "conflicting answers"
      : item.deadEnd ? "answered in one place only" : "transposition";
    const why = item.conflict
      ? `The same position, two different moves of yours: ${item.moves.join(" and ")}. Decide which one you play.`
      : item.deadEnd
        ? "One route into this position has your move, the other stops here."
        : "Reached by more than one move order.";
    body.appendChild(listItem({
      tag,
      tagClass: item.conflict ? "missing" : item.deadEnd ? "undecided" : "info",
      line: routes, why,
      onClick: async () => {
        const first = item.occurrences[0];
        if (first.chapterId !== state.chapterId) await openChapter(first.chapterId);
        goTo(first.path);
        setTab("moves");
      },
    }));
  }
}

// ------------------------------------------------------------------ drill

async function renderDrill() {
  const body = $("drill-body");
  if (!state.rep) return;

  if (!state.drill) {
    const summary = await guard(() =>
      api("GET", `/api/repertoires/${state.rep.slug}/drill`));
    if (!summary) return;
    state.drill = { summary, cards: [], index: 0, card: null, done: 0, right: 0 };
  }

  const drill = state.drill;
  $("drill-count").textContent = drill.summary && drill.summary.due
    ? drill.summary.due : "";

  if (!drill.card) {
    const summary = drill.summary || { total: 0, new: 0, due: 0, scheduled: 0 };
    body.innerHTML =
      `<div class="drill-head">` +
      `<span class="drill-stat"><b>${summary.total}</b> questions</span>` +
      `<span class="drill-stat"><b>${summary.new}</b> new</span>` +
      `<span class="drill-stat"><b>${summary.due}</b> due</span>` +
      `<span class="drill-stat"><b>${summary.scheduled}</b> resting</span>` +
      `</div>` +
      (drill.done
        ? `<p class="why">Last session: ${drill.right} of ${drill.done} right.</p>` : "") +
      `<p class="why">The app plays the opponent. You play your own move on the ` +
      `board; anything else counts as a miss and comes back sooner.</p>`;
    const start = document.createElement("button");
    start.type = "button";
    start.className = "primary";
    start.textContent = summary.total ? "Start a session" : "Nothing to drill yet";
    start.disabled = !summary.total;
    start.addEventListener("click", startDrill);
    body.appendChild(start);

    const whole = document.createElement("label");
    whole.className = "inline small";
    whole.style.marginLeft = "10px";
    whole.innerHTML = `<input type="checkbox" id="drill-all"> whole repertoire`;
    body.appendChild(whole);
    return;
  }

  const card = drill.card;
  const total = drill.cards.length;
  body.innerHTML =
    `<div class="progress"><span style="width:${((drill.index) / total * 100).toFixed(0)}%"></span></div>` +
    `<div class="drill-card">` +
    `<div class="drill-prompt">Question ${drill.index + 1} of ${total}</div>` +
    `<div class="drill-line">${escapeHtml(card.chapterName)}: ${escapeHtml(card.line)}</div>` +
    `<div class="drill-feedback ${drill.verdict ? drill.verdict.kind : ""}">` +
    `${escapeHtml(drill.verdict ? drill.verdict.text : "")}</div>` +
    `<div class="drill-actions"></div></div>`;

  const actions = body.querySelector(".drill-actions");
  if (drill.verdict) {
    const next = document.createElement("button");
    next.type = "button";
    next.className = "primary";
    next.textContent = drill.index + 1 < total ? "Next" : "Finish";
    next.addEventListener("click", nextDrillCard);
    actions.appendChild(next);

    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "ghost";
    jump.textContent = "Show me in the tree";
    jump.addEventListener("click", async () => {
      await endDrill();
      if (card.chapterId !== state.chapterId) await openChapter(card.chapterId);
      goTo(card.path);
      setTab("moves");
    });
    actions.appendChild(jump);
  } else {
    const hint = document.createElement("button");
    hint.type = "button";
    hint.className = "ghost";
    hint.textContent = "I do not remember";
    hint.addEventListener("click", () => answerDrill(null));
    actions.appendChild(hint);
  }

  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "ghost";
  stop.textContent = "Stop";
  stop.addEventListener("click", endDrill);
  actions.appendChild(stop);
}

async function startDrill() {
  const all = $("drill-all") && $("drill-all").checked;
  const payload = await guard(() =>
    api("POST", `/api/repertoires/${state.rep.slug}/drill/session`,
        { chapterId: all ? null : state.chapterId, limit: 20 }));
  if (!payload || !payload.cards.length) {
    say("Nothing is due in that selection.", "ok");
    return;
  }
  state.drill = {
    summary: payload.summary, cards: payload.cards, index: 0,
    card: payload.cards[0], verdict: null, done: 0, right: 0,
  };
  state.flipped = payload.color === "black";
  state.pick = null;
  state.dests = [];
  renderBoard();
  renderEvalBar(null);
  renderDrill();
}

async function answerDrill(uci) {
  const drill = state.drill;
  if (!drill || !drill.card || drill.verdict) return;
  const card = drill.card;

  let kind = "wrong";
  let text;
  if (uci === null) {
    text = `The move is ${card.answerSan}.`;
  } else if (uci === card.answerUci || uci + "q" === card.answerUci) {
    kind = "right";
    text = `${card.answerSan} — yes.`;
    drill.right += 1;
  } else if (card.alternativeUcis.includes(uci)) {
    kind = "right";
    text = `That is a line you keep, but your main move here is ${card.answerSan}.`;
  } else {
    text = `Not that one. You play ${card.answerSan}.`;
  }
  if (card.comment && kind === "right") text += ` ${card.comment}`;

  drill.verdict = { kind, text };
  drill.done += 1;

  await guard(() =>
    api("POST", `/api/repertoires/${state.rep.slug}/drill/answer`, {
      key: card.key,
      correct: kind === "right",
      alternative: kind === "right" && uci !== card.answerUci,
      hinted: uci === null,
    }));

  const played = uci && kind === "right" ? uci : card.answerUci;
  $("board").src = boardUrl(card.fen, {
    arrows: [["green", played.slice(0, 2), played.slice(2, 4)]],
  });
  renderDrill();
}

function nextDrillCard() {
  const drill = state.drill;
  if (!drill) return;
  drill.index += 1;
  if (drill.index >= drill.cards.length) {
    endDrill();
    return;
  }
  drill.card = drill.cards[drill.index];
  drill.verdict = null;
  state.pick = null;
  state.dests = [];
  renderBoard();
  renderDrill();
}

async function endDrill() {
  const done = state.drill ? state.drill.done : 0;
  const right = state.drill ? state.drill.right : 0;
  const summary = await guard(() =>
    api("GET", `/api/repertoires/${state.rep.slug}/drill`));
  state.drill = { summary: summary || null, cards: [], index: 0, card: null,
                  done, right };
  if (state.doc && state.doc.chapter) {
    state.flipped = state.doc.chapter.orientation === "black";
  }
  renderBoard();
  renderAnnotations();
  refreshEval();
  renderDrill();
}

// --------------------------------------------------------------- dialogs

function openDialog(id) { $(id).showModal(); }

async function doNewRepertoire() {
  const name = $("new-name").value.trim();
  if (!name) return;
  const color = $("new-color").value;
  const payload = await guard(() =>
    api("POST", "/api/repertoires", { name, color }));
  if (!payload) return;
  await loadList();
  await openRepertoire(payload.slug);
  say(`Created ${payload.name}.`, "ok");
}

async function doImport() {
  const url = $("import-url").value.trim();
  const pgn = $("import-pgn").value.trim();
  const color = $("import-color").value;
  let payload = null;
  if (url) {
    payload = await guard(() => api("POST", "/api/import/study", { url, color }));
  } else if (pgn) {
    const name = $("import-name").value.trim() || "Imported repertoire";
    payload = await guard(() => api("POST", "/api/import/pgn", { pgn, name, color }));
  } else {
    say("Paste a study URL or some PGN.", "error");
    return;
  }
  if (!payload) return;
  await loadList();
  await openRepertoire(payload.slug);
  say(`Imported ${payload.chapterCount} chapters.`, "ok");
}

async function checkToken() {
  const info = await guard(() => api("GET", "/api/lichess/token"));
  const box = $("token-status");
  if (!info) return;
  if (!info.hasToken) {
    box.textContent = "No token yet. Pushing will not work.";
    box.className = "small dim";
  } else if (!info.canWrite) {
    box.textContent =
      `Token for ${info.userId}, but it lacks study:write, so it cannot push.`;
    box.className = "small";
    box.style.color = "var(--danger)";
  } else {
    box.textContent = `Ready: ${info.userId}, scopes ${info.scopes.join(", ")}.`;
    box.className = "small";
    box.style.color = "var(--accent-dark)";
  }
  if (state.health) state.health.hasToken = info.hasToken;
}

async function useToken() {
  const token = $("token-input").value.trim();
  const info = await guard(() => api("POST", "/api/lichess/token", { token }));
  if (!info) return;
  $("token-input").value = "";
  await checkToken();
}

async function doPush() {
  const report = $("push-report");
  report.innerHTML = `<p class="empty">Pushing...</p>`;
  const payload = await guard(() =>
    api("POST", `/api/repertoires/${state.rep.slug}/push`, {
      force: $("push-force").checked,
      visibility: $("push-visibility").value,
    }));
  if (!payload) { report.innerHTML = ""; return; }

  state.rep = payload.repertoire;
  renderHeader();
  renderLeftPanel();

  const marks = { created: "created", updated: "updated", skipped: "unchanged", failed: "FAILED" };
  report.innerHTML =
    `<p class="why">${payload.created} created, ${payload.updated} updated, ` +
    `${payload.skipped} unchanged, ${payload.failed} failed.</p>` +
    payload.chapters.map((c) =>
      `<div class="item"><div class="head"><span class="tagline ` +
      `${c.action === "failed" ? "missing" : "info"}">${marks[c.action]}</span></div>` +
      `<div class="line">${escapeHtml(c.name)}</div>` +
      (c.detail ? `<div class="why">${escapeHtml(c.detail)}</div>` : "") +
      `</div>`).join("") +
    (payload.studyUrl
      ? `<p><a class="link" target="_blank" rel="noopener" href="${payload.studyUrl}">` +
        `Open the study on Lichess</a></p>` : "");
}

async function doUniversalExport() {
  const report = $("uni-report");
  report.innerHTML = `<p class="empty">Publishing...</p>`;
  const payload = await guard(() => api("POST", "/api/universal/export", {
    name: $("uni-name").value.trim() || "Universal book",
    visibility: $("uni-visibility").value,
  }));
  if (!payload) { report.innerHTML = ""; return; }

  report.innerHTML =
    `<p class="why">${payload.created} created, ${payload.failed} failed.</p>` +
    payload.chapters.map((c) =>
      `<div class="item"><div class="head"><span class="tagline ` +
      `${c.action === "failed" ? "missing" : "info"}">${c.action}</span></div>` +
      `<div class="line">${escapeHtml(c.name)}</div>` +
      (c.detail ? `<div class="why">${escapeHtml(c.detail)}</div>` : "") +
      `</div>`).join("") +
    `<p><a class="link" target="_blank" rel="noopener" href="${payload.studyUrl}">` +
    `Open the study on Lichess</a></p>`;
}

async function doExport() {
  const body = {
    mode: $("export-mode").value,
    showEvals: $("export-evals").checked,
    includeNotation: $("export-notation").checked,
    includeSteps: $("export-steps").checked,
  };
  say("Building the PDF...");
  try {
    const response = await fetch(`/api/repertoires/${state.rep.slug}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try { const payload = await response.json(); if (payload.detail) message = payload.detail; }
      catch (_) { /* keep the status line */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${state.rep.slug}.pdf`;
    link.click();
    URL.revokeObjectURL(url);
    say("PDF downloaded.", "ok");
  } catch (error) {
    say(error.message, "error");
  }
}

// -------------------------------------------------------------------- git

function fillGitDialog() {
  const info = state.git || {};
  $("git-enabled").checked = Boolean(info.enabled);
  $("git-push").checked = Boolean(info.push);
  $("git-remote").value = info.remote || "origin";
  $("git-branch").value = info.branch || "";
  $("git-debounce").value = info.debounceSeconds || 20;

  const report = $("git-report");
  if (!info.inRepo) {
    report.innerHTML =
      `<span style="color:var(--danger)">The repertoires folder is not inside a ` +
      `git repository, so there is nothing to commit to.</span>`;
  } else if (info.lastError) {
    report.innerHTML = `<span style="color:var(--danger)">${escapeHtml(info.lastError)}</span>`;
  } else if (info.lastAction) {
    report.textContent =
      `Last ${info.lastAction}${info.lastMessage ? ": " + info.lastMessage : ""}. ` +
      `${info.commits} ${info.commits === 1 ? "commit" : "commits"} this session.`;
  } else {
    report.textContent = `Repository: ${info.repo || "unknown"}` +
      (info.hasRemote ? "" : ` (no remote called ${info.remote})`);
  }
}

async function refreshGit() {
  const info = await guard(() => api("GET", "/api/git"));
  if (info) { state.git = info; renderGit(); }
  return info;
}

async function saveGit() {
  const info = await guard(() => api("POST", "/api/git", {
    enabled: $("git-enabled").checked,
    push: $("git-push").checked,
    remote: $("git-remote").value.trim() || "origin",
    branch: $("git-branch").value.trim(),
    debounceSeconds: parseFloat($("git-debounce").value) || 20,
  }));
  if (info) { state.git = info; renderGit(); fillGitDialog(); }
}

async function commitNow() {
  const button = $("git-now");
  button.disabled = true;
  button.textContent = "Committing...";
  const info = await guard(() => api("POST", "/api/git/commit"));
  button.disabled = false;
  button.textContent = "Commit now";
  if (info) { state.git = info; renderGit(); fillGitDialog(); }
}

// -------------------------------------------------------------- keyboard

function onKeyDown(event) {
  const tag = (event.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") {
    if (event.key === "Enter" && event.target.id === "line-input") {
      event.preventDefault();
      addLine();
    }
    return;
  }
  if (event.target.closest && event.target.closest("dialog")) return;
  if (state.drill && state.drill.card) return;

  switch (event.key) {
    case "ArrowRight": case " ": event.preventDefault(); stepForward(); break;
    case "ArrowLeft": event.preventDefault(); stepBack(); break;
    case "Home": goTo(""); break;
    case "End": goTo(lastOfLine(state.path)); break;
    case "f": case "F": flipBoard(); break;
    case "Delete": case "Backspace": event.preventDefault(); deleteCurrent(); break;
    case "m": case "M": promoteCurrent(); break;
    case "a": case "A": if (isUni()) toggleAssist(); break;
    default: break;
  }
}

function flipBoard() {
  state.flipped = !state.flipped;
  renderBoard();
  renderEvalBar(state.evalCache.get(currentFen()) || (node() && node().eval) || null);
}

// ------------------------------------------------------------------- boot

async function loadList() {
  const payload = await guard(() => api("GET", "/api/repertoires"));
  if (payload) state.list = payload.repertoires;
}

function wire() {
  buildSquares();

  $("mode-rep").addEventListener("click", () => setMode("rep"));
  $("mode-uni").addEventListener("click", () => setMode("uni"));

  $("rep-select").addEventListener("change", (event) => openRepertoire(event.target.value));
  $("btn-new").addEventListener("click", () => openDialog("dlg-new"));
  $("btn-import").addEventListener("click", () => openDialog("dlg-import"));
  $("btn-token").addEventListener("click", () => { openDialog("dlg-token"); checkToken(); });
  $("git-pill").addEventListener("click", async () => {
    await refreshGit();
    fillGitDialog();
    openDialog("dlg-git");
  });
  $("btn-push").addEventListener("click", () => {
    if (isUni()) { $("uni-report").innerHTML = ""; openDialog("dlg-uniexport"); return; }
    $("push-report").innerHTML = "";
    $("push-target").textContent = state.rep.lichess.url
      ? `Updating ${state.rep.lichess.url}`
      : "This will create a new study on Lichess.";
    $("push-visibility").value = state.rep.lichess.visibility || "unlisted";
    $("push-visibility").disabled = Boolean(state.rep.lichess.studyId);
    openDialog("dlg-push");
  });
  $("btn-export").addEventListener("click", () => openDialog("dlg-export"));

  $("btn-add-chapter").addEventListener("click", addChapter);
  $("btn-line").addEventListener("click", addLine);
  $("btn-first").addEventListener("click", () => goTo(""));
  $("btn-prev").addEventListener("click", stepBack);
  $("btn-next").addEventListener("click", stepForward);
  $("btn-last").addEventListener("click", () => goTo(lastOfLine(state.path)));
  $("btn-flip").addEventListener("click", flipBoard);
  $("btn-delete").addEventListener("click", deleteCurrent);
  $("btn-promote").addEventListener("click", promoteCurrent);
  $("btn-assist").addEventListener("click", toggleAssist);
  $("comment").addEventListener("blur", saveComment);
  $("btn-scan").addEventListener("click", runScan);

  $("lines-count").addEventListener("change", () => {
    localStorage.setItem("rc.lines", $("lines-count").value);
    state.lines = null;
    refreshLines();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setTab(tab.dataset.tab));
  });

  $("new-ok").addEventListener("click", () => setTimeout(doNewRepertoire, 0));
  $("import-ok").addEventListener("click", () => setTimeout(doImport, 0));
  $("token-ok").addEventListener("click", (event) => {
    event.preventDefault();          // keep the dialog open to show the result
    useToken();
  });
  $("push-ok").addEventListener("click", (event) => {
    event.preventDefault();
    doPush();
  });
  $("uni-ok").addEventListener("click", (event) => {
    event.preventDefault();
    doUniversalExport();
  });
  $("git-ok").addEventListener("click", (event) => {
    event.preventDefault();
    saveGit();
  });
  $("git-now").addEventListener("click", commitNow);
  $("export-ok").addEventListener("click", () => setTimeout(doExport, 0));

  document.addEventListener("keydown", onKeyDown);

  // The commit timer runs on the server; poll gently so the pill is honest
  // about what has actually happened.
  setInterval(refreshGit, 15000);
}

async function init() {
  wire();
  state.health = await guard(() => api("GET", "/api/health"));

  if (state.health) {
    // Inset the click overlay by the SVG's own coordinate margin, or every
    // square is off by a few per cent. See board.margin_fraction.
    document.documentElement.style.setProperty(
      "--board-margin", `${(state.health.boardMargin || 0) * 100}%`);

    $("token-link").href = state.health.scopeUrl;
    $("lines-count").max = state.health.maxLines || 5;
    $("lines-count").value = localStorage.getItem("rc.lines") || "2";
    state.git = state.health.git;

    const modes = $("export-mode");
    modes.innerHTML = "";
    for (const [key, label] of Object.entries(state.health.pdfModes || {})) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = label;
      if (key === "book" && !state.health.latex) option.disabled = true;
      modes.appendChild(option);
    }
    const notes = [];
    if (!state.health.sibling) {
      notes.push("PDF export and the engine need the sibling app: " +
                 "pip install -e Lichess-Study-to-PDF");
    } else if (!state.health.stockfish) {
      notes.push("No Stockfish found, so there is no eval bar or engine " +
                 "suggestions. Put a binary in Lichess-Study-to-PDF/engine/.");
    }
    if (!state.health.latex) notes.push("Book mode needs a LaTeX install.");
    $("export-note").textContent = notes.join(" ");
    if (notes.length && !state.health.stockfish) say(notes[0]);
  }

  state.assist = localStorage.getItem("rc.assist") === "1";
  if (state.assist) {
    $("btn-assist").classList.add("on");
    $("btn-assist").textContent = "Assist: on";
  }

  await loadList();
  const wantUniversal = localStorage.getItem("rc.mode") === "uni";

  if (wantUniversal) {
    state.mode = "uni";
    await loadUniversal();
    const remembered = localStorage.getItem("rc.recording");
    const found = state.uni && state.uni.recordings.find((r) => r.id === remembered);
    const first = found || (state.uni && state.uni.recordings[0]);
    if (first) await openRecording(first.id);
    else renderAll();
  } else {
    const remembered = localStorage.getItem("rc.slug");
    const slug = state.list.some((r) => r.slug === remembered)
      ? remembered
      : (state.list[0] && state.list[0].slug);
    if (slug) await openRepertoire(slug);
    else renderAll();
  }
  setTab(state.tab);
}

init();
