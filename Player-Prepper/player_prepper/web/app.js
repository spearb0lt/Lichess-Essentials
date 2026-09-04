/* Player Prepper - browser interface.
 *
 * The server owns the report; this file owns what you are looking at. A scout
 * is a background job, so the only long-lived client state is "which report,
 * which colour, which row" - everything else is re-read from the server.
 *
 * One convention worth knowing before reading any of the rendering: every
 * score, everywhere, is from the *scouted player's* point of view, exactly as
 * it is on the server. A green 62% means they are doing well, which is bad
 * news for you. The colour coding follows that and never flips. The one
 * exception is the eval bar, which is a chess evaluation and therefore White's
 * point of view, like every eval bar anywhere.
 *
 * There is no chess library here on purpose, as in both sibling apps. Legality,
 * SAN and the positions along a line all come from the server, which is why
 * the board keeps `fens` for the whole line rather than a position: stepping
 * with the wheel has to be instant, and a round trip per notch would not be.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const FILES = "abcdefgh";

const state = {
  health: null,
  scouts: [],
  key: null,
  report: null,
  colour: "white",       // the colour THEY have
  tab: "gaps",
  selected: null,        // { row, kind }
  book: [],              // book source specs
  engineOn: false,       // auto-ask the engine for every position
  factors: { frequency: true, record: true, edge: true },
  jobTimer: null,
  explore: [],           // uci moves walked in the Explore tab
  exploitJob: null,

  // The board, and the line it is standing in. `path` is the whole line from
  // the starting position; `base` is how much of it came from the row you
  // clicked, so anything past it is a move you played yourself.
  board: {
    path: [], sans: [], fens: [null], cursor: 0, base: 0,
    flipped: false, pick: null, dests: [], lines: null, evalCache: new Map(),
  },
};

let evalToken = 0;
let linesToken = 0;
let linesTimer = null;
let wheelAccum = 0;

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
    statusTimer = setTimeout(() => { box.className = "status hidden"; }, 5000);
  }
}

async function guard(work) {
  try { return await work(); }
  catch (error) { say(error.message, "error"); return null; }
}

const escapeHtml = (value) => String(value == null ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const percent = (value) => `${Math.round((value || 0) * 100)}%`;

function scoreClass(score) {
  if (score >= 0.56) return "score-good";
  if (score <= 0.44) return "score-bad";
  return "score-even";
}

/** A SAN list as `1.e4 c5 2.Nf3`, matching the server's own formatting.
 *
 * The `1...` form is only right when the line *starts* on Black's move; with
 * White's move in front of it, the number is already there and repeating it
 * reads as a different line ("1.e4 1...d5").
 */
function prettyLine(sans) {
  const parts = [];
  (sans || []).forEach((san, index) => {
    if (index % 2 === 0) parts.push(`${index / 2 + 1}.${san}`);
    else if (!parts.length) parts.push(`${(index - 1) / 2 + 1}...${san}`);
    else parts.push(san);
  });
  return parts.join(" ");
}

const narrow = () => window.matchMedia("(max-width: 860px)").matches;

// ------------------------------------------------------------------ health

async function loadHealth() {
  const health = await api("GET", "/api/health");
  state.health = health;

  const speeds = $("opt-speeds");
  speeds.innerHTML = health.speeds.map((speed) => `
    <label data-speed="${speed}">
      <input type="checkbox" value="${speed}"> ${speed}
    </label>`).join("");
  speeds.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => {
      input.closest("label").classList.toggle("on", input.checked);
    });
  });

  $("opt-limit").value = health.defaults.limit;
  $("opt-maxply").value = health.defaults.maxPly;
  $("opt-mingames").value = health.defaults.minGames;
  $("opt-suggest").value = health.engine ? health.defaults.suggest : 0;
  $("opt-limit").max = health.maxGames;
  $("settings-box").open = !narrow();

  $("pdf-mode").innerHTML = Object.entries(health.pdfModes)
    .map(([key, label]) => `<option value="${key}">${escapeHtml(label)}</option>`)
    .join("");

  const saved = health.settings || {};
  if ((saved.book || []).length) state.book = saved.book;
  if (saved.factors) Object.assign(state.factors, saved.factors);
  state.engineOn = Boolean(saved.engineOn) && health.engine;
  renderBookPill();
  renderEnginePill();
  layoutSquares();

  const problems = [];
  if (!health.sibling) {
    problems.push("The sibling app is not installed, so there is no PDF export, " +
                  "no eval bar, no engine hints, and private studies cannot be " +
                  "read as a book. From the repository root: " +
                  "pip install -e Lichess-Study-to-PDF");
  } else if (!health.stockfish) {
    problems.push("No Stockfish found. The eval bar and gap suggestions fall " +
                  "back to the Lichess cloud, which only knows openings. Drop a " +
                  "binary into Lichess-Study-to-PDF/engine/ for the rest.");
  }
  if (problems.length) say(problems.join("\n"));
}

/** Persist one key of the settings blob without clobbering the others. */
async function saveSetting(key, value) {
  const settings = Object.assign({}, (state.health && state.health.settings) || {});
  settings[key] = value;
  if (state.health) state.health.settings = settings;
  await guard(() => api("POST", "/api/settings", { settings }));
}

// ------------------------------------------------------------------- book

function renderBookPill() {
  const pill = $("book-pill");
  if (!state.book.length) {
    pill.textContent = "no book";
    pill.className = "pill book none";
    pill.title = "Coverage needs a book. Click to choose one.";
    return;
  }
  // "book: 2 sources" is 117px wide, and it is the single item that pushes the
  // header onto a third row on a 360px phone. The short form says the same.
  pill.textContent = narrow()
    ? `book · ${state.book.length}`
    : `book: ${state.book.length} source${state.book.length > 1 ? "s" : ""}`;
  pill.className = "pill book";
  pill.title = state.book.map(describeSource).join("\n");
}

function describeSource(source) {
  if (source.kind === "repertoire") return `Repertoire: ${source.slug}`;
  if (source.kind === "study") return `Study: ${source.url} (${source.color || "auto"})`;
  if (source.kind === "games") return `Your games: ${source.site}/${source.username}`;
  return source.kind;
}

