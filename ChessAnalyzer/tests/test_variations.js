/* Variation-tree tests, run against the real app.js rather than a copy.
 *
 * The functions under test are pure over `state`, so they are lifted out of
 * the shipped file by name and evaluated with a `state` this file controls.
 * Lifting rather than duplicating is the point: a copy would keep passing
 * after app.js changed underneath it, which is the one thing a test must not
 * do.
 *
 *   node ChessAnalyzer/tests/test_variations.js
 */

"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "chess_analyzer", "web", "app.js"), "utf8");

/** Cut one top-level `function name(...) { ... }` out of the source. */
function lift(name) {
  const start = SOURCE.indexOf(`function ${name}(`);
  assert.notStrictEqual(start, -1, `app.js no longer defines ${name}()`);
  let depth = 0;
  let index = SOURCE.indexOf("{", start);
  const open = index;
  for (; index < SOURCE.length; index += 1) {
    if (SOURCE[index] === "{") depth += 1;
    else if (SOURCE[index] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  assert.ok(index < SOURCE.length, `${name}() is unbalanced`);
  return SOURCE.slice(start, index + 1);
}

const NAMES = ["lineOf", "branchesAt", "mainlineDepth", "resolveMove", "line"];
const state = {};
// `line()` reaches for liveLine() on the live path, which these tests never
// take; a stub keeps the lifted source evaluable without dragging in the DOM.
const scope = { state, liveLine: () => [] };
// eslint-disable-next-line no-new-func
new Function("state", "liveLine", "exports",
  NAMES.map(lift).join("\n\n") + "\n" +
  NAMES.map((n) => `exports.${n} = ${n};`).join("\n")
)(scope.state, scope.liveLine, scope);

const { lineOf, branchesAt, mainlineDepth, resolveMove } = scope;

// ------------------------------------------------------------------ fixtures

/** A game of `count` plies, the way the import endpoint hands one over. */
function mainline(count) {
  const moves = [{ ply: 0, san: "", uci: "", fen: "start", moveNumber: 1, color: "" }];
  for (let ply = 1; ply <= count; ply += 1) {
    moves.push({
      ply,
      san: `m${ply}`,
      uci: `a${ply}a${ply}`,
      fen: `fen${ply}`,
      moveNumber: Math.floor((ply - 1) / 2) + 1,
      color: ply % 2 === 1 ? "white" : "black",
    });
  }
  return moves;
}

function reset(plies = 10) {
  state.mode = "game";
  state.moves = mainline(plies);
  state.vars = new Map();
  state.active = null;
  state.varSeq = 0;
  state.ply = 0;
}

/** Apply what resolveMove decided, the way playMove does. */
function play(uci, san) {
  const plan = resolveMove(uci);
  if (plan.action === "walk") { state.ply += 1; return plan; }
  if (plan.action === "enter") { state.active = plan.id; state.ply += 1; return plan; }

  const move = { ply: state.ply + 1, san: san || uci, uci, fen: `fen-${uci}`,
                 moveNumber: 1, color: "white", branch: true };
  if (plan.action === "append") {
    state.vars.get(state.active).moves.push(move);
  } else {
    const id = `v${(state.varSeq += 1)}`;
    state.vars.set(id, { id, parent: plan.owner, from: state.ply, moves: [move] });
    state.active = id;
  }
  state.ply += 1;
  return plan;
}

const sans = (list) => list.slice(1).map((m) => m.san).join(" ");

let passed = 0;
function check(name, body) {
  try {
    body();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    console.error(`  FAIL ${name}\n       ${error.message}`);
    process.exitCode = 1;
  }
}

// --------------------------------------------------------------------- tests

check("the game is untouched by a variation -- the reported bug", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "alt");
  // The variation is on the board...
  assert.strictEqual(state.active, "v1");
  assert.strictEqual(sans(lineOf(state.active)), "m1 m2 m3 m4 alt");
  // ...and the game itself still has all ten of its moves.
  assert.strictEqual(state.moves.length, 11);
  assert.strictEqual(sans(state.moves), "m1 m2 m3 m4 m5 m6 m7 m8 m9 m10");
  assert.strictEqual(sans(lineOf(null)), "m1 m2 m3 m4 m5 m6 m7 m8 m9 m10");
});

