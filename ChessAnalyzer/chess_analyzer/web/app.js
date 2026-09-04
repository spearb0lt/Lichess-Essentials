/* Chess Analyzer - browser interface.
 *
 * The server owns chess and the engine; this file owns what you are looking
 * at. Two consequences shape everything here.
 *
 * The board is drawn here rather than fetched as an image. Stepping through a
 * game with the mouse wheel is the main way anyone reads a review, and one
 * HTTP request per notch would make that feel broken. So every position in a
 * game arrives as a FEN in the import payload, and moving between them is a
 * re-render of an SVG the browser already has. Nothing is fetched, so nothing
 * can lag.
 *
 * But the browser has no chess rules. Playing a move, listing legal
 * destinations and reading a PGN all go to the server, because there is
 * exactly one chess implementation in this project and duplicating a subset
 * of it in JavaScript is how the two end up disagreeing about en passant at
 * the worst possible moment.
 *
 * `state.mode` is the only real switch: "game" is a fixed game you are
 * reading, "live" is one still being played. They share the board, the eval
 * bar and the engine panel; they differ in where the position comes from.
 */

"use strict";

const $ = (id) => document.getElementById(id);
const FILES = "abcdefgh";
const SVG_NS = "http://www.w3.org/2000/svg";
const SQ = 100;                    // board is an 800x800 viewBox

const LABEL_COLORS = {
  brilliant: "--l-brilliant", great: "--l-great", best: "--l-best",
  book: "--l-book", excellent: "--l-excellent", good: "--l-good",
  forced: "--l-forced", inaccuracy: "--l-inaccuracy", mistake: "--l-mistake",
  miss: "--l-miss", blunder: "--l-blunder",
};

const LABEL_ORDER = ["brilliant", "great", "best", "excellent", "good", "book",
                     "forced", "inaccuracy", "mistake", "miss", "blunder"];

const PIECE_NAMES = { p: "pawn", n: "knight", b: "bishop", r: "rook",
                      q: "queen", k: "king" };

const state = {
  mode: "game",              // "game" | "live"
  health: null,
  settings: {},

  game: null,                // the record
  moves: [],                 // [{ply, san, uci, fen, moveNumber, color, clock}]
  review: null,
  ply: 0,

  // Moves you try that the players did not. The game itself is never touched:
  // `moves` stays exactly as it was imported, and these hang off it.
  vars: new Map(),           // id -> { id, parent, from, moves: [...] }
  active: null,              // the variation we are standing in, or null
  varSeq: 0,

  live: null,                // the live session payload
  liveTimer: null,

  flipped: false,
  pick: null,
  dests: [],
  leftTab: "library",
  rightTab: "review",

  library: [],
  reviewJob: null,
  reviewTimer: null,
  engineTimer: null,
  engineToken: 0,
  lines: null,
  catalog: null,
  pasteTarget: "import",
  showLines: false,          // the "Lines" toggle in the top bar

  // While `editing` is true the board is not a game, it is an editing
  // surface: clicking a square stamps a piece instead of playing a move.
  editing: false,
  setup: {
    pieces: {},          // { e1: "K", ... }
    turn: "w",
    castling: "",        // the subset of KQkq that is ticked
    ep: "-",
    stamp: "P",          // what the next board click puts down
    info: null,          // the server's last verdict on the arrangement
  },
};

/* The palette, in board order: royalty first, pawns last, white above black.
   The eraser is a piece as far as clicking is concerned, which is why it
   lives in the same list rather than beside it. */
const PALETTE = [
  ["K", "Q", "R", "B", "N", "P", "eraser"],
  ["k", "q", "r", "b", "n", "p", null],
];

const PIECE_LETTERS = { p: "pawn", n: "knight", b: "bishop", r: "rook",
                        q: "queen", k: "king" };

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
  // The banner is a row in the page, so showing or hiding it moves everything
  // below it -- including the board, which is sized from the room it has.
  // Without re-fitting, a banner that has just faded leaves a gap the board
  // never grows into.
  fitBoard();
  if (message && kind !== "error") {
    statusTimer = setTimeout(() => {
      box.className = "status hidden";
      fitBoard();
    }, 5000);
  }
}

async function guard(work) {
  try { return await work(); }
  catch (error) { say(error.message, "error"); return null; }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, value);
  }
  return node;
}

function labelColor(label) {
  const name = LABEL_COLORS[label];
  return name ? `var(${name})` : "var(--muted)";
}

function clockText(seconds) {
  if (seconds == null) return "";
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

// ------------------------------------------------------------- positions

/* Variations.
 *
 * A variation is a list of moves hanging off one position of the line it
 * departs from -- the game, or another variation. The game's own move list is
 * never modified: `state.moves` is what was imported and stays that way, which
 * is the whole point. Trying a move used to replace everything after it, and
 * the rest of the game simply vanished from the notation.
 *
 * `state.ply` indexes the *current* line, which is the game up to the point
 * this variation departs, followed by its moves. A variation off a variation
 * works the same way, one level further down.
 */

/** The line currently on the board: the game, or a variation within it. */
function line() {
  if (state.mode === "live") return liveLine();
  return lineOf(state.active);
}

function lineOf(id) {
  if (id === null || id === undefined) return state.moves;
  const variation = state.vars.get(id);
  if (!variation) return state.moves;
  return lineOf(variation.parent)
    .slice(0, variation.from + 1)
    .concat(variation.moves);
}

/** Variations that depart from position `index` of the line owned by `parent`. */
function branchesAt(parent, index) {
  const found = [];
  for (const variation of state.vars.values()) {
    if (variation.parent === parent && variation.from === index) {
      found.push(variation);
    }
  }
  return found;
}

/** How much of the current line is still the real game.
 *
 * Everything up to here is a position the players actually reached, so the
 * review's judgments and its stored engine lines still apply to it. Past it
 * they do not, and must not be shown as though they did.
 */
function mainlineDepth() {
  let id = state.active;
  let depth = Infinity;
  while (id !== null && id !== undefined) {
    const variation = state.vars.get(id);
    if (!variation) break;
    depth = Math.min(depth, variation.from);
    id = variation.parent;
  }
  return depth;
}

function clearVariations() {
  state.vars = new Map();
  state.active = null;
  state.varSeq = 0;
}

function liveLine() {
  const session = state.live;
  if (!session) return [];
  // The session sends every position, not just the current one, so a game in
  // progress scrolls backwards exactly like a finished one.
  return session.positions || [];
}

function node(index) {
  const list = line();
  const at = index === undefined ? state.ply : index;
  return list[Math.max(0, Math.min(list.length - 1, at))] || null;
}

/** True when the board is showing the newest position of a live game. */
function atLatest() {
  return state.ply >= line().length - 1;
}

function currentFen() {
  const item = node();
  return item ? item.fen : "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
}

/** The review row for the ply on the board, if this ply is still the game. */
function reviewRow(index) {
  const at = index === undefined ? state.ply : index;
  if (!state.review || at < 1) return null;
  if (at > mainlineDepth()) return null;     // a move nobody played
  return state.review.moves[at - 1] || null;
}

// ----------------------------------------------------------------- board

function squareToXY(name) {
  const file = FILES.indexOf(name[0]);
  const rank = parseInt(name[1], 10) - 1;
  const column = state.flipped ? 7 - file : file;
  const row = state.flipped ? rank : 7 - rank;
  return { x: column * SQ, y: row * SQ };
}

function xyToSquare(x, y) {
  const column = Math.floor(x / SQ);
  const row = Math.floor(y / SQ);
  if (column < 0 || column > 7 || row < 0 || row > 7) return null;
  const file = state.flipped ? 7 - column : column;
  const rank = state.flipped ? row + 1 : 8 - row;
  return FILES[file] + rank;
}

function parseFen(fen) {
  const board = {};
  const rows = fen.split(" ")[0].split("/");
  for (let row = 0; row < 8; row += 1) {
    let file = 0;
    for (const character of rows[row]) {
      if (character >= "1" && character <= "8") {
        file += parseInt(character, 10);
      } else {
        board[FILES[file] + (8 - row)] = character;
        file += 1;
      }
    }
  }
  return board;
}

function findKing(pieces, white) {
  const want = white ? "K" : "k";
  for (const [square, piece] of Object.entries(pieces)) {
    if (piece === want) return square;
  }
  return null;
}

function buildSquares() {
  const layer = $("layer-squares");
  if (layer.childElementCount) return;
  for (let row = 0; row < 8; row += 1) {
    for (let column = 0; column < 8; column += 1) {
      const rect = svg("rect", {
        x: column * SQ, y: row * SQ, width: SQ, height: SQ,
        fill: (row + column) % 2 === 0 ? "var(--sq-light)" : "var(--sq-dark)",
        class: "sq",
      });
      rect.dataset.row = row;
      rect.dataset.column = column;
      layer.appendChild(rect);
    }
  }
}

function clear(id) {
  const layer = $(id);
  while (layer.firstChild) layer.removeChild(layer.firstChild);
  return layer;
}

function renderBoard() {
  buildSquares();
  if (state.editing) { renderEditingBoard(); return; }

  const fen = currentFen();
  const pieces = parseFen(fen);
  const item = node();
  const row = reviewRow();

  // last move
  const lastLayer = clear("layer-lastmove");
  const lastMove = item && item.uci;
  if (lastMove && lastMove.length >= 4) {
    for (const square of [lastMove.slice(0, 2), lastMove.slice(2, 4)]) {
      const { x, y } = squareToXY(square);
      lastLayer.appendChild(svg("rect", {
        x, y, width: SQ, height: SQ, class: "last",
      }));
    }
  }

  // check
  const checkLayer = clear("layer-check");
  const turnWhite = fen.split(" ")[1] === "w";
  if (state.checkSquare) {
    const { x, y } = squareToXY(state.checkSquare);
    checkLayer.appendChild(svg("circle", {
      cx: x + SQ / 2, cy: y + SQ / 2, r: SQ * 0.48,
      fill: "var(--sq-check)", opacity: ".55", class: "check",
    }));
  }

  // pieces
  const pieceLayer = clear("layer-pieces");
  for (const [square, symbol] of Object.entries(pieces)) {
    const { x, y } = squareToXY(square);
    const colour = symbol === symbol.toUpperCase() ? "white" : "black";
    const kind = PIECE_NAMES[symbol.toLowerCase()];
    const use = svg("use", {
      href: `#piece-${colour}-${kind}`, x, y, width: SQ, height: SQ,
      class: "piece",
    });
    use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href",
                       `#piece-${colour}-${kind}`);
    pieceLayer.appendChild(use);
  }

  // selection and legal destinations
  const hints = clear("layer-hints");
  if (state.pick) {
    const { x, y } = squareToXY(state.pick);
    hints.appendChild(svg("rect", { x, y, width: SQ, height: SQ, class: "pick" }));
  }
  for (const square of state.dests) {
    const { x, y } = squareToXY(square);
    if (pieces[square]) {
      hints.appendChild(svg("circle", {
        cx: x + SQ / 2, cy: y + SQ / 2, r: SQ * 0.45, class: "hint-capture",
      }));
    } else {
      hints.appendChild(svg("circle", {
        cx: x + SQ / 2, cy: y + SQ / 2, r: SQ * 0.16, class: "hint",
      }));
    }
  }

  // the engine's suggestion, drawn only when the move played was not it
  const arrows = clear("layer-arrows");
  if (row && !row.isBest && row.bestMove && state.rightTab === "review") {
    drawArrow(arrows, row.bestMove, "var(--accent)");
  }

  // the label badge on the destination square
  const badge = clear("layer-badge");
  if (row && lastMove) {
    const { x, y } = squareToXY(lastMove.slice(2, 4));
    const group = svg("g", { class: "badge" });
    group.appendChild(svg("circle", {
      cx: x + SQ - 12, cy: y + 12, r: 17, fill: labelColor(row.label),
    }));
    const text = svg("text", { x: x + SQ - 12, y: y + 13 });
    text.textContent = row.glyph;
    group.appendChild(text);
    badge.appendChild(group);
  }

  renderCoords();
  renderPlayers();
  renderCaption();
}