function renderBookSources() {
  const box = $("book-sources");
  if (!state.book.length) {
    box.innerHTML = `<p class="dim small">Nothing yet. Without a book this app
      still reports what they play, but it cannot tell you what you have no
      answer for.</p>`;
    return;
  }
  box.innerHTML = state.book.map((source, index) => `
    <div class="book-source">
      <span>${escapeHtml(describeSource(source))}</span>
      <button type="button" class="small-btn ghost" data-drop="${index}">Remove</button>
    </div>`).join("");
  box.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", () => {
      state.book.splice(Number(button.dataset.drop), 1);
      renderBookSources();
      renderBookPill();
      saveSetting("book", state.book);
    });
  });
}

async function openBookDialog() {
  const payload = await guard(() => api("GET", "/api/repertoires"));
  const rows = (payload && payload.repertoires) || [];
  $("book-slug").innerHTML = rows.length
    ? rows.map((row) => `<option value="${escapeHtml(row.slug)}">
        ${escapeHtml(row.name)} (${row.color}, ${row.chapters} chapters)
      </option>`).join("")
    : `<option value="">none found</option>`;
  $("book-report").textContent = rows.length ? "" :
    `No repertoires in ${payload ? payload.folder : "the repertoires folder"}.`;
  renderBookSources();
  $("dlg-book").showModal();
}

function bookKindChanged() {
  const kind = $("book-kind").value;
  $("add-repertoire").classList.toggle("hidden", kind !== "repertoire");
  $("add-study").classList.toggle("hidden", kind !== "study");
  $("add-games").classList.toggle("hidden", kind !== "games");
}

function addBookSource() {
  const kind = $("book-kind").value;
  let source = null;
  if (kind === "repertoire") {
    const slug = $("book-slug").value;
    if (!slug) return say("There is no repertoire to add.", "error");
    source = { kind, slug };
  } else if (kind === "study") {
    const url = $("book-url").value.trim();
    if (!url) return say("Paste a study URL first.", "error");
    source = { kind, url, color: $("book-color").value };
  } else {
    const username = $("book-user").value.trim();
    if (!username) return say("Type your own username first.", "error");
    source = { kind, site: $("book-site").value, username,
               limit: Number($("book-limit").value) || 200 };
  }
  state.book.push(source);
  renderBookSources();
  renderBookPill();
  saveSetting("book", state.book);
  return undefined;
}

async function checkBook() {
  if (!state.book.length) return say("Add a source first.", "error");
  $("book-report").textContent = "Building...";
  const stats = await guard(() => api("POST", "/api/book", { book: state.book }));
  if (!stats) { $("book-report").textContent = ""; return undefined; }
  const notes = stats.sources.filter((s) => s.note).map((s) => `${s.label}: ${s.note}`);
  $("book-report").textContent =
    `${stats.positions} positions, ${stats.moves} moves, ` +
    `${stats.branchPoints} branch points.` + (notes.length ? "\n" + notes.join("\n") : "");
  return undefined;
}

// ----------------------------------------------------------- engine toggle

function renderEnginePill() {
  const pill = $("engine-pill");
  const has = state.health && state.health.engine;
  pill.textContent = state.engineOn ? "engine on" : "engine off";
  pill.className = "pill engine" + (state.engineOn ? "" : " off");
  pill.disabled = !has;
  pill.title = has
    ? (state.engineOn
        ? "Every position you look at is analysed automatically. Click to stop."
        : "Click to analyse every position you look at, without asking each time.")
    : "No engine available. Install the sibling app and add a Stockfish binary.";
  $("btn-suggest").classList.toggle("hidden", !has || state.engineOn);
}

function toggleEngine() {
  state.engineOn = !state.engineOn;
  renderEnginePill();
  saveSetting("engineOn", state.engineOn);
  if (state.engineOn) refreshLines(true);
  else renderEngineBox();
}

// ------------------------------------------------------------------ scouts

async function loadScouts() {
  const payload = await api("GET", "/api/scouts");
  state.scouts = payload.scouts || [];
  const list = $("scout-list");
  list.innerHTML = state.scouts.map((row) => `
    <li data-key="${escapeHtml(row.key)}" class="${row.key === state.key ? "active" : ""}">
      <span>
        <span class="who">${escapeHtml(row.username)}</span>
        <span class="dim small"> ${row.site === "lichess" ? "Lichess" : "Chess.com"}
          &middot; ${row.games} games</span>
      </span>
      <button type="button" class="drop" data-drop="${escapeHtml(row.key)}"
              title="Forget this scout" aria-label="Forget">&times;</button>
    </li>`).join("");

  $("scouts-note").textContent = state.scouts.length
    ? `${state.scouts.length} saved. Reports are kept, so reopening one costs nothing.`
    : "Nobody yet.";

  list.querySelectorAll("li").forEach((item) => {
    item.addEventListener("click", (event) => {
      if (event.target.dataset.drop) return;
      openScout(item.dataset.key);
    });
  });
  list.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await guard(() => api("DELETE", `/api/scouts/${button.dataset.drop}`));
      if (state.key === button.dataset.drop) {
        state.key = null;
        state.report = null;
        $("report").classList.add("hidden");
        $("empty").classList.remove("hidden");
        $("btn-pdf").disabled = true;
        $("btn-pgn").disabled = true;
      }
      loadScouts();
    });
  });
}

/** The book specs that produced a report, so reopening it re-arms the picker. */
function specsFromReport(report) {
  const sources = ((report || {}).book || {}).sources || [];
  return sources.map((source) => {
    if (source.kind === "repertoire") return { kind: "repertoire", slug: source.slug };
    if (source.kind === "study") {
      return { kind: "study", url: source.url, color: source.color || "auto" };
    }
    if (source.kind === "games") {
      return { kind: "games", site: source.site, username: source.username };
    }
    return null;
  }).filter(Boolean);
}

async function openScout(key) {
  const report = await guard(() => api("GET", `/api/scouts/${key}`));
  if (!report) return;
  state.key = key;
  state.report = report;
  state.selected = null;
  state.explore = [];
  clearBoard();

  if (!state.book.length) {
    const specs = specsFromReport(report);
    if (specs.length) { state.book = specs; renderBookPill(); }
  }

  const colours = report.colors || {};
  const white = (colours.white || {}).tally || {};
  state.colour = white.games ? "white" : "black";
  $("btn-pdf").disabled = false;
  $("btn-pgn").disabled = false;
  loadScouts();
  renderReport();
}

