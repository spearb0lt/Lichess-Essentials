/* Lichess Study to PDF -- browser interface.
 *
 * What this does that chesspaper.me does not:
 *   1. every move (main line, sidelines, comments) is on screen from the start,
 *      with no per-node "toggle diagram" clicking;
 *   2. the board steps along whichever line you are actually in, from the
 *      keyboard -- space or the arrow keys;
 *   3. the eval bar updates for the position you are looking at, immediately,
 *      instead of waiting for the whole chapter to be analysed;
 *   4. you can pick pieces up and play your own moves from any position, and
 *      keep getting evaluations while you do.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const FILES = "abcdefgh";

const state = {
  key: null,
  study: null,
  loadedUrl: null,      // the URL as typed: a chapter URL must stay one
  chapter: null,
  chapterIndex: 0,
  stepIndex: 0,
  flipped: false,
  evals: {},            // fen -> {cp, mate, depth, source}
  autoplay: null,
  free: null,           // {fen, lastMove, history: [...], fromStep}
  selected: null,       // square currently picked up
  legal: {},            // square -> [destinations]
  analyseToken: 0,      // cancels background analysis on chapter change
  evalToken: 0,         // cancels a stale single-position eval
  hasEngine: false,
};

const boardCache = new Map();

/* ------------------------------------------------------------ utilities */

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function showStatus(message, kind) {
  const node = $("status");
  node.textContent = message || "";
  node.className = "status" + (kind === "info" ? " info" : "");
  node.classList.toggle("hidden", !message);
}

/** ``s3`` for the third sideline of the chapter, ``main`` otherwise. */
function sidelineTag(branch) {
  return branch ? `s${branch}` : "main";
}

/** A sideline's {ink, rule, tint}, or null for the main line.
 *
 *  The palette comes from the server so the page and the PDF writers paint
 *  the same colours -- see sidelines.py.
 */
function sidelineColor(branch) {
  const palette = state.study && state.study.sidelinePalette;
  if (!branch || !palette || !palette.length) return null;
  return palette[(branch - 1) % palette.length];
}

/** Paint (or clear) one sideline's colour on an element and its children. */
function applySidelineColor(node, color) {
  if (!node) return;
  node.classList.toggle("side", !!color);
  for (const [name, value] of [["--side-ink", color && color.ink],
                               ["--side-rule", color && color.rule],
                               ["--side-tint", color && color.tint]]) {
    if (value) node.style.setProperty(name, value);
    else node.style.removeProperty(name);
  }
}

/** The position currently on the board: a study step, or a free-play move. */
function currentPosition() {
  if (state.free) {
    return {
      fen: state.free.fen,
      uci: state.free.lastUci || "",
      circles: [],
      arrows: [],
      free: true,
    };
  }
  const step = state.chapter.steps[state.stepIndex];
  return {
    fen: step.fen, uci: step.uci,
    circles: step.circles, arrows: step.arrows, free: false,
  };
}

function boardUrl(position, size, coords) {
  const circles = (position.circles || []).map((c) => c.join(":")).join(",");
  const arrows = (position.arrows || []).map((a) => a.join(":")).join(",");
  const params = new URLSearchParams({
    fen: position.fen,
    flip: state.flipped ? "1" : "0",
    size: String(size || 480),
    coords: coords === false ? "0" : "1",
  });
  if (position.uci) params.set("lastmove", position.uci);
  if (circles) params.set("circles", circles);
  if (arrows) params.set("arrows", arrows);
  return "/api/board?" + params.toString();
}

function prefetch(index) {
  const chapter = state.chapter;
  if (!chapter || index == null || index < 0 || index >= chapter.steps.length) return;
  const step = chapter.steps[index];
  const url = boardUrl({ fen: step.fen, uci: step.uci,
                         circles: step.circles, arrows: step.arrows }, 480, false);
  if (boardCache.has(url)) return;
  const img = new Image();
  img.src = url;
  boardCache.set(url, img);
  if (boardCache.size > 240) boardCache.delete(boardCache.keys().next().value);
}