function drawArrow(layer, uci, colour) {
  const from = squareToXY(uci.slice(0, 2));
  const to = squareToXY(uci.slice(2, 4));
  const x1 = from.x + SQ / 2, y1 = from.y + SQ / 2;
  const x2 = to.x + SQ / 2, y2 = to.y + SQ / 2;
  const angle = Math.atan2(y2 - y1, x2 - x1);
  // Stop short of the centre so the arrowhead does not cover the piece.
  const shorten = 32;
  const ex = x2 - Math.cos(angle) * shorten;
  const ey = y2 - Math.sin(angle) * shorten;

  layer.appendChild(svg("line", {
    x1, y1, x2: ex, y2: ey, stroke: colour, "stroke-width": 13,
    "stroke-linecap": "round", opacity: ".72",
  }));
  const head = 26;
  const points = [
    [x2 - Math.cos(angle) * 6, y2 - Math.sin(angle) * 6],
    [ex - Math.cos(angle - Math.PI / 2) * head * 0.55,
     ey - Math.sin(angle - Math.PI / 2) * head * 0.55],
    [ex + Math.cos(angle - Math.PI / 2) * head * 0.55,
     ey + Math.sin(angle - Math.PI / 2) * head * 0.55],
  ];
  layer.appendChild(svg("polygon", {
    points: points.map((p) => p.join(",")).join(" "),
    fill: colour, opacity: ".72",
  }));
}

function renderCoords() {
  const layer = clear("layer-coords");
  for (let index = 0; index < 8; index += 1) {
    const file = state.flipped ? FILES[7 - index] : FILES[index];
    const rank = state.flipped ? index + 1 : 8 - index;
    const dark = index % 2 === 0;
    layer.appendChild(Object.assign(
      svg("text", {
        x: index * SQ + SQ - 14, y: 8 * SQ - 8, class: "coord",
        fill: dark ? "var(--sq-dark)" : "var(--sq-light)",
      }), { textContent: file }));
    layer.appendChild(Object.assign(
      svg("text", {
        x: 6, y: index * SQ + 18, class: "coord",
        fill: index % 2 === 0 ? "var(--sq-dark)" : "var(--sq-light)",
      }), { textContent: rank }));
  }
}

function renderPlayers() {
  const game = state.game;
  const live = state.live;
  const top = $("who-top"), bottom = $("who-bottom");
  top.innerHTML = ""; bottom.innerHTML = "";

  const white = live ? { name: live.white } : {
    name: game ? game.white : "", rating: game ? game.whiteElo : "",
  };
  const black = live ? { name: live.black } : {
    name: game ? game.black : "", rating: game ? game.blackElo : "",
  };
  const clocks = live ? live.clocks || {} : {};
  const turn = currentFen().split(" ")[1] === "w" ? "white" : "black";

  const build = (side, who) => {
    const box = el("div");
    box.appendChild(el("span", "nm", who.name || "?"));
    if (who.rating) box.appendChild(el("span", "rt", who.rating));
    const seconds = clocks[side];
    if (seconds != null) {
      const clock = el("span", "clk" + (turn === side ? " on" : ""),
                       clockText(seconds));
      box.appendChild(clock);
    }
    return box;
  };

  const topSide = state.flipped ? "white" : "black";
  top.appendChild(build(topSide, topSide === "white" ? white : black));
  const bottomSide = state.flipped ? "black" : "white";
  bottom.appendChild(build(bottomSide, bottomSide === "white" ? white : black));
}

function renderCaption() {
  const item = node();
  const row = reviewRow();
  const total = line().length - 1;
  const parts = [];
  if (item && item.ply > 0) {
    parts.push(`${item.moveNumber}${item.color === "white" ? "." : "..."} ${item.san}`);
  } else {
    parts.push("start");
  }
  parts.push(`${state.ply}/${total}`);
  if (row) {
    parts.push(`${row.label}${row.winLoss > 0 ? ` -${row.winLoss}%` : ""}`);
  }
  if (state.active !== null) parts.push("variation");
  $("move-caption").textContent = parts.join("   ");

  // The engine's preferred line used to live here; it is beside the controls
  // now, so this is only the notice that you have left the game.
  const note = $("board-note");
  note.innerHTML = "";
  if (state.active !== null) {
    note.appendChild(el("span", "", "You are in a variation. "));
    const back = el("button", "ghost small-btn", "Back to the game");
    back.addEventListener("click", () => {
      const depth = mainlineDepth();
      state.active = null;
      goTo(Math.min(state.ply, depth === Infinity ? state.ply : depth));
    });
    note.appendChild(back);
  }
  if (state.vars.size) {
    const wipe = el("button", "ghost small-btn", `Clear ${state.vars.size} variation`
      + (state.vars.size === 1 ? "" : "s"));
    wipe.addEventListener("click", () => {
      const depth = mainlineDepth();
      clearVariations();
      goTo(Math.min(state.ply, depth === Infinity ? state.ply : depth));
    });
    note.appendChild(wipe);
  }
}

function renderEvalBar() {
  let fraction = 0.5;
  let text = "";
  if (state.editing) {
    $("evalfill").style.height = "50%";
    $("evaltext").textContent = "";
    return;
  }
  const row = reviewRow();
  if (state.mode === "live" && state.live && state.live.analysis && atLatest()) {
    fraction = state.live.analysis.whiteFraction ?? 0.5;
    text = state.live.analysis.text || "";
  } else if (row) {
    fraction = row.evalAfter.whiteFraction ?? 0.5;
    text = row.evalAfter.text || "";
  } else if (state.review && state.ply === 0) {
    const first = state.review.graph[0];
    fraction = (first ? first.white : 50) / 100;
    text = "";
  } else if (state.lines && state.lines.fen === currentFen()) {
    fraction = state.lines.whiteFraction ?? 0.5;
    text = state.lines.text || "";
  }
  $("evalfill").style.height = `${(fraction * 100).toFixed(1)}%`;
  $("evaltext").textContent = text;
  $("evalbar").classList.toggle("flipped", state.flipped);
}

// ---------------------------------------------------- arranging a position

/* You are watching a game over someone's shoulder, or there is a wooden board
 * in front of you. There is no URL and no PGN, only a position. So the board
 * becomes an editor: pick a piece, click squares, say whose move it is.
 *
 * Every change is sent to the server, which assembles the FEN and judges it.
 * That round trip is the point rather than an overhead: "you have two white
 * kings" and "Black is in check but White is to move" are rules questions,
 * and answering them here would mean a second, worse chess implementation
 * whose disagreements with the real one surface as an engine that mysteriously
 * refuses to start.
 */

function pieceSvg(symbol, size) {
  const colour = symbol === symbol.toUpperCase() ? "white" : "black";
  const kind = PIECE_LETTERS[symbol.toLowerCase()];
  const holder = svg("svg", { viewBox: "0 0 45 45" });
  if (size) { holder.setAttribute("width", size); holder.setAttribute("height", size); }
  const use = svg("use", { href: `#piece-${colour}-${kind}` });
  use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href",
                     `#piece-${colour}-${kind}`);
  holder.appendChild(use);
  return holder;
}

function renderPalette() {
  const box = $("setup-palette");
  box.innerHTML = "";
  for (const row of PALETTE) {
    for (const symbol of row) {
      if (symbol === null) { box.appendChild(el("span")); continue; }

      const button = el("button", state.setup.stamp === symbol ? "on" : "");
      if (symbol === "eraser") {
        button.appendChild(el("span", "eraser", "✖"));
        button.title = "Erase -- or right-click any square";
      } else {
        button.appendChild(pieceSvg(symbol));
        button.title = symbol;
      }
      button.addEventListener("click", () => {
        state.setup.stamp = symbol;
        renderPalette();
      });
      box.appendChild(button);
    }
  }
}