function speedsChosen() {
  return Array.from($("opt-speeds").querySelectorAll("input:checked"))
    .map((input) => input.value);
}

async function startScout(event) {
  if (event) event.preventDefault();
  const username = $("username").value.trim();
  if (!username) return say("Type a username first.", "error");

  const days = Number($("opt-days").value) || 0;
  const body = {
    site: $("site").value,
    username,
    book: state.book,
    limit: Number($("opt-limit").value) || 300,
    speeds: speedsChosen(),
    ratedOnly: $("opt-rated").checked,
    sinceMs: days ? Date.now() - days * 86400000 : null,
    maxPly: Number($("opt-maxply").value) || 24,
    minGames: Number($("opt-mingames").value) || 5,
    refresh: $("opt-refresh").checked,
    suggest: Number($("opt-suggest").value) || 0,
  };

  const started = await guard(() => api("POST", "/api/scout", body));
  if (!started) return undefined;
  $("btn-scout").disabled = true;
  watchJob(started.job.id, () => {
    $("btn-scout").disabled = false;
    say(`Scouted ${started.key.split("-").slice(1).join("-")}.`, "ok");
    openScout(started.key);
    loadScouts();
  }, () => { $("btn-scout").disabled = false; loadScouts(); });
  return undefined;
}

/** Poll one job, then hand control back. Used by scouting and by exploit. */
function watchJob(jobId, onDone, onEnd) {
  $("job").classList.remove("hidden");
  $("job-cancel").onclick = () => guard(() => api("POST", `/api/jobs/${jobId}/cancel`));

  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    let job;
    try {
      job = await api("GET", `/api/jobs/${jobId}`);
    } catch (error) {
      clearInterval(state.jobTimer);
      $("job").classList.add("hidden");
      if (onEnd) onEnd();
      return;
    }
    $("job-fill").style.width = `${job.percent}%`;
    $("job-message").textContent =
      `${job.message || job.state}${job.total ? ` (${job.done}/${job.total})` : ""}`;

    if (["done", "failed", "cancelled"].includes(job.state)) {
      clearInterval(state.jobTimer);
      $("job").classList.add("hidden");
      if (job.state === "failed") say(job.error, "error");
      else if (job.state === "cancelled") say("Stopped.", null);
      else if (onDone) onDone(job);
      if (onEnd) onEnd();
    }
  }, 600);
}

// ------------------------------------------------------------------ report

function section() {
  const colours = (state.report && state.report.colors) || {};
  return colours[state.colour] || {};
}

function renderReport() {
  const report = state.report;
  if (!report) return;
  $("empty").classList.add("hidden");
  $("report").classList.remove("hidden");

  const summary = report.summary || {};
  const tally = summary.tally || {};
  const site = report.site === "lichess" ? "Lichess" : "Chess.com";
  $("rep-name").textContent = `${report.username} on ${site}`;

  const rating = summary.rating || {};
  const speeds = Object.entries(summary.speeds || {})
    .map(([name, count]) => `${count} ${name}`).join(", ");
  const bits = [
    `${summary.games || 0} games`,
    summary.from ? `${summary.from} to ${summary.to}` : "",
    `scoring ${percent(tally.score)} (+${tally.w || 0} =${tally.d || 0} -${tally.l || 0})`,
    rating.median ? `rating ${rating.min}-${rating.max}` : "",
    speeds,
    report.book ? `book: ${report.book.label}` : "no book, so coverage is not measured",
  ].filter(Boolean);
  $("rep-meta").textContent = bits.join(" · ");

  document.querySelectorAll(".colour-switch .seg").forEach((button) => {
    const colour = button.dataset.colour;
    const games = ((report.colors || {})[colour] || {}).tally || {};
    button.classList.toggle("active", colour === state.colour);
    button.disabled = !games.games;
    button.textContent = `They play ${colour === "white" ? "White" : "Black"}` +
      (games.games ? ` (${games.games})` : "");
  });

  renderCoverage();
  renderTabs();
}

function renderCoverage() {
  const data = section();
  const coverage = data.coverage || {};
  const tally = data.tally || {};
  const box = $("coverage");

  if (coverage.noBook) {
    box.innerHTML = `<div class="stat wide"><span>coverage</span>
      <b>No book set, so nothing was measured. Choose one with the
      <em>book</em> button in the top bar and scout again.</b></div>`;
    return;
  }

  box.innerHTML = `
    <div class="stat"><span>their score</span>
      <b class="${scoreClass(tally.score)}">${percent(tally.score)}</b></div>
    <div class="stat"><span>games you would face</span>
      <b>${coverage.inScope || 0}</b></div>
    <div class="stat"><span>covered end to end</span>
      <b>${coverage.percent || 0}%</b>
      <div class="bar"><span style="width:${coverage.percent || 0}%"></span></div></div>
    <div class="stat"><span>gap positions</span>
      <b>${coverage.gapPositions || 0}</b></div>
    <div class="stat"><span>games hitting a gap</span>
      <b>${coverage.allGapGames || 0}</b></div>
    <div class="stat wide"><span>reading it</span>
      <b>Of their ${coverage.games || 0} games as
      ${coverage.theyPlay}, ${coverage.offBook || 0} never reach your repertoire
      at all (their opponent played something you do not), and
      ${coverage.inScope || 0} do. Of those,
      ${coverage.covered || 0} stay inside your book to move
      ${Math.ceil((coverage.maxPly || 24) / 2)}; the other
      ${coverage.gapGames || 0} run into one of the positions below.</b></div>`;
}

function renderTabs() {
  const data = section();
  const gaps = ((data.coverage || {}).gaps) || [];
  const weak = data.weakSpots || [];
  const exploitRows = ((data.exploit || {}).rows) || [];
  $("count-gaps").textContent = gaps.length || "";
  $("count-weak").textContent = weak.length || "";
  $("count-exploit").textContent = exploitRows.length || "";

  document.querySelectorAll("#tabs .tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.tab);
  });
  ["gaps", "exploit", "openings", "weak", "moves", "explore"].forEach((name) => {
    $(`tab-${name}`).classList.toggle("hidden", name !== state.tab);
  });

  if (state.tab === "gaps") renderGaps();
  else if (state.tab === "exploit") renderExploit();
  else if (state.tab === "openings") renderOpenings();
  else if (state.tab === "weak") renderWeak();
  else if (state.tab === "moves") renderMoves();
  else if (state.tab === "explore") renderExplore();
}