/* --------------------------------------------------------- move stepping */

function nextIndex(index) {
  const kids = state.chapter.children[String(index)] || [];
  if (!kids.length) return null;
  for (const child of kids) {
    if (!state.chapter.steps[child].startsVariation) return child;
  }
  return kids[0];
}

function prevIndex(index) {
  const step = state.chapter.steps[index];
  if (!step || step.line.length < 2) return null;
  return step.line[step.line.length - 2];
}

function lastIndexOfLine(index) {
  let current = index;
  for (let guard = 0; guard < 5000; guard += 1) {
    const next = nextIndex(current);
    if (next == null) return current;
    current = next;
  }
  return current;
}

function goTo(index, options) {
  if (!state.chapter || index == null) return;
  state.free = null;
  state.selected = null;
  const clamped = Math.max(0, Math.min(state.chapter.steps.length - 1, index));
  state.stepIndex = clamped;
  renderPosition();
  if (!options || options.scroll !== false) scrollMoveIntoView();
  prefetch(nextIndex(clamped));
  prefetch(prevIndex(clamped));
}

function step(delta) {
  if (state.free) {
    if (delta < 0) { undoFreeMove(); return; }
    return;                      // nothing to step forward into off-line
  }
  const target = delta > 0 ? nextIndex(state.stepIndex) : prevIndex(state.stepIndex);
  if (target == null) { stopAutoplay(); return; }
  goTo(target);
}

function stopAutoplay() {
  if (state.autoplay) {
    clearInterval(state.autoplay);
    state.autoplay = null;
    $("btn-play").classList.remove("primary");
  }
}

function toggleAutoplay() {
  if (state.autoplay) { stopAutoplay(); return; }
  $("btn-play").classList.add("primary");
  state.autoplay = setInterval(() => step(1), 1100);
}

/* ------------------------------------------------------------- free play */

async function playMove(from, to) {
  const position = currentPosition();
  try {
    const data = await postJson("/api/play", {
      fen: position.fen, from_: from, to: to,
    });
    if (!data.legal) return false;

    if (!state.free) {
      state.free = { history: [], fromStep: state.stepIndex };
    }
    state.free.history.push({ san: data.san, fen: data.fen, uci: data.uci });
    state.free.fen = data.fen;
    state.free.lastUci = data.uci;
    state.selected = null;
    renderPosition();
    return true;
  } catch (error) {
    showStatus(error.message);
    return false;
  }
}

function undoFreeMove() {
  if (!state.free) return;
  state.free.history.pop();
  if (!state.free.history.length) {
    returnToLine();
    return;
  }
  const last = state.free.history[state.free.history.length - 1];
  state.free.fen = last.fen;
  state.free.lastUci = last.uci;
  state.selected = null;
  renderPosition();
}

function returnToLine() {
  const back = state.free ? state.free.fromStep : state.stepIndex;
  state.free = null;
  state.selected = null;
  goTo(back);
}

async function onSquareClick(square) {
  stopAutoplay();
  const position = currentPosition();

  if (state.selected && (state.legal[state.selected] || []).includes(square)) {
    await playMove(state.selected, square);
    return;
  }

  // Otherwise treat it as picking a piece up.
  try {
    const params = new URLSearchParams({ fen: position.fen, square });
    const response = await fetch("/api/legal?" + params.toString());
    const data = await response.json();
    const targets = (data.moves || {})[square] || [];
    state.selected = targets.length ? square : null;
    state.legal = data.moves || {};
    paintSquares();
  } catch (error) {
    state.selected = null;
    paintSquares();
  }
}

/* ------------------------------------------------------------- rendering */