function renderEditingBoard() {
  const pieces = state.setup.pieces;

  clear("layer-lastmove");
  clear("layer-check");
  clear("layer-hints");
  clear("layer-arrows");
  clear("layer-badge");

  const pieceLayer = clear("layer-pieces");
  for (const [square, symbol] of Object.entries(pieces)) {
    const { x, y } = squareToXY(square);
    const colour = symbol === symbol.toUpperCase() ? "white" : "black";
    const kind = PIECE_LETTERS[symbol.toLowerCase()];
    const use = svg("use", {
      href: `#piece-${colour}-${kind}`, x, y, width: SQ, height: SQ,
      class: "piece",
    });
    use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href",
                       `#piece-${colour}-${kind}`);
    pieceLayer.appendChild(use);
  }

  renderCoords();

  $("who-top").innerHTML = "";
  $("who-bottom").innerHTML = "";
  $("move-caption").textContent = "arranging — click squares to place pieces";
  $("board-note").textContent =
    "Right-click a square to clear it. Pick "
    + (state.setup.turn === "w" ? "White" : "Black")
    + " to play below, then start.";
}

function stampSquare(name) {
  const pieces = state.setup.pieces;
  const stamp = state.setup.stamp;

  if (stamp === "eraser" || pieces[name] === stamp) {
    // Clicking the piece that is already there removes it, so placing and
    // unplacing is the same click rather than a trip to the eraser.
    delete pieces[name];
  } else {
    pieces[name] = stamp;
  }
  renderBoard();
  validateSetup();
}

function eraseSquare(name) {
  if (!state.editing) return;
  delete state.setup.pieces[name];
  renderBoard();
  validateSetup();
}

function fenField(fen, index) {
  return (fen || "").split(" ")[index] || "";
}

let setupTimer = null;
function validateSetup() {
  clearTimeout(setupTimer);
  setupTimer = setTimeout(runSetupCheck, 120);
}

async function runSetupCheck() {
  const setup = state.setup;
  try {
    setup.info = await api("POST", "/api/position", {
      pieces: setup.pieces,
      turn: setup.turn,
      castling: setup.castling,
      enPassant: setup.ep,
    });
    // The server drops castling rights and en-passant squares the position
    // cannot support, so adopt what it kept rather than arguing with it.
    setup.castling = fenField(setup.info.fen, 2).replace("-", "");
    setup.ep = fenField(setup.info.fen, 3) || "-";
  } catch (error) {
    setup.info = { valid: false, problems: [error.message], fen: "",
                   castling: {}, enPassantOptions: [] };
  }
  renderSetupState();
}

function renderSetupState() {
  const info = state.setup.info;
  const status = $("setup-status");
  const go = $("btn-live-start");

  if (!info) { status.textContent = ""; return; }

  if (info.valid && info.gameOver) {
    status.className = "small setup-status bad";
    status.textContent = info.outcome;
    go.disabled = true;
  } else if (info.valid) {
    const material = info.material || {};
    const edge = material.diff > 0 ? `White +${material.diff}`
      : material.diff < 0 ? `Black +${-material.diff}` : "material level";
    status.className = "small setup-status good";
    status.textContent =
      `Legal position — ${info.turn} to play, ${edge}`
      + (info.check ? ", and in check" : "") + `.\n${info.fen}`;
    go.disabled = false;
  } else {
    status.className = "small setup-status bad";
    status.textContent = info.problems.join("\n");
    go.disabled = true;
  }

  // Only offer castling for a king and rook still at home; a tick box that
  // cannot mean anything is worse than no tick box.
  const castling = $("setup-castling");
  castling.innerHTML = "";
  const names = { K: "White 0-0", Q: "White 0-0-0",
                  k: "Black 0-0", q: "Black 0-0-0" };
  let anyPossible = false;
  for (const flag of "KQkq") {
    const possible = !!(info.castling || {})[flag];
    anyPossible = anyPossible || possible;
    const label = el("label", possible ? "" : "off");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.setup.castling.includes(flag);
    box.disabled = !possible;
    box.addEventListener("change", () => {
      const kept = new Set(state.setup.castling.split(""));
      if (box.checked) kept.add(flag); else kept.delete(flag);
      state.setup.castling = "KQkq".split("").filter((f) => kept.has(f)).join("");
      validateSetup();
    });
    label.appendChild(box);
    label.appendChild(el("span", "", names[flag]));
    castling.appendChild(label);
  }
  if (!anyPossible) {
    castling.appendChild(el("span", "small dim",
      "none possible — no king and rook are on their home squares"));
  }

  const options = info.enPassantOptions || [];
  $("setup-ep-field").classList.toggle("hidden", !options.length);
  const select = $("setup-ep");
  select.innerHTML = "";
  for (const value of ["-", ...options]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === "-" ? "not available" : value;
    select.appendChild(option);
  }
  select.value = state.setup.ep;

  if (document.activeElement !== $("setup-fen")) {
    $("setup-fen").value = info.fen || "";
  }
}

function loadSetupFen(fen) {
  const text = (fen || "").trim();
  if (!text) return;
  state.setup.pieces = parseFen(text);
  const turn = fenField(text, 1);
  state.setup.turn = turn === "b" ? "b" : "w";
  state.setup.castling = fenField(text, 2).replace("-", "");
  state.setup.ep = fenField(text, 3) || "-";
  $("setup-turn").value = state.setup.turn;
  renderBoard();
  validateSetup();
}

function setupFromBoard() {
  // Whatever position you are looking at, as a starting point. Adjusting a
  // real position by two pieces is far more common than building one from
  // an empty board.
  loadSetupFen(currentFen());
  say("Copied the position on the board into the editor.");
}

/** Whether the board should currently be an editor rather than a game. */
function syncEditing() {
  const wanted = state.leftTab === "live"
    && $("live-kind").value === "setup"
    && !state.live;

  state.editing = wanted;
  $("setup-panel").classList.toggle("hidden", !wanted);
  $("board").classList.toggle("editing", wanted);
  $("btn-live-start").textContent = wanted
    ? "Analyse this position" : "Start watching";
  $("btn-live-start").disabled = false;

  if (wanted) {
    renderPalette();
    if (!Object.keys(state.setup.pieces).length) {
      // An empty board is a worse starting point than the position already
      // in front of you, which is usually the one being asked about.
      loadSetupFen(currentFen());
    } else {
      validateSetup();
    }
  }
  render();
}

// ------------------------------------------------------------- navigation

function goTo(ply) {
  const total = line().length - 1;
  state.ply = Math.max(0, Math.min(total, ply));
  state.pick = null;
  state.dests = [];
  state.checkSquare = null;
  render();
  scheduleEngine();
  refreshCheck();
}

const step = (delta) => goTo(state.ply + delta);

/* Scrolling the board walks the game.
 *
 * Two things stop this feeling broken. Deltas are normalised, because a wheel
 * notch is 100 in most browsers but 3 in line mode and 1 in page mode, so
 * acting on the raw number means one app behaves differently per browser. And
 * a step needs an accumulated threshold rather than firing per event: a
 * trackpad sends a stream of single-digit deltas, and one flick would
 * otherwise skip through ten moves.
 */
const WHEEL_THRESHOLD = 26;
const WHEEL_GESTURE = 400;
let wheelTotal = 0;
let wheelAt = 0;

function wheelDelta(event) {
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 100 : 1;
  const now = Date.now();
  if (now - wheelAt > WHEEL_GESTURE) wheelTotal = 0;   // a new gesture
  wheelAt = now;
  wheelTotal += event.deltaY * scale;
  if (Math.abs(wheelTotal) < WHEEL_THRESHOLD) return 0;
  const direction = wheelTotal > 0 ? 1 : -1;
  wheelTotal = 0;
  return direction;
}

function onWheel(event) {
  // While arranging, the board is not a game and there is nothing to step
  // through -- so let the page scroll normally instead of swallowing it.
  if (state.editing) return;
  event.preventDefault();
  const direction = wheelDelta(event);
  if (direction) step(direction);
}

function squareFromEvent(event) {
  const box = $("board").getBoundingClientRect();
  return xyToSquare(((event.clientX - box.left) / box.width) * 800,
                    ((event.clientY - box.top) / box.height) * 800);
}

async function refreshCheck() {
  // Whether the king is in check is a rules question, so the server answers
  // it. It is cosmetic, so a failure is silent.
  const fen = currentFen();
  try {
    const data = await api("GET", `/api/legal?fen=${encodeURIComponent(fen)}`);
    if (fen !== currentFen()) return;
    const pieces = parseFen(fen);
    state.checkSquare = data.check
      ? findKing(pieces, data.turn === "white") : null;
    renderBoard();
  } catch (_) { /* cosmetic */ }
}

async function onSquare(name) {
  if (state.editing) { stampSquare(name); return; }

  const fen = currentFen();
  if (state.pick && state.dests.includes(name)) {
    const from = state.pick;
    state.pick = null;
    state.dests = [];
    await playMove(from + name);
    return;
  }
  if (state.pick === name) {
    state.pick = null;
    state.dests = [];
    renderBoard();
    return;
  }
  const data = await guard(() =>
    api("GET", `/api/legal?fen=${encodeURIComponent(fen)}&square=${name}`));
  const dests = data && data.moves ? data.moves[name] || [] : [];
  state.pick = dests.length ? name : null;
  state.dests = dests;
  renderBoard();
}

/** Where a move belongs in the tree. Pure: it decides, it does not act.
 *
 *  "walk"   - it is already the next move of this line
 *  "enter"  - a variation from here already starts with it
 *  "append" - it continues the variation we are standing at the end of
 *  "create" - a new variation, hanging off `owner`
 */
function resolveMove(uci) {
  const head = uci.slice(0, 4);
  const next = line()[state.ply + 1];
  if (next && next.uci && next.uci.slice(0, 4) === head) {
    return { action: "walk" };
  }

  // Whichever line actually owns this position. Standing on the part of a
  // variation that it shares with its parent, a new move belongs to the
  // parent -- otherwise the tree grows branches off moves they precede.
  let owner = state.active;
  for (;;) {
    const variation = state.vars.get(owner);
    if (!variation || state.ply > variation.from) break;
    owner = variation.parent;
  }

  const twin = branchesAt(owner, state.ply).find(
    (variation) => variation.moves[0]
      && variation.moves[0].uci.slice(0, 4) === head);
  if (twin) return { action: "enter", id: twin.id };

  const current = state.vars.get(state.active);
  if (current && owner === state.active
      && state.ply === current.from + current.moves.length) {
    return { action: "append", owner };
  }
  return { action: "create", owner };
}