function rowHtml(index, countCell, lineText, named, rightCell) {
  return `<div class="row" data-row="${index}">
    <div class="count-cell">${countCell}</div>
    <div>
      <div class="line">${escapeHtml(lineText || "(starting position)")}</div>
      ${named ? `<div class="named">${escapeHtml(named)}</div>` : ""}
    </div>
    <div class="right">${rightCell}</div>
  </div>`;
}

function wire(container, rows, kind) {
  container.querySelectorAll(".row").forEach((element) => {
    element.addEventListener("click", () => {
      container.querySelectorAll(".row").forEach((other) =>
        other.classList.remove("active"));
      element.classList.add("active");
      select(rows[Number(element.dataset.row)], kind);
    });
  });
}

function renderGaps() {
  const coverage = section().coverage || {};
  const gaps = coverage.gaps || [];
  const box = $("tab-gaps");

  if (coverage.noBook) {
    box.innerHTML = `<div class="note">Set a book to see gaps.</div>`;
    return;
  }
  if (!gaps.length) {
    box.innerHTML = `<div class="note">No gaps. Every one of their games that
      reaches your repertoire stays inside it for all
      ${coverage.maxPly || 24} plies scouted.</div>`;
    return;
  }

  box.innerHTML = `<p class="dim small">Positions their games reach where it is
    your move and your book has nothing. The number is how many of their games
    would put you there.</p><div class="rows">` +
    gaps.map((gap, index) => rowHtml(
      index,
      `${gap.games}<small>g</small>`,
      gap.lineText,
      (gap.opening || {}).name,
      `<span class="${scoreClass(gap.theirScore)}">${percent(gap.theirScore)}</span>
       <div class="dim small">they score</div>`)).join("") + `</div>`;
  wire(box, gaps, "gap");
}

function renderOpenings() {
  const rows = section().openings || [];
  const box = $("tab-openings");
  if (!rows.length) { box.innerHTML = `<div class="note">Nothing to show.</div>`; return; }
  box.innerHTML = `<p class="dim small">Every game grouped by the deepest named
    opening it reached, so a transposition lands with the opening it became.</p>
    <div class="rows">` + rows.map((row) => `
    <div class="row">
      <div class="count-cell">${row.games}<small>g</small></div>
      <div>
        <div class="line">${escapeHtml(row.name)}</div>
        <div class="named">${escapeHtml(row.eco || "")} &middot; ${percent(row.share)} of their games</div>
      </div>
      <div class="right"><span class="${scoreClass(row.score)}">${percent(row.score)}</span>
        <div class="dim small">+${row.w} =${row.d} -${row.l}</div></div>
    </div>`).join("") + `</div>`;
  // Opening rows are a group of games, not one position, so nothing to select.
  box.querySelectorAll(".row").forEach((element) => {
    element.style.cursor = "default";
  });
}

function renderWeak() {
  const rows = section().weakSpots || [];
  const box = $("tab-weak");
  const minimum = ((state.report || {}).settings || {}).minGames || 5;
  if (!rows.length) {
    box.innerHTML = `<div class="note">Nothing they have played at least
      ${minimum} times has cost them points. Lower "smallest sample" on the
      left and scout again to look harder.</div>`;
    return;
  }
  box.innerHTML = `<p class="dim small">Their own moves, ranked by points
    dropped below an even score: games &times; (0.5 &minus; their score). A fact
    about their results, not a verdict on the move -- the raw record is
    shown so you can judge the sample.</p><div class="rows">` +
    rows.map((row, index) => rowHtml(
      index,
      `${row.leak}<small> pts</small>`,
      `${row.lineText}${row.line.length ? " " : ""}→ ${row.san}`,
      (row.opening || {}).name,
      `<span class="${scoreClass(row.score)}">${percent(row.score)}</span>
       <div class="dim small">${row.games} games, +${row.w} =${row.d} -${row.l}</div>`
    )).join("") + `</div>`;
  wire(box, rows, "move");
}

function renderMoves() {
  const rows = section().topMoves || [];
  const box = $("tab-moves");
  if (!rows.length) { box.innerHTML = `<div class="note">Nothing to show.</div>`; return; }
  box.innerHTML = `<p class="dim small">Every choice they made, busiest position
    first. The share is how often they picked this move out of the times they
    reached that position.</p><div class="rows">` +
    rows.map((row, index) => rowHtml(
      index,
      `${row.games}<small>g</small>`,
      `${row.lineText}${row.line.length ? " " : ""}→ ${row.san}`,
      (row.opening || {}).name,
      `<span class="${scoreClass(row.score)}">${percent(row.score)}</span>
       <div class="dim small">${percent(row.share)} of ${row.reached}</div>`
    )).join("") + `</div>`;
  wire(box, rows, "move");
}

// ----------------------------------------------------------------- exploit

const FACTOR_LABEL = {
  frequency: "how often they play it",
  record: "how badly it goes for them",
  edge: "what the engine gives you",
};

/** Multiply the factors that are switched on. See exploit.py for the reasoning. */
function opportunityOf(row) {
  const factors = row.factors || {};
  let score = 1;
  let used = 0;
  ["frequency", "record", "edge"].forEach((name) => {
    if (!state.factors[name]) return;
    const value = factors[name];
    if (value === null || value === undefined) return;
    score *= value;
    used += 1;
  });
  return used ? score : 0;
}

function renderExploitFactors() {
  const box = $("exploit-factors");
  box.innerHTML = ["frequency", "record", "edge"].map((name) => `
    <label data-factor="${name}" class="${state.factors[name] ? "on" : ""}">
      <input type="checkbox" ${state.factors[name] ? "checked" : ""}>
      ${escapeHtml(name)} &mdash; ${escapeHtml(FACTOR_LABEL[name])}
    </label>`).join("");
  box.querySelectorAll("label").forEach((label) => {
    label.querySelector("input").addEventListener("change", (event) => {
      state.factors[label.dataset.factor] = event.target.checked;
      label.classList.toggle("on", event.target.checked);
      saveSetting("factors", state.factors);
      renderExploitRows();
    });
  });
}