function buildSquares() {
  const grid = $("squares");
  grid.innerHTML = "";
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const cell = document.createElement("div");
      cell.className = "sq";
      grid.appendChild(cell);
    }
  }
  grid.addEventListener("click", (event) => {
    const cells = Array.from(grid.children);
    const index = cells.indexOf(event.target);
    if (index < 0) return;
    onSquareClick(squareNameAt(index));
  });
}

/** Grid cell index -> square name, honouring board orientation. */
function squareNameAt(index) {
  const row = Math.floor(index / 8);
  const col = index % 8;
  const file = state.flipped ? 7 - col : col;
  const rank = state.flipped ? row : 7 - row;
  return FILES[file] + (rank + 1);
}

function paintSquares() {
  const cells = Array.from($("squares").children);
  const targets = state.selected ? (state.legal[state.selected] || []) : [];
  cells.forEach((cell, index) => {
    const name = squareNameAt(index);
    cell.className = "sq";
    if (state.selected === name) cell.classList.add("selected");
    else if (targets.includes(name)) cell.classList.add("target");
  });
}

function renderPosition() {
  const position = currentPosition();
  $("board").src = boardUrl(position, 480, false);

  const label = $("move-label");
  const lineLabel = $("line-label");

  if (position.free) {
    const moves = state.free.history.map((h) => h.san);
    label.textContent = moves[moves.length - 1] || "Your move";
    label.classList.add("variation");
    applySidelineColor($("side-chip"), null);
    lineLabel.textContent = "your own line: " + moves.join(" ");
    $("comment").textContent = "";
    $("free-banner").classList.remove("hidden");
    $("free-text").textContent =
      `Off the study line — ${moves.length} move${moves.length === 1 ? "" : "s"} played by hand`;
  } else {
    const current = state.chapter.steps[state.stepIndex];
    label.textContent = current.san ? current.label : "Starting position";
    label.classList.toggle("variation", current.depth > 0);
    applySidelineColor($("side-chip"), sidelineColor(current.branch));
    lineLabel.textContent = current.depth
      ? `${sidelineTag(current.branch)} · sideline (depth ${current.depth})`
        + ` — ${current.lineLabel}`
      : "Main line";
    $("comment").textContent = current.comment || "";
    $("free-banner").classList.add("hidden");

    document.querySelectorAll(".mv.current").forEach((n) => n.classList.remove("current"));
    const node = document.querySelector(`.mv[data-index="${state.stepIndex}"]`);
    if (node) node.classList.add("current");
  }

  paintSquares();
  renderEvalBar(state.evals[position.fen]);
  requestEval(position.fen);

  $("btn-prev").disabled = !state.free && prevIndex(state.stepIndex) == null;
  $("btn-next").disabled = !!state.free || nextIndex(state.stepIndex) == null;
}

