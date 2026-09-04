/* Weakness Report - browser interface.
 *
 * The server owns the report; this file owns what you are looking at. There is
 * only one long operation in the whole app -- reviewing -- and it is a
 * background job with a progress bar. Everything else, including changing how
 * much evidence a claim needs, is a request that reads what is already on disk.
 *
 * One convention: a positive number is always bad for you. Excess loss is
 * centipawns you gave away beyond your own average, so red is worse and green
 * is better, everywhere, and nothing flips depending on which colour you had.
 */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  health: null,
  reports: [],
  key: null,
  report: null,
  tab: "findings",
  slice: null,
  moment: null,
  flipped: false,
  jobTimer: null,
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
    statusTimer = setTimeout(() => { box.className = "status hidden"; }, 6000);
  }
}

async function guard(work) {
  try { return await work(); }
  catch (error) { say(error.message, "error"); return null; }
}

const escapeHtml = (value) => String(value == null ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const num = (value, digits = 1) =>
  value === null || value === undefined ? "-" : Number(value).toFixed(digits);

const narrow = () => window.matchMedia("(max-width: 860px)").matches;

function duration(seconds) {
  seconds = Math.round(seconds || 0);
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

// ------------------------------------------------------------------ health

async function loadHealth() {
  const health = await api("GET", "/api/health");
  state.health = health;

  $("opt-preset").innerHTML = Object.entries(health.presets)
    .map(([key, preset]) =>
      `<option value="${key}">${escapeHtml(preset.label)} — depth ${preset.depth}</option>`)
    .join("");
  $("opt-preset").value = health.defaults.preset;
  presetChanged();

  $("opt-speeds").innerHTML = health.speeds.map((speed) => `
    <label data-speed="${speed}">
      <input type="checkbox" value="${speed}"> ${speed}
    </label>`).join("");
  $("opt-speeds").querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => {
      input.closest("label").classList.toggle("on", input.checked);
    });
  });

  $("opt-limit").value = health.defaults.limit;
  $("opt-limit").max = health.maxGames;
  $("opt-threads").value = health.defaults.threads;
  $("opt-minmoves").value = health.defaults.minMoves;
  $("opt-mingames").value = health.defaults.minGames;
  $("settings-box").open = !narrow();

  renderEnginePill();

  if (!health.analyzer) {
    say("ChessAnalyzer could not be found, so games cannot be reviewed. Keep " +
        "its folder beside this one in the repository, set CHESS_ANALYZER_DIR, " +
        "or run: pip install -e ChessAnalyzer", "error");
  } else if (!health.stockfish) {
    say("No Stockfish found, so there is nothing to review with. Drop a binary " +
        "into Lichess-Study-to-PDF/engine/ or let ChessAnalyzer download one.");
  } else if (!health.study) {
    say("The study exporter is not installed, so the PDF will have no board " +
        "diagrams. Everything else works. pip install -e Lichess-Study-to-PDF");
  }
}

function renderEnginePill() {
  const health = state.health || {};
  const pill = $("engine-pill");
  if (!health.analyzer) {
    pill.textContent = "no review rules";
    pill.className = "pill none";
    pill.title = "ChessAnalyzer could not be found.";
    return;
  }
  pill.textContent = `${health.reviewsOnDisk} reviews`;
  pill.className = "pill";
  pill.title =
    `Review rules from ChessAnalyzer (${health.analyzerVia}).\n` +
    `Engine: ${health.stockfish || "none found"}\n` +
    `${health.reviewsOnDisk} reviews on disk, shared by every report.`;
}

function presetChanged() {
  const health = state.health || {};
  const preset = (health.presets || {})[$("opt-preset").value];
  $("preset-note").textContent = preset ? preset.detail : "";
  estimate();
}

let estimateTimer = null;
function estimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(async () => {
    const body = runBody();
    if (!body) { $("estimate-note").textContent = ""; return; }
    const guess = await api("POST", "/api/estimate", body).catch(() => null);
    if (!guess) { $("estimate-note").textContent = ""; return; }
    $("estimate-note").textContent = guess.cached
      ? `${guess.games} games cached, ${guess.ready} already reviewed at these ` +
        `settings. ${guess.outstanding} to go: roughly ${duration(guess.seconds)}.`
      : "Nothing cached yet, so the games are fetched first. The estimate " +
        "appears once they are.";
  }, 400);
}

// ------------------------------------------------------------------- run

function sourceChanged() {
  const kind = $("source").value;
  $("path").classList.toggle("hidden", kind !== "pgn");
  $("username").classList.toggle("hidden", kind === "pgn");
  $("username").placeholder = kind === "analyzer"
    ? "your username (to pick your side)" : "your username";
  estimate();
}