async function playMove(uci) {
  if (state.mode === "live") {
    const session = state.live;
    if (!session || (session.kind !== "manual" && session.kind !== "pgn")) {
      say("This session follows a real game, so moves come from the source.");
      return;
    }
    const payload = await guard(() =>
      api("POST", `/api/live/${session.id}/move`, { uci }));
    if (payload) applyLive(payload);
    return;
  }

  const fen = currentFen();
  const plan = resolveMove(uci);

  if (plan.action === "walk") {
    goTo(state.ply + 1);                    // it is this line's own next move
    return;
  }
  if (plan.action === "enter") {
    state.active = plan.id;                 // a variation we already have
    goTo(state.ply + 1);
    return;
  }

  const payload = await guard(() => api("POST", "/api/play", { fen, uci }));
  if (!payload) return;

  // Both the move number and whose move it is are already in the FEN we
  // moved from -- fields 6 and 2 -- so there is nothing to work out.
  const fields = fen.split(" ");
  const move = {
    ply: state.ply + 1,
    san: payload.san,
    uci: payload.uci,
    fen: payload.fen,
    moveNumber: parseInt(fields[5], 10) || 1,
    color: fields[1] === "w" ? "white" : "black",
    clock: null,
    branch: true,
  };

  if (plan.action === "append") {
    state.vars.get(state.active).moves.push(move);
  } else {
    const id = `v${(state.varSeq += 1)}`;
    state.vars.set(id,
      { id, parent: plan.owner, from: state.ply, moves: [move] });
    state.active = id;
  }
  goTo(state.ply + 1);
}

// --------------------------------------------------------------- library

async function loadLibrary() {
  const payload = await guard(() => api("GET", "/api/library"));
  if (!payload) return;
  state.library = payload.games;
  renderLibrary();
}

function renderLibrary() {
  const box = $("library-list");
  box.innerHTML = "";
  $("library-count").textContent = state.library.length
    ? `${state.library.length} games` : "";

  if (!state.library.length) {
    box.appendChild(el("div", "empty",
      "Nothing saved yet. Paste a game URL above, or pick a player on the "
      + "Lichess or Chess.com tab."));
    return;
  }

  for (const game of state.library) {
    const item = el("div", "item" + (state.game && state.game.id === game.id
      ? " on" : ""));
    const line1 = el("div", "line1");
    line1.appendChild(el("span", "names",
      `${game.white || "?"} - ${game.black || "?"}`));
    line1.appendChild(el("span", "res", game.result || "*"));
    item.appendChild(line1);

    const line2 = el("div", "line2");
    if (game.source) line2.appendChild(el("span", "", game.source));
    if (game.speed) line2.appendChild(el("span", "", game.speed));
    if (game.plyCount) line2.appendChild(el("span", "", `${game.plyCount} plies`));
    if (game.reviewed) {
      const white = game.accuracy.white, black = game.accuracy.black;
      line2.appendChild(el("span", "acc",
        `${white == null ? "?" : white}% / ${black == null ? "?" : black}%`));
    }
    item.appendChild(line2);

    item.addEventListener("click", () => openGame(game.id));
    const remove = el("button", "ghost small-btn danger", "Delete");
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      await guard(() => api("DELETE", `/api/games/${game.id}`));
      if (state.game && state.game.id === game.id) {
        state.game = null; state.moves = []; state.review = null; render();
      }
      loadLibrary();
    });
    const actions = el("div", "actions");
    actions.appendChild(remove);
    item.appendChild(actions);
    box.appendChild(item);
  }
}

async function openGame(gameId) {
  const payload = await guard(() => api("GET", `/api/games/${gameId}`));
  if (payload) applyGame(payload);
}

function applyGame(payload) {
  stopLive();
  state.editing = false;
  $("board").classList.remove("editing");
  $("setup-panel").classList.add("hidden");
  state.mode = "game";
  state.game = payload.game;
  state.moves = payload.moves || [];
  state.review = payload.review || null;
  clearVariations();
  state.ply = 0;
  state.lines = null;
  renderLibrary();
  render();
  goTo(state.review ? 0 : 0);
  setRightTab(state.review ? "review" : "review");
  say(`${state.game.white} vs ${state.game.black} loaded`
      + (state.review ? " with its saved review" : ""));
}

async function doImport(text) {
  if (!text || !text.trim()) { say("Paste something first."); return; }
  say("Fetching...");
  const payload = await guard(() => api("POST", "/api/import", { text }));
  if (!payload) return;
  applyGame(payload);
  loadLibrary();
}

// ----------------------------------------------------------- user lookups

function gameRow(game, onOpen) {
  const item = el("div", "item");
  const line1 = el("div", "line1");
  const white = game.white || {}, black = game.black || {};
  line1.appendChild(el("span", "names",
    `${white.name || "?"}${white.rating ? ` (${white.rating})` : ""}`
    + ` - ${black.name || "?"}${black.rating ? ` (${black.rating})` : ""}`));
  line1.appendChild(el("span", "res", game.result || "*"));
  item.appendChild(line1);

  const line2 = el("div", "line2");
  if (game.speed) line2.appendChild(el("span", "", game.speed));
  if (game.rated) line2.appendChild(el("span", "", "rated"));
  if (!game.finished) line2.appendChild(el("span", "acc", "in progress"));
  if (game.opening && game.opening.name) {
    line2.appendChild(el("span", "", game.opening.name));
  }
  item.appendChild(line2);
  item.addEventListener("click", () => onOpen(game));
  return item;
}

async function loadLichessGames() {
  const user = $("lichess-user").value.trim();
  if (!user) { say("Type a Lichess username."); return; }
  localStorage.setItem("ca.lichess", user);
  say(`Loading ${user}'s games...`);
  const payload = await guard(() =>
    api("GET", `/api/users/lichess/${encodeURIComponent(user)}/games?limit=25`));
  if (!payload) return;
  renderUserGames($("lichess-list"), payload.games, "lichess");
  say(`${payload.games.length} games`);
}

async function loadChesscomGames() {
  const user = $("chesscom-user").value.trim();
  if (!user) { say("Type a Chess.com username."); return; }
  localStorage.setItem("ca.chesscom", user);
  say(`Loading ${user}'s games...`);
  const payload = await guard(() =>
    api("GET", `/api/users/chesscom/${encodeURIComponent(user)}/games?limit=25`));
  if (!payload) return;
  renderUserGames($("chesscom-list"), payload.games, "chesscom");
  say(`${payload.games.length} games`);
}

function renderUserGames(box, games, source) {
  box.innerHTML = "";
  if (!games.length) {
    box.appendChild(el("div", "empty", "No games found for that name."));
    return;
  }
  for (const game of games) {
    box.appendChild(gameRow(game, (chosen) => {
      // The PGN came with the listing, so opening a game costs no request.
      doImport(chosen.pgn || chosen.url);
    }));
  }
}

async function lichessCurrent() {
  const user = $("lichess-user").value.trim();
  if (!user) { say("Type a Lichess username first."); return; }
  const payload = await guard(() =>
    api("GET", `/api/users/lichess/${encodeURIComponent(user)}/current`));
  if (!payload) return;
  const game = payload.game;
  if (!game.live) {
    say(`${user} is not playing right now. Showing their last game instead.`,
        "warn");
    doImport(game.pgn || game.url);
    return;
  }
  $("live-kind").value = "lichess";
  $("live-reference").value = game.gameId;
  setLeftTab("live");
  startLive();
}

async function chesscomOngoing() {
  const user = $("chesscom-user").value.trim();
  if (!user) { say("Type a Chess.com username first."); return; }
  const payload = await guard(() =>
    api("GET", `/api/users/chesscom/${encodeURIComponent(user)}/ongoing`));
  if (!payload) return;
  const box = $("chesscom-list");
  box.innerHTML = "";
  if (!payload.games.length) {
    box.appendChild(el("div", "empty",
      "No daily games in progress. Chess.com's public API only reports "
      + "correspondence games -- for a live game, paste its URL on the Live tab."));
    return;
  }
  for (const game of payload.games) {
    box.appendChild(gameRow({ ...game, result: "*", finished: false },
      (chosen) => doImport(chosen.pgn || chosen.url)));
  }
}

// ---------------------------------------------------------------- review

function renderPresets() {
  const select = $("review-preset");
  select.innerHTML = "";
  const presets = (state.health && state.health.presets) || {};
  for (const [key, preset] of Object.entries(presets)) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = preset.label;
    select.appendChild(option);
  }
  select.value = state.settings.preset || "standard";
  showPresetDetail();
}

function showPresetDetail() {
  const presets = (state.health && state.health.presets) || {};
  const preset = presets[$("review-preset").value];
  $("preset-detail").textContent = preset ? preset.detail : "";
}

async function startReview() {
  if (!state.game) { say("Import a game first."); return; }
  const body = {
    preset: $("review-preset").value,
    engineId: state.settings.reviewEngine || null,
    threads: state.settings.threads || null,
    hashMb: state.settings.hashMb || null,
    force: false,
  };
  const payload = await guard(() =>
    api("POST", `/api/games/${state.game.id}/review`, body));
  if (!payload) return;

  if (payload.review) {
    state.review = payload.review;
    render();
    say("That review was already saved at these settings.");
    return;
  }
  state.reviewJob = payload.job;
  $("btn-review").disabled = true;
  $("btn-review-cancel").classList.remove("hidden");
  $("review-progress").classList.remove("hidden");
  pollReview();
}

function pollReview() {
  clearTimeout(state.reviewTimer);
  state.reviewTimer = setTimeout(async () => {
    if (!state.reviewJob) return;
    let job;
    try {
      job = await api("GET", `/api/jobs/${state.reviewJob.id}`);
    } catch (error) {
      finishReview();
      say(error.message, "error");
      return;
    }
    $("review-fill").style.width = `${job.percent}%`;
    $("review-message").textContent =
      `${job.message || ""}  ${job.elapsed}s`;

    if (job.state === "done") {
      state.review = job.result;
      finishReview();
      render();
      loadLibrary();
      say(`Reviewed in ${job.elapsed}s.`);
      setRightTab("review");
      return;
    }
    if (job.state === "failed" || job.state === "cancelled") {
      finishReview();
      say(job.error || "Review stopped.", job.error ? "error" : "warn");
      return;
    }
    pollReview();
  }, 500);
}