function scrollMoveIntoView() {
  const node = document.querySelector(`.mv[data-index="${state.stepIndex}"]`);
  if (node) node.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function evalFraction(ev) {
  if (!ev) return 0.5;
  if (ev.mate != null) return ev.mate > 0 ? 1 : 0;
  if (ev.cp == null) return 0.5;
  const capped = Math.max(-1500, Math.min(1500, ev.cp));
  const chances = 2 / (1 + Math.exp(-0.00368208 * capped)) - 1;
  return Math.max(0, Math.min(1, (chances + 1) / 2));
}

function evalText(ev) {
  if (!ev) return "";
  if (ev.mate != null) return (ev.mate > 0 ? "+M" : "-M") + Math.abs(ev.mate);
  if (ev.cp == null) return "";
  return (ev.cp >= 0 ? "+" : "") + (ev.cp / 100).toFixed(2);
}

function renderEvalBar(ev) {
  const fraction = evalFraction(ev);
  const fill = $("evalfill");
  fill.style.height = (fraction * 100).toFixed(1) + "%";
  fill.style.bottom = state.flipped ? "auto" : "0";
  fill.style.top = state.flipped ? "0" : "auto";
  $("evaltext").textContent = evalText(ev);

  const line = $("engine-line");
  if (ev && (ev.cp != null || ev.mate != null)) {
    const best = ev.best_move ? ` · best ${ev.best_move}` : "";
    line.textContent = `${evalText(ev)} · depth ${ev.depth} · ${ev.source}${best}`;
  } else {
    line.textContent = state.hasEngine ? "evaluating…" : "";
  }
}

/** Evaluate just the position on the board. This is the fast path. */
async function requestEval(fen) {
  if (state.evals[fen]) { renderEvalBar(state.evals[fen]); return; }
  const token = ++state.evalToken;
  try {
    const data = await postJson("/api/eval", { fen, movetime: 0.2 });
    if (token !== state.evalToken) return;      // user already moved on
    const ev = data.eval;
    if (ev && (ev.cp != null || ev.mate != null)) {
      state.evals[fen] = ev;
      renderEvalBar(ev);
      renderGraph();
    } else {
      renderEvalBar(null);
    }
  } catch (error) {
    /* a failed eval must never break navigation */
  }
}

/** Fill the whole chapter in the background so the graph has a shape. */
async function backgroundAnalyse(chapter) {
  const token = ++state.analyseToken;
  const fens = chapter.steps.map((s) => s.fen).filter((f) => !state.evals[f]);
  if (!fens.length) { $("analyse-state").textContent = ""; return; }

  const CHUNK = 24;
  for (let i = 0; i < fens.length; i += CHUNK) {
    if (token !== state.analyseToken) return;
    $("analyse-state").textContent = `analysing ${Math.min(i + CHUNK, fens.length)}/${fens.length}`;
    try {
      const data = await postJson("/api/evals", {
        fens: fens.slice(i, i + CHUNK), movetime: 0.1,
      });
      if (token !== state.analyseToken) return;
      Object.assign(state.evals, data.evals);
      state.hasEngine = !!data.hasEngine;
      renderGraph();
    } catch (error) {
      $("analyse-state").textContent = "analysis unavailable";
      return;
    }
  }
  $("analyse-state").textContent = "";
  renderEvalBar(state.evals[currentPosition().fen]);
}

/** Chess-book layout: everything visible, sidelines indented, all clickable. */
function renderMoves() {
  const chapter = state.chapter;
  const container = $("moves");
  const blocks = [];
  let current = null;
  let depth = 0;
  let branch = 0;
  let forceNumber = true;

  const flush = () => {
    if (current && current.parts.length) blocks.push(current);
    current = null;
  };

  const intro = chapter.steps[0];
  if (intro && intro.comment) {
    blocks.push({ depth: 0, parts: [`<span class="cmt">${escapeHtml(intro.comment)}</span>`] });
  }

  for (let i = 1; i < chapter.steps.length; i += 1) {
    const s = chapter.steps[i];
    if (!current || s.depth !== depth || s.branch !== branch
        || s.startsVariation) {
      flush();
      depth = s.depth;
      branch = s.branch || 0;
      current = { depth, branch, parts: [] };
      forceNumber = true;
      // Name the sideline where it opens, so the colour has a label.
      if (branch && s.startsVariation) {
        current.parts.push(`<span class="stag">${sidelineTag(branch)}</span>`);
      }
    }

    let number = "";
    if (s.whiteMoved) number = `${s.moveNumber}.`;
    else if (forceNumber) number = `${s.moveNumber}...`;

    const classes = ["mv", s.depth === 0 ? "d0" : "var"];
    current.parts.push(
      `<span class="${classes.join(" ")}" data-index="${s.index}" data-fen="${escapeHtml(s.fen)}" data-uci="${escapeHtml(s.uci)}">` +
      `${escapeHtml(number)}${escapeHtml(s.san)}${escapeHtml(s.nags)}</span>`
    );
    forceNumber = false;

    if (s.comment) {
      current.parts.push(`<span class="cmt">${escapeHtml(s.comment)}</span>`);
      forceNumber = true;
    } else if (s.circles.length || s.arrows.length) {
      current.parts.push('<span class="dot" title="has board annotations">&#9679;</span>');
    }
  }
  flush();

  container.innerHTML = blocks
    .map((b) => {
      const color = sidelineColor(b.branch);
      const tones = color
        ? ` style="--side-ink:${color.ink};--side-rule:${color.rule};`
          + `--side-tint:${color.tint}"`
        : "";
      return `<div class="blk${color ? " side" : ""}" `
        + `data-depth="${b.depth}"${tones}>${b.parts.join(" ")}</div>`;
    })
    .join("");

  container.querySelectorAll(".mv").forEach((node) => {
    node.addEventListener("click", () => {
      stopAutoplay();
      goTo(Number(node.dataset.index), { scroll: false });
    });
    node.addEventListener("mouseenter", () => showPreview(node));
    node.addEventListener("mouseleave", hidePreview);
  });
}

/* ------------------------------------------------------- hover preview */

let previewTimer = null;

function showPreview(node) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const box = $("preview");
    const url = boardUrl({
      fen: node.dataset.fen, uci: node.dataset.uci,
      circles: [], arrows: [],
    }, 240, false);
    $("preview-img").src = url;

    const rect = node.getBoundingClientRect();
    const width = 218;
    let left = rect.left - width - 12;
    if (left < 8) left = rect.right + 12;
    let top = rect.top - 90;
    top = Math.max(8, Math.min(window.innerHeight - 230, top));
    box.style.left = left + "px";
    box.style.top = top + "px";
    box.classList.remove("hidden");
  }, 130);
}