function renderExploit() {
  renderExploitFactors();
  const data = section();
  const exploit = data.exploit;

  if (!exploit) {
    $("exploit-head").innerHTML = `<div class="note">Working out the best reply
      to each of their real choices. This is the one part of the app that needs
      a lot of engine time, so it runs once and is then saved with the
      report.</div>`;
    $("exploit-body").innerHTML = "";
    startExploit();
    return;
  }

  const summary = exploit.summary || {};
  const missing = summary.pending || 0;
  $("exploit-head").innerHTML = `
    <p class="dim small">
      ${summary.positions || 0} of their positions, covering
      ${summary.games || 0} games.
      ${exploit.engine === false
        ? "No engine was available, so the ranking uses their record and how often they play it."
        : `${summary.analysed || 0} analysed by the engine` +
          (missing ? `, ${missing} it had nothing to say about` : "") + "."}
      <button type="button" id="exploit-again" class="small-btn ghost">Run again</button>
    </p>`;
  $("exploit-again").addEventListener("click", () => startExploit(true));
  renderExploitRows();
}

function renderExploitRows() {
  const data = section();
  const exploit = data.exploit;
  const box = $("exploit-body");
  if (!exploit) return;

  const rows = (exploit.rows || []).slice();
  rows.forEach((row) => { row._opp = opportunityOf(row); });
  rows.sort((a, b) => (b._opp - a._opp) || (b.games - a.games));

  if (!rows.length) {
    box.innerHTML = `<div class="note">Nothing to rank.</div>`;
    return;
  }

  box.innerHTML = `<div class="rows">` + rows.map((row, index) => {
    const best = ((row.engine || {}).lines || [{}])[0];
    const factors = row.factors || {};
    const you = row.youPlay === "white" ? "White" : "Black";

    // Only one point of view in this column, and it is yours: an evaluation in
    // pawns beside a percentage of winning chances, with the two counted from
    // opposite sides, is the fastest way to misread a row. The raw White-POV
    // number is still in the engine box under the board.
    const edge = factors.edge === null || factors.edge === undefined
      ? `<span class="dim">no engine</span>`
      : `<span class="${factors.edge >= 0.5 ? "score-good" : "score-bad"}"
              title="Winning chances for you after the engine's best reply"
              >${percent(factors.edge)}</span>`;

    const named = (row.opening || {}).name || "";
    const reply = best && best.first
      ? `you play ${escapeHtml(best.first.san)}` : "";

    return `<div class="row" data-row="${index}">
      <div class="count-cell opp" title="Opportunity score: the enabled factors multiplied together"
        >${(row._opp * 100).toFixed(0)}<small> score</small></div>
      <div>
        <div class="line">${escapeHtml(row.lineText || prettyLine(row.line))}</div>
        <div class="named">${[escapeHtml(named), reply].filter(Boolean).join(" &middot; ")}</div>
      </div>
      <div class="right">
        ${edge}
        <div class="dim small">${row.games}g &middot; they score ${percent(row.score)} &middot; you have ${you}</div>
      </div>
    </div>`;
  }).join("") + `</div>`;
  wire(box, rows, "exploit");
}

async function startExploit(force) {
  if (state.exploitJob) return;
  const data = section();
  if (!force && data.exploit) return;

  const body = {
    color: state.colour,
    minGames: Math.max(1, Number($("opt-mingames").value) || 3),
    movetime: 0.6,
  };
  const started = await guard(() =>
    api("POST", `/api/scouts/${state.key}/exploit`, body));
  if (!started) {
    $("exploit-head").innerHTML = `<div class="note">Could not start. See the
      message above.</div>`;
    return;
  }
  state.exploitJob = started.job.id;
  watchJob(started.job.id, async () => {
    const report = await guard(() => api("GET", `/api/scouts/${state.key}`));
    if (report) {
      state.report = report;
      say("Exploit analysis done.", "ok");
    }
  }, () => {
    state.exploitJob = null;
    if (state.tab === "exploit") renderExploit();
    renderTabs();
  });
}

// ----------------------------------------------------------------- explore

async function renderExplore() {
  const box = $("explore-body");
  const crumbs = $("explore-crumbs");
  box.innerHTML = `<p class="dim small">Walking their tree...</p>`;

  const line = state.explore.join(",");
  const payload = await guard(() =>
    api("GET", `/api/scouts/${state.key}/tree?color=${state.colour}&line=${line}`));
  if (!payload) return;

  crumbs.innerHTML =
    `<button type="button" class="ghost small-btn" data-depth="0">start</button>` +
    state.explore.map((_, index) =>
      `<button type="button" class="ghost small-btn" data-depth="${index + 1}">
        ${escapeHtml(((payload.node || {}).line || [])[index] || "?")}</button>`).join("");
  crumbs.querySelectorAll("[data-depth]").forEach((button) => {
    button.addEventListener("click", () => {
      state.explore = state.explore.slice(0, Number(button.dataset.depth));
      renderExplore();
    });
  });

  if (!payload.found) {
    box.innerHTML = `<div class="note">${escapeHtml(payload.reason ||
      "They never reached this position.")}</div>`;
    return;
  }

  const node = payload.node;
  select({ fen: node.fen, line: node.line, lineUci: node.lineUci,
           lineText: prettyLine(node.line), games: node.games,
           opening: null }, "node");

  const theirs = node.moves || [];
  const replies = payload.replies || [];
  const rows = (theirs.length ? theirs : replies).map((move) => ({
    label: move.san, uci: move.uci, games: move.games, score: move.score,
    w: move.w, d: move.d, l: move.l,
    share: node.games ? move.games / node.games : 0,
  }));

  const whose = theirs.length ? "They played" : "Their opponents played";
  if (!rows.length) {
    box.innerHTML = `<div class="note">The tree stops here -- this is as deep as
      the scout went (${((state.report || {}).settings || {}).maxPly || 24}
      plies).</div>`;
    return;
  }

  box.innerHTML = `<p class="dim small">${whose}, from
    ${node.games} game${node.games === 1 ? "" : "s"} through this position.
    Click a move to walk on.</p><div class="rows">` +
    rows.map((row, index) => `
      <div class="row" data-row="${index}">
        <div class="count-cell">${row.games}<small>g</small></div>
        <div><div class="line">${escapeHtml(row.label)}</div>
          <div class="named">${percent(row.share)} of the time</div></div>
        <div class="right"><span class="${scoreClass(row.score)}">${percent(row.score)}</span>
          <div class="dim small">+${row.w} =${row.d} -${row.l}</div></div>
      </div>`).join("") + `</div>`;

  box.querySelectorAll(".row").forEach((element) => {
    element.addEventListener("click", () => {
      state.explore = state.explore.concat([rows[Number(element.dataset.row)].uci]);
      renderExplore();
    });
  });
}