function finishReview() {
  clearTimeout(state.reviewTimer);
  state.reviewJob = null;
  $("btn-review").disabled = false;
  $("btn-review-cancel").classList.add("hidden");
  $("review-progress").classList.add("hidden");
}

async function cancelReview() {
  if (!state.reviewJob) return;
  await guard(() => api("DELETE", `/api/jobs/${state.reviewJob.id}`));
}

function renderReport() {
  const box = $("review-report");
  box.innerHTML = "";
  const review = state.review;
  if (!review) {
    box.appendChild(el("div", "empty", state.game
      ? "Not reviewed yet. Pick a depth and press the button."
      : "Import a game to review it."));
    return;
  }

  const white = review.summary.white, black = review.summary.black;
  const names = state.game || { white: "White", black: "Black" };

  const card = el("div", "scorecard");
  const left = el("div");
  left.appendChild(el("div", "who2", names.white));
  left.appendChild(el("div", "big", `${white.accuracy}`));
  card.appendChild(left);
  card.appendChild(el("div", "mid", "accuracy"));
  const right = el("div", "r");
  right.appendChild(el("div", "who2", names.black));
  right.appendChild(el("div", "big", `${black.accuracy}`));
  card.appendChild(right);
  box.appendChild(card);

  const stat = (label, a, b, title) => {
    const row = el("div", "statrow");
    row.appendChild(el("div", "", a == null ? "-" : String(a)));
    const middle = el("div", "lbl", label);
    if (title) middle.title = title;
    row.appendChild(middle);
    row.appendChild(el("div", "r", b == null ? "-" : String(b)));
    box.appendChild(row);
  };

  stat("centipawn loss", white.acpl, black.acpl,
       "Average centipawns given away per move, book moves excluded, "
       + "each move capped at 1000.");
  stat("~ game rating", white.estimatedRating, black.estimatedRating,
       `A rough fit, not a measurement: ${review.ratingFormula}`);
  stat("engine's move", pct(white.bestMoveShare), pct(black.bestMoveShare),
       "How often you played the engine's first choice.");
  stat("opening", pct(white.phases.opening), pct(black.phases.opening));
  stat("middlegame", pct(white.phases.middlegame), pct(black.phases.middlegame));
  stat("endgame", pct(white.phases.endgame), pct(black.phases.endgame));

  const opening = el("div", "opening-line");
  opening.textContent = `${review.opening.eco} ${review.opening.name}`
    + `  -  theory ran ${review.opening.bookPly} plies`;
  box.appendChild(opening);

  const heading = el("h4");
  heading.textContent = "Moves";
  const why = el("button", "ghost small-btn", "what do these mean?");
  why.style.marginLeft = "8px";
  why.addEventListener("click", showRules);
  heading.appendChild(why);
  box.appendChild(heading);

  for (const label of LABEL_ORDER) {
    const a = white.counts[label] || 0, b = black.counts[label] || 0;
    if (!a && !b) continue;
    const row = el("div", "labelrow");
    row.appendChild(el("div", "", String(a)));
    const middle = el("div", "mid");
    const chip = el("span", "chip", (state.glyphs || {})[label] || "");
    chip.style.background = labelColor(label);
    middle.appendChild(chip);
    middle.appendChild(el("span", "", label));
    if (review.labelRules && review.labelRules[label]) {
      middle.title = review.labelRules[label];
    }
    row.appendChild(middle);
    row.appendChild(el("div", "r", String(b)));
    box.appendChild(row);
  }

  const lichessHeading = el("h4");
  lichessHeading.textContent = "On the Lichess scale";
  lichessHeading.title = "Inaccuracy, mistake and blunder at 10, 20 and 30 "
    + "winning-chance points lost. Published, so these are exact.";
  box.appendChild(lichessHeading);
  for (const key of ["inaccuracy", "mistake", "blunder"]) {
    const row = el("div", "labelrow");
    row.appendChild(el("div", "", String(white.judgments[key])));
    row.appendChild(el("div", "mid", key));
    row.appendChild(el("div", "r", String(black.judgments[key])));
    box.appendChild(row);
  }

  if (review.moments.length) {
    box.appendChild(Object.assign(el("h4"), { textContent: "Turning points" }));
    const list = el("div", "moments");
    const rows = new Map(review.moves.map((row) => [row.ply, row]));
    for (const ply of review.moments) {
      const row = rows.get(ply);
      if (!row) continue;
      const item = el("div", "moment");
      const chip = el("span", "chip", row.glyph);
      chip.style.background = labelColor(row.label);
      item.appendChild(chip);
      item.appendChild(el("span", "mv",
        `${row.moveNumber}${row.color === "white" ? "." : "..."} ${row.san}`));
      item.appendChild(el("span", "why",
        `-${row.winLoss}%  ${row.bestSan ? `best ${row.bestSan}` : ""}`));
      item.addEventListener("click", () => { state.active = null; goTo(ply); });
      list.appendChild(item);
    }
    box.appendChild(list);
  }

  const footer = el("p", "small dim");
  footer.style.marginTop = "12px";
  footer.textContent = `${review.engine.name}, ${review.settings.preset}, `
    + `${review.elapsed}s.`;
  box.appendChild(footer);
}

const pct = (value) => (value == null ? null : `${value}%`);

function showRules() {
  const dialog = $("dlg-rules");
  $("rules-intro").textContent =
    "The Lichess three are published thresholds. The rest are this app's own "
    + "rules, written out so a surprising label can be checked rather than "
    + "guessed at. Chess.com has never published its criteria, so nothing here "
    + "claims to reproduce them.";
  const box = $("rules-list");
  box.innerHTML = "";
  const rules = (state.health && state.health.labelRules) || {};
  for (const label of LABEL_ORDER) {
    if (!rules[label]) continue;
    const row = el("div", "rule");
    const chip = el("span", "chip", (state.glyphs || {})[label] || "");
    chip.style.background = labelColor(label);
    row.appendChild(chip);
    row.appendChild(el("span", "nm", label));
    row.appendChild(el("span", "tx", rules[label]));
    box.appendChild(row);
  }
  dialog.showModal();
}

// ------------------------------------------------------------- move list

/* The move list always shows the whole game, and hangs the variations off it
 * in brackets, the way Lichess and Chess.com do. Previously it rendered only
 * the current line, so trying a move made the rest of the game disappear. */