check("playing the game's own next move just walks it", () => {
  reset(10);
  state.ply = 3;
  assert.strictEqual(play("a4a4").action, "walk");
  assert.strictEqual(state.active, null);
  assert.strictEqual(state.ply, 4);
  assert.strictEqual(state.vars.size, 0);
});

check("a variation extends as you keep playing", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "alt1");
  assert.strictEqual(play("z2z2", "alt2").action, "append");
  assert.strictEqual(play("z3z3", "alt3").action, "append");
  assert.strictEqual(state.vars.size, 1);
  assert.strictEqual(sans(lineOf(state.active)), "m1 m2 m3 m4 alt1 alt2 alt3");
});

check("replaying the same variation re-enters it instead of copying it", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "alt");
  state.active = null;
  state.ply = 4;
  const plan = play("z1z1", "alt");
  assert.strictEqual(plan.action, "enter");
  assert.strictEqual(plan.id, "v1");
  assert.strictEqual(state.vars.size, 1, "a second copy was created");
});

check("two different tries from the same move are two siblings", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "altA");
  state.active = null;
  state.ply = 4;
  play("y1y1", "altB");
  assert.strictEqual(state.vars.size, 2);
  const siblings = branchesAt(null, 4);
  assert.strictEqual(siblings.length, 2);
  assert.deepStrictEqual(siblings.map((v) => v.moves[0].san).sort(), ["altA", "altB"]);
});

check("a variation off a variation nests under it", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "altA");
  play("z2z2", "altB");
  state.ply = 5;                       // back to just after altA
  const plan = play("y9y9", "sub");
  assert.strictEqual(plan.action, "create");
  assert.strictEqual(state.vars.get(state.active).parent, "v1");
  assert.strictEqual(sans(lineOf(state.active)), "m1 m2 m3 m4 altA sub");
  // The parent variation still has both of its own moves.
  assert.strictEqual(sans(lineOf("v1")), "m1 m2 m3 m4 altA altB");
});

check("a move played on the shared prefix belongs to the parent line", () => {
  // Standing inside a variation but on a position it shares with the game,
  // a new move is an alternative to the *game's* move, not to the variation.
  reset(10);
  state.ply = 4;
  play("z1z1", "alt");
  state.ply = 2;                       // still "in" v1, but on shared ground
  const plan = play("y5y5", "early");
  assert.strictEqual(plan.action, "create");
  assert.strictEqual(plan.owner, null, "it should hang off the game");
  assert.strictEqual(state.vars.get(state.active).parent, null);
  assert.strictEqual(sans(lineOf(state.active)), "m1 m2 early");
});

check("mainlineDepth marks where the real game stops", () => {
  reset(10);
  assert.strictEqual(mainlineDepth(), Infinity);   // on the game itself
  state.ply = 6;
  play("z1z1", "alt");
  assert.strictEqual(mainlineDepth(), 6);
  play("z2z2", "alt2");
  assert.strictEqual(mainlineDepth(), 6, "extending must not move the boundary");
  state.ply = 7;
  play("y9y9", "sub");
  assert.strictEqual(mainlineDepth(), 6, "a nested variation keeps the game's boundary");
});

check("branchesAt only answers for the line that owns the position", () => {
  reset(10);
  state.ply = 4;
  play("z1z1", "alt");
  assert.strictEqual(branchesAt(null, 4).length, 1);
  assert.strictEqual(branchesAt(null, 5).length, 0);
  assert.strictEqual(branchesAt("v1", 4).length, 0);
});

check("lineOf(null) is always the game, whatever is being explored", () => {
  reset(6);
  state.ply = 1;
  play("z1z1", "a");
  play("z2z2", "b");
  state.ply = 3;
  play("z3z3", "c");
  assert.strictEqual(sans(lineOf(null)), "m1 m2 m3 m4 m5 m6");
});

console.log(`\n  ${passed} passed`);