// ------------------------------------------------------------------- board

function clearBoard() {
  const board = state.board;
  board.path = []; board.sans = []; board.fens = [null];
  board.cursor = 0; board.base = 0; board.pick = null;
  board.dests = []; board.lines = null;
  $("detail").classList.add("hidden");
  $("detail-empty").classList.remove("hidden");
}

const boardFen = () => state.board.fens[state.board.cursor] || null;

/** Where the board is standing relative to the row that was clicked. */
const atRowPosition = () => state.board.cursor === state.board.base
  && state.board.path.length === state.board.base;

async function select(row, kind) {
  if (!row) return;
  state.selected = { row, kind };
  $("detail-empty").classList.add("hidden");
  $("detail").classList.remove("hidden");

  // A gap is a position where *you* move, so orient the board your way; for
  // everything else, show it from the side whose choice is being discussed.
  const you = (section().coverage || {}).youPlay;
  if (kind === "gap") state.board.flipped = you === "black";
  else if (kind === "exploit") state.board.flipped = row.youPlay === "black";
  else state.board.flipped = state.colour === "black";

  // A move row is *about* their move, so stand just before it and let the
  // wheel play it: one notch forward and you are looking at what you face.
  const base = (row.lineUci || []).slice();
  const path = kind === "move" && row.uci ? base.concat([row.uci]) : base;

  const payload = await guard(() =>
    api("GET", `/api/line?moves=${path.join(",")}`));
  if (!payload) return;

  const board = state.board;
  board.path = payload.uci;
  board.sans = payload.sans;
  board.fens = payload.fens;
  board.base = Math.min(base.length, payload.uci.length);
  board.cursor = kind === "move" ? board.base : payload.uci.length;
  board.pick = null;
  board.dests = [];
  board.lines = null;

  renderDetailText();
  renderBoard();
}

function stepBoard(delta) {
  const board = state.board;
  const next = Math.max(0, Math.min(board.path.length, board.cursor + delta));
  if (next === board.cursor) return;
  board.cursor = next;
  board.pick = null;
  board.dests = [];
  board.lines = null;
  renderBoard();
}

function resetBoard() {
  if (!state.selected) return;
  select(state.selected.row, state.selected.kind);
}

function renderBoard() {
  const board = state.board;
  const fen = boardFen();
  if (!fen) return;

  const arrows = [];
  const best = bestMoveNow();
  if (best) arrows.push(`blue:${best}`);
  const upcoming = board.path[board.cursor];
  if (upcoming && upcoming !== best) arrows.push(`yellow:${upcoming}`);

  const params = new URLSearchParams({
    fen,
    size: "440",
    arrows: arrows.join(","),
    lastMove: board.cursor > 0 ? board.path[board.cursor - 1] : "",
    // "1"/"0", never an empty string: FastAPI rejects "" for a bool with a 422
    // and the browser shows a broken image with no clue why.
    flipped: board.flipped ? "1" : "0",
  });
  $("detail-board").src = `/api/board?${params.toString()}`;
  $("detail-lichess").href =
    `https://lichess.org/analysis/${fen.replace(/ /g, "_")}`;

  $("btn-back").disabled = board.cursor === 0;
  $("btn-first").disabled = board.cursor === 0;
  $("btn-forward").disabled = board.cursor >= board.path.length;
  $("btn-last").disabled = board.cursor >= board.path.length;
  $("btn-reset").classList.toggle("hidden", board.path.length <= board.base);

  renderSquares();
  renderMoveStrip();
  refreshEval();
  refreshLines();
  loadDests();
}

function renderMoveStrip() {
  const board = state.board;
  const box = $("move-strip");
  if (!board.sans.length) { box.innerHTML = ""; return; }

  const parts = [`<button type="button" data-ply="0"
      class="${board.cursor === 0 ? "here" : ""}">start</button>`];
  board.sans.forEach((san, index) => {
    if (index % 2 === 0) parts.push(`<span class="num">${index / 2 + 1}.</span>`);
    parts.push(`<button type="button" data-ply="${index + 1}"
      class="${board.cursor === index + 1 ? "here" : ""} ${index >= board.base ? "played" : ""}"
      >${escapeHtml(san)}</button>`);
  });
  box.innerHTML = parts.join(" ");
  box.querySelectorAll("[data-ply]").forEach((button) => {
    button.addEventListener("click", () => {
      board.cursor = Number(button.dataset.ply);
      board.pick = null; board.dests = []; board.lines = null;
      renderBoard();
    });
  });
}

/** Build the 64 click targets, inset by the SVG's coordinate margin. */
function layoutSquares() {
  const box = $("squares");
  if (box.children.length !== 64) {
    box.innerHTML = Array.from({ length: 64 }, (_, index) =>
      `<div data-index="${index}"></div>`).join("");
    box.addEventListener("click", (event) => {
      const cell = event.target.closest("[data-index]");
      if (cell) onSquare(squareName(Number(cell.dataset.index)));
    });
  }
  const margin = ((state.health || {}).boardMargin || 0) * 100;
  box.style.left = `${margin}%`;
  box.style.top = `${margin}%`;
  box.style.width = `${100 - 2 * margin}%`;
  box.style.height = `${100 - 2 * margin}%`;
}

function squareName(index) {
  const row = Math.floor(index / 8);
  const column = index % 8;
  return state.board.flipped
    ? `${FILES[7 - column]}${row + 1}`
    : `${FILES[column]}${8 - row}`;
}

function renderSquares() {
  const board = state.board;
  const cells = $("squares").children;
  for (let index = 0; index < cells.length; index += 1) {
    const name = squareName(index);
    const cell = cells[index];
    cell.className = "";
    if (board.pick === name) cell.classList.add("pick");
    if (board.dests.includes(name)) cell.classList.add("dest");
  }
}

async function loadDests() {
  const fen = boardFen();
  if (!fen) return;
  const legal = await guard(() => api("GET", `/api/legal?fen=${encodeURIComponent(fen)}`));
  if (!legal || fen !== boardFen()) return;
  state.board.legal = legal.moves || {};
}