function hidePreview() {
  clearTimeout(previewTimer);
  $("preview").classList.add("hidden");
}

/* --------------------------------------------------------- chapters */

function renderChapters() {
  const list = $("chapter-list");
  list.innerHTML = state.study.chapters
    .map((c, i) => (
      `<li data-index="${i}" class="${i === state.chapterIndex ? "active" : ""}">` +
      `<span>${escapeHtml(`${i + 1}. ${c.name}`)}</span>` +
      `<span class="count">${c.moveCount}</span></li>`
    ))
    .join("");
  list.querySelectorAll("li").forEach((node) => {
    node.addEventListener("click", () => selectChapter(Number(node.dataset.index)));
  });
}

function selectChapter(index) {
  stopAutoplay();
  state.chapterIndex = index;
  state.chapter = state.study.chapters[index];
  state.flipped = state.chapter.orientation === "black";
  state.stepIndex = 0;
  state.free = null;
  state.selected = null;
  boardCache.clear();

  $("chapter-name").textContent = `${index + 1}. ${state.chapter.name}`;
  renderChapters();
  renderMoves();
  renderPosition();
  renderGraph();
  $("moves").scrollTop = 0;
  backgroundAnalyse(state.chapter);
}

/* ------------------------------------------------------------ eval graph */

function mainlineSteps() {
  const out = [];
  let index = 0;
  for (let guard = 0; guard < 5000; guard += 1) {
    out.push(state.chapter.steps[index]);
    const next = nextIndex(index);
    if (next == null) break;
    index = next;
  }
  return out;
}