function renderMoveList() {
  const box = $("move-list");
  box.innerHTML = "";

  const main = state.mode === "live" ? line() : state.moves;
  if (main.length <= 1) {
    box.appendChild(el("div", "empty", "No moves."));
    return;
  }

  const judged = new Map();
  if (state.review) {
    for (const row of state.review.moves) judged.set(row.ply, row);
  }

  let current = null;
  for (let ply = 1; ply < main.length; ply += 1) {
    const item = main[ply];
    if (item.color === "white" || current === null) {
      current = el("div", "moverow");
      // A black move opening a row -- which happens after a variation has
      // been interposed -- is numbered "6..." and not "6.", the same as it
      // would be written down.
      current.appendChild(el("div", "num",
        `${item.moveNumber}${item.color === "white" ? "." : "..."}`));
      if (item.color === "black") current.appendChild(el("div"));
      box.appendChild(current);
    }

    const row = judged.get(ply);
    const onGame = state.active === null && ply === state.ply;
    const cell = el("div", "mv" + (onGame ? " on" : ""));
    if (row) {
      const chip = el("span", "chip", row.glyph);
      chip.style.background = labelColor(row.label);
      chip.title = row.label;
      cell.appendChild(chip);
    }
    cell.appendChild(el("span", "san", item.san));
    if (row) cell.appendChild(el("span", "ev", row.evalAfter.text));
    cell.addEventListener("click", () => { state.active = null; goTo(ply); });
    current.appendChild(cell);

    // A variation departing from the position *before* this move is an
    // alternative to it, so it reads after it -- 5...O-O (5...d5 6.Bb5 ...).
    const alternatives = branchesAt(null, ply - 1);
    if (alternatives.length) {
      renderVariations(box, alternatives, 0);
      current = null;                 // the next move starts a fresh row
    }
  }

  // Anything tried from the final position has no move to follow.
  renderVariations(box, branchesAt(null, main.length - 1), 0);

  const active = box.querySelector(".mv.on, .vm.on");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderVariations(box, variations, depth) {
  for (const variation of variations) {
    const holder = el("div", "variation");
    holder.style.marginLeft = `${depth * 12}px`;
    holder.appendChild(el("span", "paren", "( "));
    fillVariation(holder, variation, depth);
    holder.appendChild(el("span", "paren", " )"));
    box.appendChild(holder);
  }
}

function fillVariation(holder, variation, depth) {
  // Real spaces between the tokens, not margins. Chess notation is words:
  // without them "6.Nd4 Kh8" renders as "6.Nd4Kh8", and selecting the line to
  // copy it gives you back something that is not a move list.
  const space = () => holder.appendChild(document.createTextNode(" "));

  variation.moves.forEach((item, index) => {
    const ply = variation.from + 1 + index;
    if (index > 0) space();

    // Number the first move always, and every white move after it: that is
    // what makes "5...d5 6.Bb5 Bg4" readable rather than a run of words.
    if (index === 0 || item.color === "white") {
      holder.appendChild(el("span", "vnum",
        `${item.moveNumber}${item.color === "white" ? "." : "..."}`));
    }

    const here = state.active === variation.id && ply === state.ply;
    const cell = el("span", "vm" + (here ? " on" : ""), item.san);
    cell.addEventListener("click", () => {
      state.active = variation.id;
      goTo(ply);
    });
    holder.appendChild(cell);

    // Variations off this variation, nested in their own brackets.
    for (const child of branchesAt(variation.id, ply)) {
      space();
      const inner = el("span", "vnest");
      inner.appendChild(el("span", "paren", "("));
      fillVariation(inner, child, depth + 1);
      inner.appendChild(el("span", "paren", ")"));
      holder.appendChild(inner);
    }
  });
}

// ----------------------------------------------------------------- graph

function renderGraph() {
  const graph = clear("graph");
  const review = state.review;
  if (!review || !review.graph.length) {
    $("graph-phases").textContent = "";
    return;
  }

  const points = review.graph;
  const width = 1000, height = 120;
  const stepX = width / Math.max(1, points.length - 1);
  const toY = (white) => height - (white / 100) * height;

  const top = [`M 0 0`];
  for (let index = 0; index < points.length; index += 1) {
    top.push(`L ${(index * stepX).toFixed(1)} ${toY(points[index].white).toFixed(1)}`);
  }
  top.push(`L ${width} 0 Z`);
  graph.appendChild(svg("path", { d: top.join(" "), class: "area-black" }));

  const bottom = [`M 0 ${height}`];
  for (let index = 0; index < points.length; index += 1) {
    bottom.push(`L ${(index * stepX).toFixed(1)} ${toY(points[index].white).toFixed(1)}`);
  }
  bottom.push(`L ${width} ${height} Z`);
  graph.appendChild(svg("path", { d: bottom.join(" "), class: "area-white" }));

  graph.appendChild(svg("line", {
    x1: 0, y1: height / 2, x2: width, y2: height / 2, class: "zero",
  }));

  for (const row of review.moves) {
    if (!["blunder", "mistake", "miss", "brilliant", "great"].includes(row.label)) {
      continue;
    }
    graph.appendChild(svg("circle", {
      cx: (row.ply * stepX).toFixed(1), cy: toY(
        points[row.ply] ? points[row.ply].white : 50).toFixed(1),
      r: 7, fill: labelColor(row.label), class: "mark",
    }));
  }

  graph.appendChild(svg("line", {
    x1: (graphPly() * stepX).toFixed(1), y1: 0,
    x2: (graphPly() * stepX).toFixed(1), y2: height, class: "cursor",
  }));

  const phases = $("graph-phases");
  phases.innerHTML = "";
  const { middlegame, endgame } = review.phases;
  const total = points.length - 1 || 1;
  const spans = [
    ["opening", 0, Math.min(middlegame, total)],
    ["middlegame", middlegame, Math.min(endgame, total)],
    ["endgame", endgame, total],
  ];
  for (const [name, from, to] of spans) {
    if (to <= from) continue;
    const span = el("span", "", name);
    span.style.width = `${((to - from) / total) * 100}%`;
    phases.appendChild(span);
  }
}

/** Where the cursor belongs on the graph, which only plots the real game. */
function graphPly() {
  const depth = mainlineDepth();
  return depth === Infinity ? state.ply : Math.min(state.ply, depth);
}

function graphClick(event) {
  const review = state.review;
  if (!review) return;
  const box = $("graph").getBoundingClientRect();
  const share = (event.clientX - box.left) / box.width;
  state.active = null;                    // the graph is the game, not a variation
  goTo(Math.round(share * (review.graph.length - 1)));
}

// -------------------------------------------------------- engine panel

function scheduleEngine() {
  clearTimeout(state.engineTimer);
  if (state.editing) return;
  if (!state.showLines && state.rightTab !== "engine" && state.mode !== "live") {
    return;
  }
  // Something already knows the answer for this position -- the review, or a
  // live session's own analysis thread. Asking the engine again would be
  // slower, shallower and pure waste while scrolling through a game.
  if (linesFor()) {
    renderStrip();
    renderBestLine();
    renderLines();
    return;
  }
  state.engineTimer = setTimeout(runEngine, 250);
}

async function runEngine() {
  const fen = currentFen();
  const token = ++state.engineToken;
  $("engine-note").textContent = "thinking...";
  renderStrip();                     // so the strip says "thinking" meanwhile
  try {
    const payload = await api("POST", "/api/eval", {
      fen,
      multipv: linesCount(),
      movetime: parseFloat($("lines-time").value) || 0.6,
      engineId: state.settings.analysisEngine || null,
    });
    if (token !== state.engineToken) return;      // a later position won
    state.lines = { ...payload, fen };
    renderLines();
    renderStrip();
    renderBestLine();
    renderEvalBar();
    $("engine-note").textContent = `${payload.engine}  depth ${payload.depth}`;
  } catch (error) {
    if (token === state.engineToken) $("engine-note").textContent = error.message;
  }
}

function linesCount() {
  const value = parseInt($("lines-count").value, 10);
  return Math.max(1, Math.min(5, Number.isFinite(value) ? value : 3));
}

/** The review's own lines for the position on the board, if it has them.
 *
 * A reviewed game already analysed every position, several variations deep
 * and usually deeper than a live call would manage. So while you scroll
 * through one the lines are already here: instant, and free. `moves[ply]` is
 * the row whose `fenBefore` is the position at that ply, which is why the
 * index is the ply itself rather than ply - 1.
 */
function storedLines() {
  if (!state.review || state.mode === "live") return null;
  if (state.ply > mainlineDepth()) return null;
  const row = state.review.moves[state.ply];
  if (!row || !row.alternatives || !row.alternatives.length) return null;
  // Belt and braces: never show one position's lines against another's board.
  if (row.fenBefore !== currentFen()) return null;
  return { lines: row.alternatives, source: "review" };
}

/** The best lines available for the position on the board, and where from. */
function linesFor() {
  if (state.mode === "live" && state.live && state.live.analysis
      && atLatest() && !state.live.stale) {
    return { lines: state.live.analysis.lines, source: "live" };
  }
  const stored = storedLines();
  if (stored) return stored;
  if (state.lines && state.lines.fen === currentFen()) {
    return { lines: state.lines.lines, source: "engine" };
  }
  return null;
}

function evalChip(text) {
  const chip = el("span", "evchip", text || "-");
  if (text && text.startsWith("+")) chip.classList.add("plus");
  else if (text && text.startsWith("-")) chip.classList.add("minus");
  return chip;
}

/* The ranked lines above the board. Kept small and kept up: while the toggle
 * is lit these follow whatever position you are looking at, so stepping
 * through a game reads like the engine is talking the whole way. */
function renderStrip() {
  const strip = $("engine-strip");
  const wanted = state.showLines && !state.editing;
  strip.classList.toggle("hidden", !wanted);

  const rows = wanted ? linesCount() : 0;
  if (!wanted) return;

  strip.innerHTML = "";
  const found = linesFor();
  const lines = found ? found.lines.slice(0, rows) : [];

  for (const item of lines) {
    const row = el("div", "row");
    row.appendChild(evalChip(item.text));
    row.appendChild(el("span", "pv", item.line));
    if (item.depth) row.appendChild(el("span", "dp", `d${item.depth}`));
    if (item.uci) row.addEventListener("click", () => playMove(item.uci));
    strip.appendChild(row);
  }

  if (found && found.source === "review" && strip.lastChild) {
    // Say where they came from: these are the review's numbers, at the
    // review's depth, not something computed a moment ago.
    strip.lastChild.appendChild(el("span", "src", "from the review"));
  }

  // Pad to a constant height. An arriving evaluation must not move the board.
  for (let index = lines.length; index < rows; index += 1) {
    const row = el("div", "row blank quiet");
    if (index === 0) {
      row.appendChild(el("span", "", state.health && state.health.hasEngine
        ? "thinking..." : "no engine yet - the engine pill can download one"));
    }
    strip.appendChild(row);
  }
}

/* The engine's preferred continuation, beside the board controls.
 *
 * On a reviewed move this is deliberately retrospective -- the line you could
 * have played instead -- because that is the question you are asking while
 * reading a review. Anywhere else it is the best line from here. */
function renderBestLine() {
  const box = $("best-line");
  box.innerHTML = "";
  if (state.editing) return;

  const row = reviewRow();
  if (row && row.bestLine) {
    const top = (row.alternatives || [])[0];
    box.appendChild(evalChip(top ? top.text : ""));
    box.appendChild(el("span", "pv", row.bestLine));
    return;
  }

  const found = linesFor();
  if (found && found.lines.length) {
    box.appendChild(evalChip(found.lines[0].text));
    box.appendChild(el("span", "pv", found.lines[0].line));
  }
}

function renderLines() {
  const box = $("engine-lines");
  box.innerHTML = "";
  const found = linesFor();

  if (!found || !found.lines.length) {
    box.appendChild(el("div", "empty", "No lines yet."));
    return;
  }
  for (const item of found.lines.slice(0, linesCount())) {
    const row = el("div", "lineitem");
    row.appendChild(el("span", "sc", item.text));
    row.appendChild(el("span", "pv", item.line));
    row.appendChild(el("span", "dp", `d${item.depth}`));
    if (item.uci) {
      row.addEventListener("click", () => playMove(item.uci));
    }
    box.appendChild(row);
  }
}

/* How big the board may be.
 *
 * A square board sized only by width is taller than a short window, which is
 * how the graph and the controls ended up off the bottom of the screen. So the
 * height left over is measured: everything in the centre column except the
 * board, subtracted from the room the column has.
 *
 * The thing to be careful of is measuring something that your own answer
 * changes. The first version of this asked "is the board too small? then drop
 * the graph" -- and dropping the graph freed the room that made the board big
 * enough again, so it flipped the graph on and off for ever and left the board
 * a different size depending on which pass ran last. So the graph is left out
 * of `others` entirely and its height is remembered from when it was last up:
 * the decision is then made from numbers the decision cannot affect.
 *
 * The other half of the shaking was the scrollbar, and that is dead in the
 * stylesheet: `scrollbar-gutter: stable` reserves its width whether or not one
 * is showing, so the board can no longer resize the panel that sizes it.
 */

//: Below this the board is too small to read, and the graph goes instead.
const MIN_BOARD = 300;
const FLOOR_BOARD = 220;

let graphHeight = 0;

function fitBoard() {
  const centre = document.querySelector(".panel.centre");
  const wrap = document.querySelector(".board-wrap");
  const graph = document.querySelector(".graph-wrap");
  if (!centre || !wrap) return;

  // Narrow enough and the columns stack, so the board is not competing with
  // anything for height -- the page simply scrolls, which is what you want on
  // a phone-shaped window. Capping it here would only make it small for no
  // reason.
  if (window.matchMedia("(max-width: 860px)").matches) {
    centre.style.removeProperty("--board-max");
    if (graph) graph.classList.remove("hidden");
    return;
  }

  const style = getComputedStyle(centre);
  let others = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
  for (const child of centre.children) {
    if (child === wrap || child === graph) continue;
    const box = child.getBoundingClientRect();
    if (!box.height) continue;
    const own = getComputedStyle(child);
    others += box.height + parseFloat(own.marginTop) + parseFloat(own.marginBottom);
  }

  if (graph) {
    const box = graph.getBoundingClientRect();
    if (box.height) {
      const own = getComputedStyle(graph);
      graphHeight = box.height
        + parseFloat(own.marginTop) + parseFloat(own.marginBottom);
    }
  }

  // Measured down from the viewport, not from the panel: the panel grows to
  // fit its own content, so asking it how tall it is asks a question whose
  // answer this function has already changed. Where the panel *starts* is
  // decided by the header above it and nothing else.
  const available = window.innerHeight - centre.getBoundingClientRect().top - 16;
  const roomAlone = available - others - 4;
  const withGraph = roomAlone - (graphHeight || 120);
  const keepGraph = withGraph >= MIN_BOARD;

  if (graph) graph.classList.toggle("hidden", !keepGraph);
  const room = keepGraph ? withGraph : roomAlone;
  centre.style.setProperty(
    "--board-max", `${Math.max(FLOOR_BOARD, Math.round(room))}px`);
}

function paintLinesPill() {
  const pill = $("pill-lines");
  pill.classList.toggle("off", !state.showLines);
  pill.textContent = state.showLines ? "Lines on" : "Lines off";
  $("engine-toggle-note").textContent = state.showLines
    ? "The lines are showing above the board. The Lines pill in the top bar turns them off."
    : "Turn on the Lines pill in the top bar to keep these above the board at all times.";
}

function toggleLines() {
  state.showLines = !state.showLines;
  localStorage.setItem("ca.lines", state.showLines ? "1" : "0");
  paintLinesPill();
  renderStrip();
  scheduleEngine();
}

// ------------------------------------------------------------------ live

function liveHint() {
  const hints = {
    lichess: "Any game on Lichess can be followed, yours or anyone's. Paste a "
      + "game URL, a game id, or just a username.",
    chesscom: "Paste the game's URL. Chess.com publishes no way to find a live "
      + "game from a username, and the endpoint this uses is undocumented -- "
      + "if it stops answering, switch to Paste PGN.",
    manual: "Nothing is fetched. Click the moves on the board as they are "
      + "played and the engine keeps up. Works for a Chess.com blitz game, a "
      + "stream, or a board in front of you.",
    setup: "Put the pieces where they are now, say who is to move, and the "
      + "engine evaluates it. From there you can click the moves as they are "
      + "played, exactly like Follow along.",
    pgn: "Paste the PGN and paste it again as the game grows. Only ever moves "
      + "forward: a short or scrambled paste cannot rewind the game.",
  };
  $("live-hint").textContent = hints[$("live-kind").value] || "";
  const needsReference = ["lichess", "chesscom"].includes($("live-kind").value);
  $("live-reference").classList.toggle("hidden", !needsReference);
  syncEditing();
}

async function startLive() {
  const kind = $("live-kind").value;
  if (kind === "setup") {
    const info = state.setup.info;
    if (!info || !info.valid) {
      say(info ? info.problems.join(" ") : "Arrange a position first.", "error");
      return;
    }
  }
  const payload = await guard(() => api("POST", "/api/live", {
    kind,
    reference: $("live-reference").value.trim(),
    startFen: kind === "setup" ? state.setup.info.fen : null,
    engineId: state.settings.analysisEngine || null,
    movetime: parseFloat($("lines-time").value) || 0.6,
    multipv: parseInt($("lines-count").value, 10) || 3,
  }));
  if (!payload) return;
  state.mode = "live";
  clearVariations();
  applyLive(payload);
  syncEditing();                 // a running session ends the editing surface
  pollLive();
  setRightTab("engine");
  say(kind === "setup"
    ? "Analysing your position. Click the moves as they are played."
    : "Watching. The board follows the game; the engine follows the board.");
}

function applyLive(payload) {
  const wasAtLatest = !state.live
    || state.ply >= (state.live.positions || []).length - 1;
  state.live = payload;
  // Follow the game only if the board was already showing the newest move.
  // Someone who has scrolled back to look at move 12 should not be yanked
  // forward every time the players move.
  if (wasAtLatest) state.ply = (payload.positions || []).length - 1;
  else state.ply = Math.min(state.ply, (payload.positions || []).length - 1);
  render();
  renderLiveSessions();
}

function pollLive() {
  clearTimeout(state.liveTimer);
  state.liveTimer = setTimeout(async () => {
    if (state.mode !== "live" || !state.live) return;
    try {
      const payload = await api("GET", `/api/live/${state.live.id}`);
      applyLive(payload);
      if (payload.error) say(payload.error, "warn");
    } catch (error) {
      say(error.message, "error");
      return;
    }
    pollLive();
  }, 900);
}

function stopLive() {
  clearTimeout(state.liveTimer);
  if (state.live) {
    api("DELETE", `/api/live/${state.live.id}`).catch(() => {});
  }
  state.live = null;
  if (state.mode === "live") state.mode = "game";
  renderLiveSessions();
  syncEditing();
}

function renderLiveSessions() {
  const box = $("live-sessions");
  box.innerHTML = "";
  if (!state.live) return;

  const session = state.live;
  const item = el("div", "item on");
  const line1 = el("div", "line1");
  line1.appendChild(el("span", "names",
    `${session.white || "?"} - ${session.black || "?"}`));
  line1.appendChild(el("span", "res", session.status));
  item.appendChild(line1);
  const line2 = el("div", "line2");
  line2.appendChild(el("span", "", session.kind));
  line2.appendChild(el("span", "", `${session.plyCount} plies`));
  if (session.stale) line2.appendChild(el("span", "acc", "thinking"));
  item.appendChild(line2);

  const actions = el("div", "actions");
  if (session.kind === "pgn") {
    const paste = el("button", "small-btn", "Paste an update");
    paste.addEventListener("click", () => {
      state.pasteTarget = "live";
      $("paste-text").value = "";
      $("dlg-paste").showModal();
    });
    actions.appendChild(paste);
  }
  if (session.kind === "manual" || session.kind === "pgn") {
    const undo = el("button", "small-btn", "Take back");
    undo.addEventListener("click", async () => {
      const payload = await guard(() =>
        api("POST", `/api/live/${session.id}/undo`));
      if (payload) applyLive(payload);
    });
    actions.appendChild(undo);
  }
  const save = el("button", "small-btn", "Save & review");
  save.addEventListener("click", async () => {
    const payload = await guard(() =>
      api("POST", `/api/live/${session.id}/save`));
    if (!payload) return;
    stopLive();
    applyGame({ ...payload, review: null });
    loadLibrary();
    setLeftTab("library");
  });
  actions.appendChild(save);

  const stop = el("button", "small-btn danger", "Stop");
  stop.addEventListener("click", () => { stopLive(); render(); });
  actions.appendChild(stop);
  item.appendChild(actions);
  box.appendChild(item);
}

async function watchTv() {
  const payload = await guard(() => api("GET", "/api/lichess/tv"));
  if (!payload || !payload.games.length) return;
  const box = $("live-sessions");
  box.innerHTML = "";
  for (const channel of payload.games.slice(0, 8)) {
    const item = el("div", "item");
    item.appendChild(el("div", "names",
      `${channel.title ? channel.title + " " : ""}${channel.name}`
      + `${channel.rating ? ` (${channel.rating})` : ""}`));
    item.appendChild(el("div", "line2", channel.channel));
    item.addEventListener("click", () => {
      $("live-kind").value = "lichess";
      $("live-reference").value = channel.gameId;
      startLive();
    });
    box.appendChild(item);
  }
}

// --------------------------------------------------------------- engines

async function openEngines() {
  const dialog = $("dlg-engines");
  dialog.showModal();
  $("engine-found").innerHTML = "";
  $("engine-downloads").innerHTML = "<div class='empty'>Asking GitHub...</div>";
  const catalog = await guard(() => api("GET", "/api/engines"));
  if (!catalog) return;
  state.catalog = catalog;
  renderCatalog();
}

function renderCatalog() {
  const catalog = state.catalog;
  $("engine-dir").textContent = `Downloads land in ${catalog.engineDir}`;

  const found = $("engine-found");
  found.innerHTML = "";
  if (!catalog.found.length) {
    found.appendChild(el("div", "empty", "None found."));
  }
  for (const spec of catalog.found) {
    const item = el("div", "item");
    item.appendChild(el("div", "names", spec.name));
    item.appendChild(el("div", "line2", spec.path));
    found.appendChild(item);
  }

  const downloads = $("engine-downloads");
  downloads.innerHTML = "";
  if (catalog.warning) {
    downloads.appendChild(el("div", "empty", catalog.warning));
  }
  for (const spec of catalog.downloads) {
    const item = el("div", "item");
    const line1 = el("div", "line1");
    line1.appendChild(el("span", "names", spec.name));
    line1.appendChild(el("span", "res",
      `${(spec.downloadSize / 1e6).toFixed(0)} MB`));
    item.appendChild(line1);
    item.appendChild(el("div", "line2", spec.note));
    const button = el("button", "small-btn", "Download");
    button.addEventListener("click", () => installEngine(spec.id));
    const actions = el("div", "actions");
    actions.appendChild(button);
    item.appendChild(actions);
    downloads.appendChild(item);
  }

  const networks = $("engine-networks");
  networks.innerHTML = "";
  for (const net of catalog.networks) {
    const item = el("div", "item");
    const line1 = el("div", "line1");
    line1.appendChild(el("span", "names", net.name));
    line1.appendChild(el("span", "res",
      net.installed ? "installed" : `${(net.size / 1e6).toFixed(0)} MB`));
    item.appendChild(line1);
    item.appendChild(el("div", "line2", net.note));
    if (!net.installed) {
      const button = el("button", "small-btn", "Download");
      button.addEventListener("click", () => installNetwork(net.id));
      const actions = el("div", "actions");
      actions.appendChild(button);
      item.appendChild(actions);
    }
    networks.appendChild(item);
  }
}

async function installEngine(id) {
  const job = await guard(() => api("POST", "/api/engines/install", { id }));
  if (job) pollEngineJob(job.id);
}

async function installNetwork(id) {
  const job = await guard(() => api("POST", "/api/engines/network", { id }));
  if (job) pollEngineJob(job.id);
}

function pollEngineJob(jobId) {
  $("engine-job").classList.remove("hidden");
  const tick = async () => {
    let job;
    try { job = await api("GET", `/api/jobs/${jobId}`); }
    catch (error) { say(error.message, "error"); return; }

    $("engine-fill").style.width = `${job.percent}%`;
    $("engine-message").textContent = job.message
      || `${job.percent}%  (${job.elapsed}s)`;

    if (job.state === "done") {
      $("engine-job").classList.add("hidden");
      say("Installed.");
      state.catalog = await api("GET", "/api/engines");
      renderCatalog();
      await refreshHealth();
      return;
    }
    if (job.state === "failed" || job.state === "cancelled") {
      $("engine-job").classList.add("hidden");
      say(job.error || "Download stopped.", "error");
      return;
    }
    setTimeout(tick, 600);
  };
  tick();
}

// -------------------------------------------------------------- settings

function openSettings() {
  const engines = (state.health && state.health.engines) || [];
  for (const [id, key] of [["set-review-engine", "reviewEngine"],
                           ["set-analysis-engine", "analysisEngine"]]) {
    const select = $(id);
    select.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "Whatever is available";
    select.appendChild(auto);
    for (const spec of engines) {
      const option = document.createElement("option");
      option.value = spec.id;
      option.textContent = spec.name;
      select.appendChild(option);
    }
    select.value = state.settings[key] || "";
  }
  $("set-threads").value = state.settings.threads || 2;
  $("set-hash").value = state.settings.hashMb || 256;
  $("set-token").value = "";
  $("dlg-settings").showModal();
}

async function saveSettings() {
  const values = {
    reviewEngine: $("set-review-engine").value || null,
    analysisEngine: $("set-analysis-engine").value || null,
    threads: parseInt($("set-threads").value, 10) || 2,
    hashMb: parseInt($("set-hash").value, 10) || 256,
    preset: $("review-preset").value,
  };
  const payload = await guard(() => api("POST", "/api/settings", { values }));
  if (payload) state.settings = payload.settings;

  const token = $("set-token").value.trim();
  if (token) {
    await guard(() => api("POST", "/api/token", { token }));
    say("Token held for this session only; it is never written to disk.");
  }
  $("dlg-settings").close();
  refreshHealth();
}

// ------------------------------------------------------------------ shell

function setLeftTab(name) {
  state.leftTab = name;
  for (const button of $("left-tabs").children) {
    button.classList.toggle("on", button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    if (!panel.closest(".left")) continue;
    panel.classList.toggle("hidden", panel.dataset.panel !== name);
  }
  syncEditing();
}

function setRightTab(name) {
  state.rightTab = name;
  for (const button of $("right-tabs").children) {
    button.classList.toggle("on", button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    if (!panel.closest(".right")) continue;
    panel.classList.toggle("hidden", panel.dataset.panel !== name);
  }
  renderBoard();
  if (name === "engine") scheduleEngine();
}

function render() {
  renderBoard();
  renderEvalBar();
  renderStrip();
  renderBestLine();
  renderMoveList();
  renderReport();
  renderGraph();
  renderLines();
  fitBoard();
}

async function refreshHealth() {
  state.health = await guard(() => api("GET", "/api/health"));
  if (!state.health) return;
  state.settings = state.health.settings || {};
  state.glyphs = {};
  for (const entry of state.health.labels || []) {
    state.glyphs[entry.key] = entry.glyph;
  }
  const pill = $("pill-engine");
  const engines = state.health.engines || [];
  if (engines.length) {
    pill.textContent = engines[0].name;
    pill.classList.remove("warn");
    pill.title = engines[0].path;
  } else {
    pill.textContent = "no engine";
    pill.classList.add("warn");
    pill.title = "Click to download one";
  }
  renderPresets();
}

function onKey(event) {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
  if (document.querySelector("dialog[open]")) return;
  const keys = {
    ArrowRight: () => step(1),
    ArrowLeft: () => step(-1),
    ArrowUp: () => goTo(0),
    ArrowDown: () => goTo(line().length - 1),
    Home: () => goTo(0),
    End: () => goTo(line().length - 1),
    f: () => { state.flipped = !state.flipped; render(); },
  };
  const action = keys[event.key];
  if (action) { event.preventDefault(); action(); }
}

function wire() {
  $("btn-import").addEventListener("click",
    () => doImport($("import-text").value));
  $("import-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter") doImport($("import-text").value);
  });

  $("btn-paste").addEventListener("click", () => {
    state.pasteTarget = "import";
    $("paste-text").value = "";
    $("dlg-paste").showModal();
  });
  $("paste-cancel").addEventListener("click", () => $("dlg-paste").close());
  $("paste-ok").addEventListener("click", async () => {
    const text = $("paste-text").value;
    const forLive = state.pasteTarget === "live";
    $("dlg-paste").close();
    if (forLive && state.live) {
      const payload = await guard(() =>
        api("POST", `/api/live/${state.live.id}/pgn`, { pgn: text }));
      if (payload) applyLive(payload);
    } else {
      doImport(text);
    }
  });

  for (const button of $("left-tabs").children) {
    button.addEventListener("click", () => setLeftTab(button.dataset.tab));
  }
  for (const button of $("right-tabs").children) {
    button.addEventListener("click", () => setRightTab(button.dataset.tab));
  }

  $("btn-refresh-library").addEventListener("click", loadLibrary);
  $("btn-lichess-load").addEventListener("click", loadLichessGames);
  $("lichess-user").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadLichessGames();
  });
  $("btn-lichess-current").addEventListener("click", lichessCurrent);
  $("btn-chesscom-load").addEventListener("click", loadChesscomGames);
  $("chesscom-user").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadChesscomGames();
  });
  $("btn-chesscom-ongoing").addEventListener("click", chesscomOngoing);

  $("btn-first").addEventListener("click", () => goTo(0));
  $("btn-prev").addEventListener("click", () => step(-1));
  $("btn-next").addEventListener("click", () => step(1));
  $("btn-last").addEventListener("click", () => goTo(line().length - 1));
  $("btn-flip").addEventListener("click", () => {
    state.flipped = !state.flipped; render();
  });

  $("board-box").addEventListener("wheel", onWheel, { passive: false });

  // Right-click clears a square while arranging, which is quicker than going
  // back to the eraser for a single mistake.
  $("board").addEventListener("contextmenu", (event) => {
    if (!state.editing) return;
    event.preventDefault();
    const square = squareFromEvent(event);
    if (square) eraseSquare(square);
  });

  $("board").addEventListener("click", (event) => {
    const square = squareFromEvent(event);
    if (square) onSquare(square);
  });

  $("graph").addEventListener("click", graphClick);

  $("review-preset").addEventListener("change", showPresetDetail);
  $("btn-review").addEventListener("click", startReview);
  $("btn-review-cancel").addEventListener("click", cancelReview);

  $("lines-count").addEventListener("change", () => { renderStrip(); scheduleEngine(); });
  $("lines-time").addEventListener("change", scheduleEngine);
  $("pill-lines").addEventListener("click", toggleLines);

  $("live-kind").addEventListener("change", liveHint);
  $("btn-live-start").addEventListener("click", startLive);
  $("btn-live-tv").addEventListener("click", watchTv);

  $("setup-turn").addEventListener("change", () => {
    state.setup.turn = $("setup-turn").value;
    renderBoard();
    validateSetup();
  });
  $("setup-ep").addEventListener("change", () => {
    state.setup.ep = $("setup-ep").value;
    validateSetup();
  });
  $("setup-from-board").addEventListener("click", setupFromBoard);
  $("setup-start-pos").addEventListener("click", () => {
    loadSetupFen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
  });
  $("setup-clear").addEventListener("click", () => {
    state.setup.pieces = {};
    state.setup.castling = "";
    state.setup.ep = "-";
    renderBoard();
    validateSetup();
  });
  $("setup-fen").addEventListener("change", () => loadSetupFen($("setup-fen").value));

  $("pill-engine").addEventListener("click", openEngines);
  $("engines-close").addEventListener("click", () => $("dlg-engines").close());
  $("btn-settings").addEventListener("click", openSettings);
  $("settings-cancel").addEventListener("click", () => $("dlg-settings").close());
  $("settings-ok").addEventListener("click", saveSettings);
  $("rules-close").addEventListener("click", () => $("dlg-rules").close());

  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", fitBoard);
}

async function init() {
  wire();

  // Chrome will not follow <use> into an external SVG, so the sprite sheet is
  // fetched and inlined rather than referenced.
  try {
    $("sprite").innerHTML = await api("GET", "/api/pieces.svg");
  } catch (error) {
    say("Could not load the piece artwork: " + error.message, "error");
  }

  await refreshHealth();
  state.showLines = localStorage.getItem("ca.lines") !== "0";   // on by default
  paintLinesPill();
  liveHint();
  buildSquares();
  render();
  scheduleEngine();
  await loadLibrary();

  const lichessUser = localStorage.getItem("ca.lichess");
  if (lichessUser) $("lichess-user").value = lichessUser;
  const chesscomUser = localStorage.getItem("ca.chesscom");
  if (chesscomUser) $("chesscom-user").value = chesscomUser;

  if (state.health && !state.health.hasEngine) {
    say("No engine found. Click the engine pill to download one.", "warn");
  }
}

init();