function onSquare(name) {
  const board = state.board;
  if (board.pick && board.dests.includes(name)) {
    playMove(`${board.pick}${name}`);
    return;
  }
  const dests = (board.legal || {})[name] || [];
  board.pick = dests.length ? name : null;
  board.dests = dests;
  renderSquares();
}

async function playMove(uci) {
  const board = state.board;
  const fen = boardFen();
  const played = await guard(() => api("POST", "/api/play", { fen, uci }));
  if (!played) return;

  // Playing at a point you have stepped back to rewrites the rest of the line,
  // exactly as taking a move back and trying something else should.
  board.path = board.path.slice(0, board.cursor).concat([played.uci]);
  board.sans = board.sans.slice(0, board.cursor).concat([played.san]);
  board.fens = board.fens.slice(0, board.cursor + 1).concat([played.fen]);
  board.base = Math.min(board.base, board.cursor);
  board.cursor += 1;
  board.pick = null;
  board.dests = [];
  board.lines = null;
  renderBoard();
}

function onWheel(event) {
  if (!state.selected) return;
  event.preventDefault();
  // Trackpads send a stream of small deltas; accumulate to a threshold so a
  // flick is one move rather than ten. Same rule as the sibling apps.
  wheelAccum += event.deltaY;
  if (Math.abs(wheelAccum) < 40) return;
  stepBoard(wheelAccum > 0 ? 1 : -1);
  wheelAccum = 0;
}

// -------------------------------------------------------------- eval bar

function renderEvalBar(value) {
  const fill = $("evalfill");
  const text = $("evaltext");
  if (!value || value.known === false) {
    fill.style.height = "50%";
    fill.style.top = "auto";
    fill.style.bottom = "0";
    text.textContent = "";
    $("evalbar").title = "No evaluation available for this position.";
    return;
  }
  // The light block is always White's, and it always sits on the end of the
  // bar that White's side of the board is on. Keeping the colour fixed and
  // moving which end it grows from is what makes the bar readable after a
  // flip; growing it from the bottom regardless would put White's share
  // against Black's edge of the board.
  const white = value.whiteFraction === undefined ? 0.5 : value.whiteFraction;
  fill.style.height = `${Math.round(white * 100)}%`;
  fill.style.top = state.board.flipped ? "0" : "auto";
  fill.style.bottom = state.board.flipped ? "auto" : "0";
  text.textContent = value.text || "";
  text.title = `depth ${value.depth || 0}, ${value.source || "?"}`;
  $("evalbar").title =
    `${value.text} at depth ${value.depth || 0} (${value.source || "?"})`;
}

async function refreshEval() {
  const fen = boardFen();
  if (!fen) return;
  const cache = state.board.evalCache;
  if (cache.has(fen)) { renderEvalBar(cache.get(fen)); return; }

  renderEvalBar(null);
  const token = ++evalToken;
  const value = await guard(() => api("GET", `/api/eval?fen=${encodeURIComponent(fen)}`));
  if (token !== evalToken || fen !== boardFen()) return;
  if (value) {
    cache.set(fen, value);
    renderEvalBar(value);
  }
}

// --------------------------------------------------------- engine lines

/** The move to draw as the engine's arrow, from whichever source we have. */
function bestMoveNow() {
  const board = state.board;
  if (board.lines && board.lines.lines && board.lines.lines.length) {
    const first = board.lines.lines[0].first;
    return first ? first.uci : null;
  }
  if (atRowPosition() && state.selected) {
    const stored = (state.selected.row.engine || {}).lines || [];
    if (stored.length && stored[0].first) return stored[0].first.uci;
  }
  return null;
}

function renderEngineBox() {
  const board = state.board;
  const box = $("detail-engine");
  const result = board.lines
    || (atRowPosition() && state.selected ? state.selected.row.engine : null);

  if (!result) {
    box.innerHTML = state.engineOn
      ? `<p class="dim small">Thinking...</p>` : "";
    return;
  }
  if (!result.lines || !result.lines.length) {
    box.innerHTML = `<p class="dim small">The engine had nothing to say here.</p>`;
    return;
  }
  box.innerHTML = `<div class="dim small">Engine (${escapeHtml(result.source || "")})</div>` +
    result.lines.map((line) => `
      <div class="eline"><span class="eval">${escapeHtml(line.text)}</span>
        <span>${escapeHtml(line.line)}</span></div>`).join("");
}

/** Ask for lines when the toggle is on, debounced so stepping is not a storm. */
function refreshLines(immediate) {
  renderEngineBox();
  if (!state.engineOn || !(state.health || {}).engine) return;

  clearTimeout(linesTimer);
  const fen = boardFen();
  if (!fen) return;
  const token = ++linesToken;

  linesTimer = setTimeout(async () => {
    const result = await guard(() =>
      api("POST", "/api/suggest", { fen, count: 3, movetime: 0.5 }));
    if (token !== linesToken || fen !== boardFen()) return;
    state.board.lines = result;
    renderEngineBox();
    renderBoardArrowsOnly();
  }, immediate ? 0 : 260);
}

/** Redraw only the image, so a late engine answer does not reset the squares. */
function renderBoardArrowsOnly() {
  const board = state.board;
  const fen = boardFen();
  if (!fen) return;
  const arrows = [];
  const best = bestMoveNow();
  if (best) arrows.push(`blue:${best}`);
  const upcoming = board.path[board.cursor];
  if (upcoming && upcoming !== best) arrows.push(`yellow:${upcoming}`);
  const params = new URLSearchParams({
    fen, size: "440", arrows: arrows.join(","),
    lastMove: board.cursor > 0 ? board.path[board.cursor - 1] : "",
    flipped: board.flipped ? "1" : "0",
  });
  $("detail-board").src = `/api/board?${params.toString()}`;
}

async function askEngine() {
  const fen = boardFen();
  if (!fen) return;
  $("detail-engine").innerHTML = `<p class="dim small">Thinking...</p>`;
  const result = await guard(() =>
    api("POST", "/api/suggest", { fen, count: 3, movetime: 0.6 }));
  if (!result) { renderEngineBox(); return; }
  state.board.lines = result;
  if (atRowPosition() && state.selected) state.selected.row.engine = result;
  renderEngineBox();
  renderBoardArrowsOnly();
}

// ------------------------------------------------------------ detail text