function renderGraph() {
  const svg = $("evalgraph");
  if (!state.chapter) return;
  const steps = mainlineSteps();
  const known = steps.filter((s) => state.evals[s.fen]);

  if (known.length < 2) {
    svg.innerHTML = "";
    $("graph-note").textContent = "Evaluation graph — filling in…";
    return;
  }
  $("graph-note").textContent =
    `Evaluation across the main line (${known.length}/${steps.length} analysed).`;

  const width = 100;
  const height = 40;
  const points = steps.map((s, i) => [
    (i / Math.max(1, steps.length - 1)) * width,
    height - evalFraction(state.evals[s.fen]) * height,
  ]);

  const area = `M0,${height / 2} ` +
    points.map((p) => `L${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ") +
    ` L${width},${height} L0,${height} Z`;
  const line = "M" + points.map((p) => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" L");

  const at = steps.findIndex((s) => s.index === state.stepIndex);
  const marker = at >= 0
    ? `<line x1="${((at / Math.max(1, steps.length - 1)) * width).toFixed(2)}" y1="0"
             x2="${((at / Math.max(1, steps.length - 1)) * width).toFixed(2)}" y2="${height}"
             stroke="#4a7c59" stroke-width="0.4"/>`
    : "";

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML =
    `<path d="${area}" fill="#dfe7d8"/>` +
    `<line x1="0" y1="${height / 2}" x2="${width}" y2="${height / 2}" stroke="#cfc8bd" stroke-width="0.3"/>` +
    `<path d="${line}" fill="none" stroke="#4a7c59" stroke-width="0.6"/>` + marker;

  svg.onclick = (event) => {
    const rect = svg.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const target = steps[Math.round(ratio * (steps.length - 1))];
    if (target) goTo(target.index);
  };
}

/* --------------------------------------------------------------- network */

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      if (data.detail) detail = data.detail;
    } catch (err) { /* keep the generic message */ }
    throw new Error(detail);
  }
  return response.json();
}

/* ------------------------------------------------------------- home page */

/** Read studies.txt through the API and draw it. Re-read on every open, so
 *  editing the file and refreshing is enough. */
async function showHome() {
  const home = $("home");
  home.classList.remove("hidden");
  try {
    const data = await (await fetch("/api/studies")).json();
    renderHome(data);
  } catch (error) {
    $("home-list").innerHTML =
      `<div class="home-empty">Could not read the studies list.</div>`;
  }
}

function renderHome(data) {
  const list = $("home-list");
  const sections = data.sections || [];

  $("home-note").textContent = data.count
    ? `${data.count} ${data.count === 1 ? "study" : "studies"} from ${data.path}`
      + " — edit that file to change this list."
    : `Add studies to ${data.path}, or load one and press ☆ Save.`;

  if (!data.count) {
    list.innerHTML = `<div class="home-empty">Nothing in the list yet. `
      + `Paste a study URL above, then press <b>☆ Save</b> to keep it here.</div>`;
  } else {
    list.innerHTML = sections.map((section) => {
      const cards = section.studies.map((study) => {
        const badge = study.viaChapter
          ? `<span class="badge" title="Opens through a chapter URL, which is `
            + `how a private study loads without a token">chapter link</span>`
          : "";
        return `<button class="study-card" data-url="${escapeHtml(study.url)}">`
          + `<span class="card-name">${escapeHtml(study.name)}</span>`
          + `<span class="card-meta">${escapeHtml(study.studyId)}${badge}</span>`
          + `</button>`;
      }).join("");
      const heading = section.heading
        ? `<h3>${escapeHtml(section.heading)}</h3>` : "";
      return `<div class="home-section">${heading}`
        + `<div class="card-row">${cards}</div></div>`;
    }).join("");
  }

  list.querySelectorAll(".study-card").forEach((card) => {
    card.addEventListener("click", () => {
      $("study-url").value = card.dataset.url;
      loadStudy(null);
    });
  });

  const problems = $("home-problems");
  const bad = data.problems || [];
  problems.classList.toggle("hidden", !bad.length);
  if (bad.length) {
    problems.textContent = `${bad.length} line${bad.length === 1 ? "" : "s"} in `
      + `the file could not be read (expected "Name | URL"): `
      + bad.map((p) => `line ${p.line}`).join(", ");
  }
}