function speedsChosen() {
  return Array.from($("opt-speeds").querySelectorAll("input:checked"))
    .map((input) => input.value);
}

function runBody() {
  const kind = $("source").value;
  const username = $("username").value.trim();
  const path = $("path").value.trim();
  if (kind === "pgn" ? !path : !username) return null;

  const days = Number($("opt-days").value) || 0;
  return {
    spec: {
      kind, username, path,
      limit: Number($("opt-limit").value) || 200,
      speeds: speedsChosen(),
      ratedOnly: $("opt-rated").checked,
      sinceMs: days ? Date.now() - days * 86400000 : null,
    },
    preset: $("opt-preset").value,
    threads: Number($("opt-threads").value) || 1,
    adopt: $("opt-adopt").value,
    refresh: $("opt-refresh").checked,
    review: !$("opt-noreview").checked,
    minMoves: Number($("opt-minmoves").value) || 40,
    minGames: Number($("opt-mingames").value) || 5,
  };
}

async function startRun(event) {
  if (event) event.preventDefault();
  const body = runBody();
  if (!body) {
    return say($("source").value === "pgn"
      ? "Give the path to a PGN file or folder."
      : "Type your username first.", "error");
  }
  const started = await guard(() => api("POST", "/api/run", body));
  if (!started) return undefined;

  $("btn-run").disabled = true;
  watchJob(started.job.id, () => {
    say(`Reviewed ${started.key}.`, "ok");
    openReport(started.key);
  }, () => {
    $("btn-run").disabled = false;
    loadReports();
    loadHealth().then(renderEnginePill);
  });
  return undefined;
}

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
      `${job.message || job.state}${job.total ? ` (${job.done}/${job.total})` : ""}` +
      `${job.elapsed ? ` · ${duration(job.elapsed)}` : ""}`;

    if (["done", "failed", "cancelled"].includes(job.state)) {
      clearInterval(state.jobTimer);
      $("job").classList.add("hidden");
      if (job.state === "failed") say(job.error, "error");
      else if (job.state === "cancelled") {
        say("Stopped. Every review already finished is kept.", null);
      } else if (onDone) onDone(job);
      if (onEnd) onEnd();
    }
  }, 700);
}

// --------------------------------------------------------------- reports

async function loadReports() {
  const payload = await api("GET", "/api/reports");
  state.reports = payload.reports || [];
  const list = $("report-list");
  list.innerHTML = state.reports.map((row) => `
    <li data-key="${escapeHtml(row.key)}" class="${row.key === state.key ? "active" : ""}">
      <span>
        <span class="who">${escapeHtml(row.label)}</span>
        <span class="dim small"> ${row.reviewed} games</span>
      </span>
      <button type="button" class="drop" data-drop="${escapeHtml(row.key)}"
              title="Forget this report (reviews are kept)" aria-label="Forget">&times;</button>
    </li>`).join("");

  $("reports-note").textContent = state.reports.length
    ? `${state.reports.length} saved. Reviews are shared, so a second history ` +
      "of the same games is free."
    : "Nothing reviewed yet.";

  list.querySelectorAll("li").forEach((item) => {
    item.addEventListener("click", (event) => {
      if (event.target.dataset.drop) return;
      openReport(item.dataset.key);
    });
  });
  list.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const result = await guard(() =>
        api("DELETE", `/api/reports/${button.dataset.drop}`));
      if (result) say(`Forgot that report. ${result.reviewsKept} reviews kept.`, "ok");
      if (state.key === button.dataset.drop) {
        state.key = null; state.report = null;
        $("report").classList.add("hidden");
        $("empty").classList.remove("hidden");
        $("btn-pdf").disabled = true;
        $("btn-csv").disabled = true;
      }
      loadReports();
    });
  });
}

async function openReport(key) {
  const report = await guard(() => api("GET", `/api/reports/${key}`));
  if (!report) return;
  state.key = key;
  state.report = report;
  state.moment = null;
  state.slice = state.slice || Object.keys(report.slices || {})[0] || null;
  $("btn-pdf").disabled = false;
  $("btn-csv").disabled = false;
  $("detail").classList.add("hidden");
  $("detail-empty").classList.remove("hidden");

  const thresholds = report.thresholds || {};
  if (thresholds.minMoves) $("opt-minmoves").value = thresholds.minMoves;
  if (thresholds.minGames) $("opt-mingames").value = thresholds.minGames;

  loadReports();
  renderReport();
}