function renderDetailText() {
  if (!state.selected) return;
  const { row, kind } = state.selected;
  const named = row.opening || {};

  $("detail-line").textContent =
    row.lineText || prettyLine(row.line) || "Starting position";
  $("detail-opening").textContent = named.known
    ? `${named.eco || ""} ${named.name || ""}`.trim() : "";

  const chips = [];
  if (kind === "gap") {
    chips.push(`<span class="chip strong">${row.games} of their games</span>`);
    chips.push(`<span class="chip">they score ${percent(row.theirScore)} from here</span>`);
    chips.push(`<span class="chip">you have ${row.youPlay}</span>`);
    chips.push(`<span class="chip">no move written down</span>`);
  } else if (kind === "exploit") {
    const factors = row.factors || {};
    chips.push(`<span class="chip strong">after ${escapeHtml(row.san)}</span>`);
    chips.push(`<span class="chip">${row.games} games</span>`);
    chips.push(`<span class="chip">they score ${percent(row.score)}</span>`);
    if (factors.edge !== null && factors.edge !== undefined) {
      chips.push(`<span class="chip">you get ${percent(factors.edge)}</span>`);
    }
    chips.push(`<span class="chip">${escapeHtml((row.from || []).join(", "))}</span>`);
  } else if (kind === "move") {
    chips.push(`<span class="chip strong">${escapeHtml(row.san)} &middot; ${row.games} games</span>`);
    chips.push(`<span class="chip">${percent(row.score)} for them</span>`);
    chips.push(`<span class="chip">+${row.w} =${row.d} -${row.l}</span>`);
    if (row.leak !== undefined) chips.push(`<span class="chip">${row.leak} points leaked</span>`);
    if (row.lastDate) chips.push(`<span class="chip">last ${escapeHtml(row.lastDate)}</span>`);
  } else {
    chips.push(`<span class="chip strong">${row.games} games here</span>`);
  }
  $("detail-stats").innerHTML = chips.join("");

  const samples = row.samples || (row.sample ? [row.sample] : []);
  $("detail-samples").innerHTML = samples.length
    ? `<div class="dim small" style="flex:1 0 100%">Their games that got here</div>`
      + samples.map((url, index) =>
        `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">game ${index + 1}</a>`).join("")
    : "";
}

// ------------------------------------------------------------------ export

async function buildPdf() {
  const body = {
    mode: $("pdf-mode").value,
    includeNotation: $("pdf-notation").checked,
    includeSteps: $("pdf-steps").checked,
    landscapePages: $("pdf-landscape").checked,
  };
  $("pdf-status").textContent = "Building...";
  try {
    const response = await fetch(`/api/scouts/${state.key}/pdf`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try { message = (await response.json()).detail || message; } catch (_) { /* keep */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `prep-${state.report.username}.pdf`;
    link.click();
    URL.revokeObjectURL(url);
    $("pdf-status").textContent = "Done.";
    $("dlg-pdf").close();
  } catch (error) {
    $("pdf-status").textContent = error.message;
  }
}

// -------------------------------------------------------------------- wire

function onKeyDown(event) {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (!state.selected) return;

  if (event.key === "ArrowRight" || event.key === "ArrowDown") stepBoard(1);
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") stepBoard(-1);
  else if (event.key === "Home") { state.board.cursor = 0; renderBoard(); }
  else if (event.key === "End") { state.board.cursor = state.board.path.length; renderBoard(); }
  else if (event.key === "f" || event.key === "F") flipBoard();
  else if (event.key === "Escape") {
    state.board.pick = null; state.board.dests = []; renderSquares();
  } else return;
  event.preventDefault();
}

function flipBoard() {
  state.board.flipped = !state.board.flipped;
  renderBoard();
}

function init() {
  $("scout-form").addEventListener("submit", startScout);

  $("book-pill").addEventListener("click", openBookDialog);
  $("book-kind").addEventListener("change", bookKindChanged);
  $("book-add").addEventListener("click", addBookSource);
  $("book-check").addEventListener("click", checkBook);
  $("engine-pill").addEventListener("click", toggleEngine);

  document.querySelectorAll(".colour-switch .seg").forEach((button) => {
    button.addEventListener("click", () => {
      state.colour = button.dataset.colour;
      state.explore = [];
      state.selected = null;
      clearBoard();
      renderReport();
    });
  });

  document.querySelectorAll("#tabs .tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      renderTabs();
    });
  });

  $("btn-flip").addEventListener("click", flipBoard);
  $("btn-back").addEventListener("click", () => stepBoard(-1));
  $("btn-forward").addEventListener("click", () => stepBoard(1));
  $("btn-first").addEventListener("click", () => {
    state.board.cursor = 0; renderBoard();
  });
  $("btn-last").addEventListener("click", () => {
    state.board.cursor = state.board.path.length; renderBoard();
  });
  $("btn-reset").addEventListener("click", resetBoard);
  $("btn-suggest").addEventListener("click", askEngine);
  $("board-holder").addEventListener("wheel", onWheel, { passive: false });
  document.addEventListener("keydown", onKeyDown);
  window.addEventListener("resize", () => {
    layoutSquares();
    renderBookPill();         // its label depends on how much room there is
  });

  $("btn-token").addEventListener("click", async () => {
    const info = await guard(() => api("GET", "/api/token"));
    $("token-status").textContent = info && info.hasToken
      ? "A token is currently in use." : "No token in use.";
    $("dlg-token").showModal();
  });
  $("token-ok").addEventListener("click", async () => {
    const info = await guard(() =>
      api("POST", "/api/token", { token: $("token-input").value }));
    if (info) {
      $("token-input").value = "";
      say(info.hasToken ? "Token saved for this session." : "Token cleared.", "ok");
      $("dlg-token").close();
    }
  });

  $("btn-pdf").addEventListener("click", () => {
    const health = state.health || {};
    $("pdf-note").textContent = health.sibling
      ? (health.latex ? "" : "Book mode needs a LaTeX install; the others work.")
      : "PDF export needs the sibling app: pip install -e Lichess-Study-to-PDF";
    $("pdf-status").textContent = "";
    $("dlg-pdf").showModal();
  });
  $("pdf-go").addEventListener("click", buildPdf);

  $("btn-pgn").addEventListener("click", () => {
    window.location.href = `/api/scouts/${state.key}/pgn`;
  });

  loadHealth().then(loadScouts).catch((error) => say(error.message, "error"));
}

document.addEventListener("DOMContentLoaded", init);