/** Add the study now on screen to studies.txt, name and all. */
async function saveCurrentStudy() {
  if (!state.study) return;
  const button = $("save-star");
  button.disabled = true;
  try {
    const data = await postJson("/api/studies", {
      url: state.loadedUrl || state.study.url,
      name: state.study.name || "",
    });
    showStatus(data.added
      ? `Saved “${data.study.name}” to my studies.`
      : `“${data.study.name}” is already in my studies.`, "info");
    button.textContent = "★ Saved";
  } catch (error) {
    showStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadStudy(event) {
  if (event) event.preventDefault();
  const url = $("study-url").value.trim();
  if (!url) return;

  const token = $("token").value.trim();
  if (token) localStorage.setItem("lsp_token", token);

  $("load-btn").disabled = true;
  showStatus("Fetching study from Lichess…", "info");
  try {
    const data = await postJson("/api/study", { url, token: token || null });
    state.key = data.key;
    state.study = data;
    state.loadedUrl = url;
    state.evals = {};

    $("study-name").textContent = data.name;
    const moves = data.chapters.reduce((sum, c) => sum + c.moveCount, 0);
    const sidelines = data.chapters.reduce((sum, c) => sum + c.variationCount, 0);
    $("study-meta").textContent =
      `${data.chapters.length} chapters · ${moves} moves · ${sidelines} sidelines`;
    $("pgn-link").href = `/api/pgn/${data.key}`;

    $("app").classList.remove("hidden");
    $("home").classList.add("hidden");
    const star = $("save-star");
    star.classList.remove("hidden");
    star.textContent = "☆ Save";
    selectChapter(0);
    showStatus("");
  } catch (error) {
    showStatus(error.message);
    if (/private|token|chapter URL/i.test(error.message)) {
      $("token-row").classList.remove("hidden");
    }
  } finally {
    $("load-btn").disabled = false;
  }
}

/* ---------------------------------------------------------------- export */

function syncExportDialog() {
  const mode = $("opt-mode").value;
  const note = $("mode-note");
  const paged = mode !== "book";

  $("opts-paged").classList.toggle("hidden", false);
  $("chk-notation").classList.toggle("hidden", !paged);
  $("chk-steps").classList.toggle("hidden", !paged);
  $("chk-landscape").classList.toggle("hidden", !paged);

  if (mode === "acrobat") {
    note.className = "note warn";
    note.innerHTML =
      "<b>Read this before choosing it.</b> Only Adobe Acrobat Reader runs PDF " +
      "JavaScript. In Chrome, Edge, Firefox, Preview or on a phone this file " +
      "shows just the first position of each chapter and the buttons do " +
      "nothing — it will look broken. Pick Slideshow unless you " +
      "specifically read PDFs in Acrobat.";
  } else if (mode === "book") {
    note.className = "note";
    note.textContent =
      "Typeset with LaTeX: portrait, two columns, figurine notation and " +
      "printed-book diagrams. Needs pdflatex installed.";
  } else if (mode === "grid") {
    note.className = "note";
    note.textContent =
      "Twelve small boards to a page, in reading order, each with its move, " +
      "evaluation and comment. About twelve times shorter than Slideshow.";
  } else {
    note.className = "note";
    note.textContent =
      "One page per position, so your reader's next-page key steps the board " +
      "one move at a time. Long, but it works in every PDF viewer.";
  }

  $("eval-note").textContent = $("opt-evals").checked
    ? "Evaluations are computed on the server for every exported position, so " +
      "this can take a while on a big study."
    : "";
}

async function runExport() {
  const button = $("export-go");
  button.disabled = true;
  const useEvals = $("opt-evals").checked;
  $("export-status").textContent = useEvals
    ? "Analysing positions and building the PDF… this can take a minute."
    : "Building PDF…";

  const payload = {
    studyKey: state.key,
    mode: $("opt-mode").value,
    includeNotation: $("opt-notation").checked,
    includeSteps: $("opt-steps").checked,
    showEvals: useEvals,
    computeEvals: useEvals,
    diagrams: $("opt-diagrams").value === "auto"
      ? null : $("opt-diagrams").value,
    chapters: $("opt-chapters").value === "current" ? [state.chapterIndex] : null,
    landscapePages: $("opt-landscape").checked,
    evals: useEvals ? state.evals : null,
  };

  try {
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Export failed (${response.status})`);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = (state.study.name || "lichess-study").replace(/[^\w -]/g, "") + ".pdf";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 4000);

    $("export-status").textContent = "Done.";
    setTimeout(() => $("export-dialog").classList.add("hidden"), 700);
  } catch (error) {
    $("export-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ wire */

function onKeyDown(event) {
  if (!state.chapter) return;
  const tag = (event.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea") return;
  if (!$("export-dialog").classList.contains("hidden")) return;

  switch (event.key) {
    case " ": case "ArrowRight": case "ArrowDown":
      event.preventDefault(); stopAutoplay(); step(1); break;
    case "ArrowLeft": case "ArrowUp":
      event.preventDefault(); stopAutoplay(); step(-1); break;
    case "Home":
      event.preventDefault(); stopAutoplay(); goTo(0); break;
    case "End":
      event.preventDefault(); stopAutoplay(); goTo(lastIndexOfLine(0)); break;
    case "Escape":
      if (state.free) returnToLine();
      else { state.selected = null; paintSquares(); }
      break;
    case "f": case "F":
      state.flipped = !state.flipped; boardCache.clear(); renderPosition(); break;
    case "p": case "P":
      toggleAutoplay(); break;
    default: break;
  }
}

async function init() {
  buildSquares();

  $("load-form").addEventListener("submit", loadStudy);
  $("token-toggle").addEventListener("click",
    () => $("token-row").classList.toggle("hidden"));
  $("save-star").addEventListener("click", saveCurrentStudy);
  $("home-toggle").addEventListener("click", () => {
    if ($("home").classList.contains("hidden")) showHome();
    else $("home").classList.add("hidden");
  });

  $("btn-next").addEventListener("click", () => { stopAutoplay(); step(1); });
  $("btn-prev").addEventListener("click", () => { stopAutoplay(); step(-1); });
  $("btn-first").addEventListener("click", () => { stopAutoplay(); goTo(0); });
  $("btn-last").addEventListener("click",
    () => { stopAutoplay(); goTo(lastIndexOfLine(0)); });
  $("btn-flip").addEventListener("click", () => {
    state.flipped = !state.flipped; boardCache.clear(); renderPosition();
  });
  $("btn-play").addEventListener("click", toggleAutoplay);
  $("free-back").addEventListener("click", returnToLine);

  $("export-open").addEventListener("click", () => {
    $("export-dialog").classList.remove("hidden");
    syncExportDialog();
  });
  $("export-cancel").addEventListener("click",
    () => $("export-dialog").classList.add("hidden"));
  $("export-go").addEventListener("click", runExport);
  $("opt-mode").addEventListener("change", syncExportDialog);
  $("opt-evals").addEventListener("change", syncExportDialog);

  document.addEventListener("keydown", onKeyDown);

  const saved = localStorage.getItem("lsp_token");
  if (saved) $("token").value = saved;

  try {
    const health = await (await fetch("/api/health")).json();
    state.hasEngine = !!health.stockfish;
    if (!health.latex) {
      const option = document.querySelector('#opt-mode option[value="book"]');
      if (option) {
        option.textContent += "  (pdflatex not found)";
        option.disabled = true;
        $("opt-mode").value = "slideshow";
      }
    }
  } catch (error) { /* health is advisory only */ }

  const params = new URLSearchParams(location.search);
  if (params.get("url")) {
    $("study-url").value = params.get("url");
    loadStudy(null);
  } else {
    // Opening the app lands on the study list rather than an empty page.
    showHome();
  }
}

document.addEventListener("DOMContentLoaded", init);
