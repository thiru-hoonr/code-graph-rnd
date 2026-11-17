// Reference parser for "functions expected" in JavaScript / JSX.
//
// Uses the TypeScript compiler (Microsoft's official parser, also what VS Code uses for JS) — not
// tree-sitter, so it is independent of code-review-graph.
//
// Usage: node functions.mjs <repo root> <file> [<file> ...]   (paths relative to the repo root)
// Prints JSON: { "<file>": { "named": [{name, line, end, pattern}], "unnamed": [{line, end}] } }
//              or { "<file>": { "error": "<message>" } } when the file cannot be read.
//
// Named = expected (agreed 2026-11-17). Pattern numbers match the table in the notes:
//   1 function declaration            function createLead() {}
//   2 class method / ctor / get / set class X { save() {} }
//   3 object shorthand method         { save() {} }
//   4 function assigned to a variable const createLead = async () => {}
//   5 function assigned to a property exports.createLead = function () {}
//   6 class field arrow               handleClick = () => {}
//   7 object property arrow           { createLead: () => {} }
//   8 wrapped function                const onSave = useCallback(() => {}, []), memo(...), forwardRef(...)
// Unnamed = not expected, reported separately: callbacks passed as arguments, IIFEs,
//   anonymous default exports.

import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

// Calls whose first argument is the named function itself (pattern 8). useMemo is NOT here: its
// argument computes a value, so the name belongs to the value, not to a function.
const WRAPPERS = new Set(["useCallback", "memo", "forwardRef"]);

const isFunctionValue = (n) => n && (ts.isArrowFunction(n) || ts.isFunctionExpression(n));
const unwrap = (n) => {
  while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n))) n = n.expression;
  return n;
};
const calleeName = (call) => {
  const e = call.expression;
  if (ts.isIdentifier(e)) return e.text;
  if (ts.isPropertyAccessExpression(e)) return e.name.text; // React.memo -> memo
  return null;
};
const nameText = (n) => (n && (ts.isIdentifier(n) || ts.isPrivateIdentifier(n) || ts.isStringLiteral(n)) ? n.text : null);

function listFunctions(sf) {
  const lineOf = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
  const named = [];
  const claimed = new Set(); // function nodes that got a name from their parent

  const add = (name, fnNode, pattern, startNode = fnNode) => {
    named.push({ name, line: lineOf(startNode.getStart(sf)), end: lineOf(fnNode.getEnd()), pattern });
    claimed.add(fnNode);
  };

  const visit = (node) => {
    if (ts.isFunctionDeclaration(node) && node.name) add(node.name.text, node, 1);
    else if (ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
      const name = nameText(node.name);
      if (name) add(name, node, ts.isObjectLiteralExpression(node.parent) ? 3 : 2);
    } else if (ts.isConstructorDeclaration(node)) add("constructor", node, 2);
    else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      const init = unwrap(node.initializer);
      const stmt = node.parent?.parent ?? node; // the whole `const ... = ...;` statement
      if (isFunctionValue(init)) add(node.name.text, init, 4, stmt);
      else if (init && ts.isCallExpression(init) && WRAPPERS.has(calleeName(init)) && isFunctionValue(unwrap(init.arguments[0])))
        add(node.name.text, unwrap(init.arguments[0]), 8, stmt);
    } else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
               && ts.isPropertyAccessExpression(node.left) && isFunctionValue(unwrap(node.right)))
      add(node.left.name.text, unwrap(node.right), 5, node);
    else if (ts.isPropertyDeclaration(node) && isFunctionValue(unwrap(node.initializer))) {
      const name = nameText(node.name);
      if (name) add(name, unwrap(node.initializer), 6, node);
    } else if (ts.isPropertyAssignment(node) && isFunctionValue(unwrap(node.initializer))) {
      const name = nameText(node.name);
      if (name) add(name, unwrap(node.initializer), 7, node);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);

  const unnamed = [];
  const collect = (node) => {
    const isFn = ts.isArrowFunction(node) || ts.isFunctionExpression(node)
      || (ts.isFunctionDeclaration(node) && !node.name);
    if (isFn && !claimed.has(node))
      unnamed.push({ line: lineOf(node.getStart(sf)), end: lineOf(node.getEnd()) });
    ts.forEachChild(node, collect);
  };
  collect(sf);
  return { named, unnamed };
}

const [root, ...files] = process.argv.slice(2);
const out = {};
for (const rel of files) {
  try {
    const text = fs.readFileSync(path.join(root, rel), "utf8");
    const kind = rel.endsWith(".jsx") || rel.endsWith(".tsx") ? ts.ScriptKind.JSX : ts.ScriptKind.JS;
    const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, kind);
    // The TS parser recovers from syntax errors instead of failing, so report them: a file with
    // errors may have functions it could not see.
    out[rel] = { ...listFunctions(sf), syntax_errors: (sf.parseDiagnostics || []).length };
  } catch (e) {
    out[rel] = { error: String(e && e.message || e) };
  }
}
process.stdout.write(JSON.stringify(out));