// ---------------------------------------------------------------- render

function renderReport() {
  const report = state.report;
  if (!report) return;
  $("empty").classList.add("hidden");
  $("report").classList.remove("hidden");

  const summary = report.summary || {};
  const record = summary.record || {};
  const batch = report.batch || {};
  const settings = batch.settings || {};

  $("rep-name").textContent = report.label || report.key;
  $("rep-meta").textContent = [
    `${summary.reviewed || 0} games reviewed`,
    summary.from ? `${summary.from} to ${summary.to}` : "",
    `+${record.win || 0} =${record.draw || 0} -${record.loss || 0}`,
    settings.depth ? `depth ${settings.depth}, ${settings.threads} thread${settings.threads === 1 ? "" : "s"}` : "",
    `built ${(report.builtAt || "").slice(0, 10)}`,
  ].filter(Boolean).join(" · ");

  $("summary").innerHTML = `
    <div class="stat"><span>your moves</span><b>${summary.scoredMoves || 0}</b></div>
    <div class="stat"><span>centipawn loss</span><b>${num(summary.acpl)}</b></div>
    <div class="stat"><span>accuracy</span><b>${num(summary.accuracy)}</b></div>
    <div class="stat"><span>~ rating</span><b>${summary.estimatedRating || "-"}</b></div>
    <div class="stat"><span>as white / black</span>
      <b>${(summary.colours || {}).white || 0} / ${(summary.colours || {}).black || 0}</b></div>`;

  const caveats = [];
  if (batch.uniform === false) {
    caveats.push("Not every game was searched the same way, so the figures " +
      "mix settings and are not strictly comparable between games. Set " +
      "<em>Existing reviews</em> to <em>ignore</em> and run again for a clean set.");
  }
  if (summary.unreviewed) {
    caveats.push(`${summary.unreviewed} of the ${summary.games} games fetched ` +
      "have no review and are not counted anywhere below.");
  }
  if ((batch.failures || []).length) {
    caveats.push(`${batch.failures.length} games could not be reviewed.`);
  }
  $("caveats").innerHTML = caveats.length
    ? `<div class="note">${caveats.join("<br>")}</div>` : "";

  renderTabs();
}

function renderTabs() {
  document.querySelectorAll("#tabs .tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.tab);
  });
  ["findings", "slices", "moments", "method"].forEach((name) => {
    $(`tab-${name}`).classList.toggle("hidden", name !== state.tab);
  });
  if (state.tab === "findings") renderFindings();
  else if (state.tab === "slices") renderSlices();
  else if (state.tab === "moments") renderMoments();
  else if (state.tab === "method") renderMethod();
}

function findingHtml(row, kind) {
  return `<div class="finding ${kind}">
    <div class="claim">
      <span>${escapeHtml(row.bucket)}</span>
      <em class="${kind === "bad" ? "score-bad" : "score-good"}">
        ${kind === "bad" ? "+" : "-"}${Math.abs(row.excessPawnsPerGame).toFixed(2)} pawns/game
      </em>
    </div>
    <div class="why">
      ${row.scored} moves in ${row.games} games at ${num(row.acpl, 0)} centipawns
      against your ${num(row.baseline, 0)}
      · accuracy ${num(row.accuracy)}
      · ${row.blunders} blunder${row.blunders === 1 ? "" : "s"}
      · slice: ${escapeHtml(row.dimensionLabel)}
    </div>
  </div>`;
}

function renderFindings() {
  const found = (state.report || {}).findings || {};
  const box = $("findings-body");
  const weaknesses = found.weaknesses || [];
  const strengths = found.strengths || [];

  if (!weaknesses.length && !strengths.length) {
    box.innerHTML = `<div class="note">Nothing cleared the evidence floor of
      ${found.minMoves} moves across ${found.minGames} games. Review more games,
      or lower the floor above &mdash; the numbers are all still in
      <em>Every slice</em>, with their sample sizes.</div>`;
    return;
  }

  box.innerHTML =
    `<p class="dim small">Ranked by excess loss: how much a kind of position
      costs you <em>beyond your own average</em>, which is what makes it worth
      studying rather than simply common.</p>` +
    weaknesses.map((row) => findingHtml(row, "bad")).join("") +
    (strengths.length
      ? `<h3 style="margin-top:16px">What you do well</h3>` +
        strengths.map((row) => findingHtml(row, "good")).join("")
      : "") +
    ((found.skipped || []).length
      ? `<p class="dim small" style="margin-top:14px">
          ${found.skipped.length} more buckets were below the floor, the biggest
          being ${escapeHtml(found.skipped[0].bucket)} with
          ${found.skipped[0].moves} moves in ${found.skipped[0].games} games.</p>`
      : "");
}

function renderSlices() {
  const report = state.report || {};
  const slices = report.slices || {};
  const keys = Object.keys(slices);
  if (!keys.length) { $("slices-body").innerHTML = ""; return; }
  if (!slices[state.slice]) state.slice = keys[0];

  $("slice-picker").innerHTML = keys.map((key) => `
    <label data-slice="${key}" class="${key === state.slice ? "on" : ""}">
      ${escapeHtml(slices[key].label)}
    </label>`).join("");
  $("slice-picker").querySelectorAll("[data-slice]").forEach((label) => {
    label.addEventListener("click", () => {
      state.slice = label.dataset.slice;
      renderSlices();
    });
  });

  const data = slices[state.slice];
  const baseline = report.baselineAcpl;
  $("slices-body").innerHTML = `
    <div class="slice-block">
      <h4>${escapeHtml(data.label)}</h4>
      <p class="dim small">${escapeHtml(data.note || "")}</p>
      <table class="slice-table">
        <thead><tr>
          <th>bucket</th><th class="num">moves</th><th class="num">games</th>
          <th class="num">acpl</th><th class="num">accuracy</th>
          <th class="num">blunders</th><th class="num">vs your ${num(baseline, 0)}</th>
        </tr></thead>
        <tbody>${(data.buckets || []).map((row) => {
          const delta = row.acpl === null || baseline === null
            ? null : row.acpl - baseline;
          return `<tr>
            <td>${escapeHtml(row.bucket)}</td>
            <td class="num">${row.moves}</td>
            <td class="num">${row.games}</td>
            <td class="num">${num(row.acpl, 0)}</td>
            <td class="num">${num(row.accuracy)}</td>
            <td class="num">${(row.judgments || {}).blunder || 0}</td>
            <td class="num delta ${delta > 0 ? "score-bad" : "score-good"}">
              ${delta === null ? "-" : (delta > 0 ? "+" : "") + delta.toFixed(0)}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`;
}

function renderMoments() {
  const moments = (state.report || {}).worstMoments || [];
  const box = $("tab-moments");
  if (!moments.length) {
    box.innerHTML = `<div class="note">No moves to show.</div>`;
    return;
  }
  box.innerHTML =
    `<p class="dim small">The single worst moves across the whole history, at
      most two from any one game. Click one to see it.</p>
     <div class="moments">` + moments.map((row, index) => `
      <div class="moment" data-index="${index}">
        <img loading="lazy" alt="position"
             src="/api/board?fen=${encodeURIComponent(row.fen)}&size=200&flipped=${row.you === "black" ? 1 : 0}">
        <div class="head">
          <span>${row.moveNumber}. ${escapeHtml(row.san)}</span>
          <span class="cost">-${row.pawns}</span>
        </div>
        <div class="sub">best ${escapeHtml(row.bestSan || "?")} · ${escapeHtml(row.label)}</div>
        <div class="sub">${escapeHtml(row.situation || row.phase)}</div>
      </div>`).join("") + `</div>`;

  box.querySelectorAll(".moment").forEach((element) => {
    element.addEventListener("click", () => {
      box.querySelectorAll(".moment").forEach((other) =>
        other.classList.remove("active"));
      element.classList.add("active");
      selectMoment(moments[Number(element.dataset.index)]);
    });
  });
}

function selectMoment(row) {
  state.moment = row;
  state.flipped = row.you === "black";
  $("detail-empty").classList.add("hidden");
  $("detail").classList.remove("hidden");
  drawMoment();

  $("detail-move").textContent = `${row.moveNumber}. ${row.san}`;
  $("detail-context").textContent =
    [row.opening, row.situation || row.phase,
     row.them ? `vs ${row.them}` : "", row.date].filter(Boolean).join(" · ");
  $("detail-stats").innerHTML = [
    `<span class="chip strong">-${row.pawns} pawns</span>`,
    `<span class="chip">${escapeHtml(row.label)}</span>`,
    row.bestSan ? `<span class="chip">engine wanted ${escapeHtml(row.bestSan)}</span>` : "",
  ].filter(Boolean).join("");
  $("detail-game").href = row.url || "#";
  $("detail-game").classList.toggle("hidden", !row.url);
}

function drawMoment() {
  const row = state.moment;
  if (!row) return;
  const params = new URLSearchParams({
    fen: row.fen, size: "360",
    // "1"/"0", never an empty string: FastAPI rejects "" for a bool with a 422
    // and the browser shows a broken image with no clue why.
    flipped: state.flipped ? "1" : "0",
  });
  $("detail-board").src = `/api/board?${params.toString()}`;
  $("detail-lichess").href =
    `https://lichess.org/analysis/${row.fen.replace(/ /g, "_")}`;
}

function renderMethod() {
  const report = state.report || {};
  const glossary = report.glossary || {};
  const thresholds = report.thresholds || {};
  const batch = report.batch || {};
  const settings = batch.settings || {};

  $("tab-method").innerHTML = `
    <p class="dim small">
      <b>Centipawn loss</b> is how much the engine thinks a move gave away, in
      hundredths of a pawn. <b>ACPL</b> is your average over the moves counted.
      Book moves are excluded, because playing twelve moves of theory is not
      evidence about your play. Both definitions are ChessAnalyzer's, unchanged,
      so a game means the same thing in both apps.
    </p>
    <p class="dim small">
      <b>Excess loss</b> is moves &times; (bucket ACPL &minus; your overall ACPL):
      how much a kind of position costs you beyond your own average. Findings are
      ranked by it, because ranking by ACPL alone crowns whichever bucket is
      smallest, and ranking by total loss crowns the middlegame every time.
    </p>
    <p class="dim small">
      Slices overlap, and none is controlled for the others: a bucket that
      happens to hold most of your middlegame will inherit how you play
      middlegames. Read two findings covering the same moves as one
      observation. Near-duplicates are already dropped by comparing move sets
      rather than names, but a partial overlap can still survive.
    </p>
    <p class="dim small">
      Nothing is claimed below ${thresholds.minMoves} counted moves across
      ${thresholds.minGames} games. Searched to a fixed depth
      ${settings.depth || "?"} on ${settings.threads || "?"} thread(s): a fixed
      depth and one thread are what make the report reproducible, so running it
      again in three months compares like with like.
    </p>
    <h3 style="margin-top:14px">What the terms mean</h3>
    <table class="slice-table"><tbody>${
      Object.entries(glossary).map(([term, meaning]) => `
        <tr><td style="width:160px"><b>${escapeHtml(term)}</b></td>
        <td>${escapeHtml(meaning)}</td></tr>`).join("")
    }</tbody></table>`;
}

// -------------------------------------------------------------- reslice

async function reslice() {
  if (!state.key) return;
  const body = {
    minMoves: Number($("opt-minmoves").value) || 40,
    minGames: Number($("opt-mingames").value) || 5,
  };
  const report = await guard(() =>
    api("POST", `/api/reports/${state.key}/reslice`, body));
  if (!report) return;
  state.report = report;
  renderReport();
  say("Re-sliced.", "ok");
}

// -------------------------------------------------------------------- pdf

async function buildPdf() {
  const body = {
    includeMoments: $("pdf-moments").checked,
    includeMethod: $("pdf-method").checked,
    landscapePages: $("pdf-landscape").checked,
  };
  $("pdf-status").textContent = "Building...";
  try {
    const response = await fetch(`/api/reports/${state.key}/pdf`, {
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
    link.download = `weakness-${state.report.label || state.key}.pdf`;
    link.click();
    URL.revokeObjectURL(url);
    $("pdf-status").textContent = "Done.";
    $("dlg-pdf").close();
  } catch (error) {
    $("pdf-status").textContent = error.message;
  }
}

// -------------------------------------------------------------------- wire

function init() {
  $("run-form").addEventListener("submit", startRun);
  $("source").addEventListener("change", sourceChanged);
  $("opt-preset").addEventListener("change", presetChanged);
  ["opt-limit", "opt-threads", "opt-adopt", "username", "path"].forEach((id) => {
    $(id).addEventListener("change", estimate);
  });

  document.querySelectorAll("#tabs .tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      renderTabs();
    });
  });

  $("btn-reslice").addEventListener("click", reslice);
  $("btn-flip").addEventListener("click", () => {
    state.flipped = !state.flipped;
    drawMoment();
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
    $("pdf-status").textContent = (state.health || {}).study
      ? "" : "No study exporter, so the PDF will have no board diagrams.";
    $("dlg-pdf").showModal();
  });
  $("pdf-go").addEventListener("click", buildPdf);
  $("btn-csv").addEventListener("click", () => {
    window.location.href = `/api/reports/${state.key}/csv`;
  });

  sourceChanged();
  loadHealth().then(loadReports).catch((error) => say(error.message, "error"));
}

document.addEventListener("DOMContentLoaded", init);
