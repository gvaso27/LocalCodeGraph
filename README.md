# LocalCodeGraph

A completely offline, local structural code graph for Java, Kotlin, and SQL
repositories — built independently, in the spirit of tools like Graphify's
`--code-only` mode, but with no dependency on Graphify or any external
service.

## Why this exists

Understanding a large repository by reading files one at a time is slow and
burns a lot of context. LocalCodeGraph parses your source with
[Tree-sitter](https://tree-sitter.github.io/tree-sitter/), extracts a
structural graph (classes, interfaces, functions, fields, imports,
inheritance, ...), and saves it locally so it can be queried instantly —
without re-parsing, and without reading source files at all once the graph
exists. The intended eventual use is letting an AI coding assistant (or a
human) find "what's relevant here?" from graph queries before deciding what,
if anything, to actually read.

## Privacy architecture

**This tool is 100% local. It has no network client anywhere in its own
code.**

- It never sends source code, file paths, or any repository metadata
  anywhere.
- It never calls any LLM (OpenAI, Anthropic, Gemini, or otherwise) and
  never requires an API key.
- It makes no HTTP/HTTPS requests, has no telemetry, and contacts no
  remote server.
- It works with the network fully disconnected — this is enforced, not
  just documented: `tests/test_security.py` runs the full
  build → save → load → query pipeline (both the library API and the
  actual `lcg` CLI) with all socket creation blocked at the Python level,
  and a separate test statically parses every module under `src/` to
  confirm none of them import a networking library.
- Only `lcg build <repository>` touches the filesystem beyond
  `.local-code-graph/`: it reads source files under the given repository
  root and writes the graph there. No other command reads source files —
  see "How building and querying are kept separate" below.
- Loading a saved graph is pure data deserialization: nothing derived from
  graph JSON is ever executed, evaluated, or dynamically imported (see
  `graph/storage.py` and `tests/test_security.py`).

### Dependency audit

Runtime dependencies (what's actually installed when you `pip install`/
`uv sync` this project, excluding dev-only test tooling):

| Package | Purpose | Networking? |
|---|---|---|
| `tree-sitter` | Python bindings for the Tree-sitter parsing library (a C library compiled locally) | No |
| `tree-sitter-java` | Compiled Java grammar for Tree-sitter | No |
| `tree-sitter-kotlin` | Compiled Kotlin grammar for Tree-sitter | No |
| `tree-sitter-sql` | Compiled SQL grammar for Tree-sitter | No |

That's the entire runtime dependency tree — `uv tree` shows nothing else.
No HTTP client, no cloud SDK, no telemetry/analytics library, no LLM
client, ever appears as a dependency of this project's own code.

**Dev-only dependency note, for full transparency:** the test suite uses
`pytest` (plus its own dependencies: `iniconfig`, `packaging`, `pluggy`,
`pygments`). `pytest` ships an optional, opt-in `--pastebin` plugin that
*can* upload failure output to `https://bpa.st` over HTTPS — but it is (a)
part of the test runner, never a dependency of the `local-code-graph`
package itself, (b) never imported by anything in `src/`, and (c) only
activates if you explicitly pass `pytest --pastebin=failed` or
`--pastebin=all`, which this project's test suite never does. It has no
bearing on `lcg` itself. (A `pygments` lexer file also matched a naive
grep for the word "socket" — that's a MySQL builtin-function name in a
syntax-highlighting keyword table, not networking code.)

### How building and querying are kept separate

```text
lcg build <repo>            repository -> scanner -> language parsers -> GraphBuilder -> save_graph()
lcg <any other command>      .local-code-graph/graph.json -> load_graph() -> QueryEngine -> result
```

Every query command loads only the saved `graph.json`; it never scans or
re-parses the repository. If no graph has been saved yet, the command fails
with a clear message telling you to run `lcg build` — it never builds one
implicitly. This is tested directly: `tests/test_cli.py` builds a graph,
deletes every source file (and separately, `chmod 000`s the source
directory), and confirms `overview`/`find`/`show`/`neighbors`/`path` all
still work from the saved graph alone.

## Installation

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone <this repository>
cd LocalCodeGraph
uv sync
```

This installs the `lcg` command inside the project's virtual environment.
Run it via `uv run lcg ...`, or activate the venv (`source .venv/bin/activate`)
and run `lcg ...` directly. `python -m local_code_graph ...` works
identically to `lcg ...` (same entry point).

### Installing it globally (recommended)

To use `lcg` from any repository on your machine, install it as a tool:

```bash
uv tool install --from /path/to/LocalCodeGraph local-code-graph
```

That puts `lcg` on your `PATH`. After changing LocalCodeGraph's own source,
re-run the same command with `--reinstall` to pick up the changes.

## Usage

```bash
# Build a graph for a repository (reads source, writes .local-code-graph/)
lcg build /path/to/repo

# Everything else only ever reads the saved graph:
lcg overview /path/to/repo
lcg find /path/to/repo SoundController
lcg show /path/to/repo com.example.sound.SoundController
lcg children /path/to/repo com.example.sound.SoundController
lcg neighbors /path/to/repo com.example.sound.SoundController --depth 2
lcg path /path/to/repo com.example.sound.SoundController com.example.sound.SoundService
lcg dependencies /path/to/repo com.example.sound.SoundController
lcg affected /path/to/repo com.example.sound.SoundService --depth 2
```

Every command's `--help` explains what it does, its arguments, and whether
it reads source or only the saved graph. Add `--json` to any query command
for machine-readable output (pure JSON on stdout, nothing else mixed in).

### Using it to cut an AI assistant's context usage

The point of the graph is to answer *structural* questions without pulling
whole files into a limited context window. Measured on a real ~350-file Java
backend:

| Question | Reading source | Using `lcg` |
|---|---|---|
| "Where is `AuthTokenService`, what implements it, what uses it?" | ~12,800 tokens (grep, then read 10 hits) | **~165 tokens** |
| "Summarize these 6 service classes" | ~6,000 tokens (read all 6) | **~2,800 tokens** |
| "What does this method actually do?" | reads the file | no help — read the file |

The gain is largest for *locating* things and *tracing relationships*, where
the answer is a handful of names but finding it would otherwise mean reading
many files. It's modest for bulk summarization, and zero when you genuinely
need implementation logic — the graph stores declarations and relationships,
never method bodies.

A practical workflow, and what to tell an assistant to do:

1. `lcg find` / `lcg show` / `lcg dependents` to identify the few relevant
   symbols, their files, and their line ranges.
2. Read *only* those files, ideally only those line ranges.
3. Skip step 2 entirely when the structural answer was all you needed.

To make this automatic, add a short note to the target repo's `CLAUDE.md` (or
your assistant's equivalent) telling it to prefer `lcg` over grepping/reading
for structural questions, and to fall back to reading source for
implementation details. Note this is per-machine tooling: someone cloning
your repo also needs `lcg` installed (or you can commit `.local-code-graph/`
so they can query without building).

### Full command list

| Command | Purpose |
|---|---|
| `build <repo>` | Scan, parse, and save the graph from scratch (full rebuild). Reads every source file. |
| `update <repo>` | Reparse only files changed since the last build/update (tracked via a saved file-hash manifest), merge into the existing graph, and save it. Requires an existing graph — never falls back to a full build implicitly. |
| `overview <repo>` | Repository-wide counts (files/nodes/edges/languages) and package list. |
| `find <repo> <query>` | Exact-match search (ID, then qualified name, then simple name). Never fuzzy; prints every match, including when ambiguous. |
| `show <repo> <query>` | Compact summary of one node: its members and its IMPORTS/EXTENDS/IMPLEMENTS relationships. No source code. |
| `children <repo> <query>` | This node's CONTAINS/DECLARES children. |
| `imports` / `imported-by <repo> <query>` | This node's outgoing IMPORTS edges / nodes that import it. |
| `extends` / `extended-by <repo> <query>` | This node's outgoing EXTENDS edge / nodes that extend it. |
| `implements` / `implemented-by <repo> <query>` | This node's outgoing IMPLEMENTS edges / nodes that implement it. |
| `dependencies` / `dependents <repo> <query>` | Direct IMPORTS/EXTENDS/IMPLEMENTS relationships, in either direction. Structural, not runtime. |
| `neighbors <repo> <query> [--depth N]` | Bounded local subgraph around a node, any edge type, either direction (default depth 1). |
| `affected <repo> <query> --depth N` | Nodes that transitively depend on this one, via reverse IMPORTS/EXTENDS/IMPLEMENTS. Graph reachability, **not** runtime/compiler impact analysis. |
| `path <repo> <source> <target> [--max-depth N]` | Shortest relationship chain connecting two nodes (undirected connectivity; default max depth 6). A graph path, **not** a runtime execution/call path. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | General/runtime error |
| 2 | Invalid command or arguments |
| 3 | No graph found for this repository |
| 4 | Query target not found |
| 5 | Ambiguous query (more than one exact match) |
| 6 | Invalid/corrupted graph file |

## Supported languages

**Java** — packages, imports (regular/wildcard), classes, interfaces,
enums, annotation types, fields, constructors, methods, parameters,
generics, `extends`/`implements`, nested types.

**Kotlin** — packages, imports (regular/aliased/wildcard), classes
(abstract/open/data/sealed/nested/inner/generic), interfaces, objects
(including companion objects), enum classes, annotation classes,
properties (`val`/`var`, including extension properties), functions
(including extension functions), primary/secondary constructors,
generics, `extends`/`implements` (inferred from `: Base()` vs. `: Type`
syntax).

**SQL** (`.sql`) — the schema a file declares: `CREATE TABLE` (with its
columns and declared types), `CREATE VIEW`, `CREATE INDEX`,
`CREATE TRIGGER`, and the references between them — foreign keys
(both `FOREIGN KEY ... REFERENCES` and inline `col INT REFERENCES t(id)`),
the tables a view selects from, and the table an index or trigger is
attached to. `ALTER TABLE ... ADD COLUMN` is followed, so in a versioned
migration folder a table's columns come out right even when the `CREATE`
lives in `001_init.sql` and the column was added in `014_add_body.sql`.

Two SQL-specific behaviours worth knowing:

- **Identifier case follows SQL's own rule.** Unquoted names are
  case-insensitive, so `REFERENCES Users(id)` resolves to
  `CREATE TABLE users`; quoted names (`"MyTable"`, `` `MyTable` ``,
  `[MyTable]`) keep their case. Node IDs use the canonical form; the
  original spelling is preserved for display.
- **Repeated declarations merge.** The same table declared or recreated
  across several migration files is one node, not a duplicate-ID error —
  the same treatment `PACKAGE` nodes get.

Queries and dependency analysis work across languages in one graph: a repo
with Java, Kotlin, and SQL produces a single `graph.json`.

Adding another language means implementing `parser/base.py`'s
`LanguageParser` interface and registering it in
`graph/builder.py::default_parsers()` — the graph model, storage, query
engine, and CLI are all language-neutral already.

## Graph format

Saved under `<repository>/.local-code-graph/` (gitignored by default —
generated graphs are never committed):

```text
<repository>/.local-code-graph/
├── graph.json         the graph itself: schema_version, root, nodes, edges
├── metadata.json      informational: generator version, counts, languages,
│                       a generation timestamp (never in graph.json — see below)
└── file_hashes.json   per-file content hashes, written by `build`/`update` so
                        the next `update` knows which files actually changed
```

`graph.json` is deterministic: building the same unchanged source tree
twice produces byte-for-byte identical output (nodes sorted by ID, edges
sorted by a canonical key, no timestamp). Node file paths are
repository-relative, not absolute, so a graph.json is portable — copying
it elsewhere doesn't leak the original machine's filesystem layout.

Node types: `FILE`, `PACKAGE`, `CLASS`, `INTERFACE`, `ENUM`, `ANNOTATION`,
`OBJECT`, `METHOD`, `CONSTRUCTOR`, `FIELD`, `PROPERTY`, `PARAMETER`,
`TABLE`, `COLUMN`, `VIEW`, `INDEX`, `TRIGGER`.
Edge types: `CONTAINS`, `IMPORTS`, `EXTENDS`, `IMPLEMENTS`, `DECLARES`,
`REFERENCES`.
Full field-level documentation, the deterministic-ID scheme, and exactly
what each field means for which node type live in
`src/local_code_graph/graph/model.py`'s module docstring.

Loading a graph validates it thoroughly (schema version, unique node IDs,
known node/edge types, edges pointing at real nodes, well-formed line
ranges, required fields) and raises a specific, catchable exception rather
than silently repairing bad data — see `graph/storage.py`.

## Query semantics

- **Accuracy over completeness.** `EXTENDS`/`IMPLEMENTS`/`IMPORTS` targets
  are only linked to another node when they resolve via exact matching
  (fully-qualified name, enclosing-type scope, the file's own explicit
  imports, or same-package lookup). External libraries, JDK/stdlib types,
  and ambiguous references are left unresolved (`target_id: null`) with
  the raw name preserved — never guessed at.
- **No CALLS edges, and no inferred references.** This graph captures
  declarations plus relationships that are *written down explicitly* in
  the source: `import`/`extends`/`implements` in Java and Kotlin, and SQL's
  `REFERENCES` (foreign keys, and the table a view/index/trigger names).
  It does not attempt method-call or reference resolution anywhere.
  `dependencies()`, `dependents()`, and `affected()` are therefore
  explicitly *structural* (built from IMPORTS/EXTENDS/IMPLEMENTS/
  REFERENCES), not runtime or compiler-level dependency/impact analysis,
  and `path()` is a graph relationship path, not an execution/call path.
  In particular, SQL embedded in Java/Kotlin string literals (Room
  `@Query`, JDBC statements) is *not* parsed, so there are no edges
  between a DAO class and the tables it queries.
- **Deterministic.** Every query result is sorted (nodes by ID, edges by a
  canonical key, neighborhoods by depth-then-ID) — the same graph queried
  twice, or an equivalent graph built in a different file/insertion order,
  always returns identical results.
- **No fuzzy matching.** `find`/`show`/etc. match by exact ID, then exact
  qualified name, then exact simple name. If more than one node matches,
  every match is returned — nothing is silently guessed.

## Known limitations

- **Deeply nested files used to be skipped; this is fixed, and the fix
  includes a dependency pin.** On one real ~400-file Android app, 54 files
  were reported as "parser crashed" and left out of the graph. Three
  separate causes produced that one symptom, and all three are addressed:

  1. Collecting syntax-error nodes recursed once per AST level. Kotlin
     produces roughly three AST levels per nested call, so a Compose screen
     blew Python's 1000-frame recursion limit while looking unremarkable.
     That traversal is now iterative (cursor-based).
  2. Any Python exception during a parse escaped and killed the parse
     worker, so the file was reported as a *native* crash — wrong and
     unactionable — and the rest of the batch paid a process respawn. Each
     file's parse now has its own crash barrier and reports the real error.
  3. **`tree-sitter` 0.26.0 genuinely segfaults while walking a deeply
     nested tree**, on any grammar (reproduced on both Java and Kotlin). It
     is not catchable from Python, so the dependency is pinned to
     `<0.26` — 0.25.2 parses the same files cleanly. Don't raise that pin
     without re-running `tests/test_deep_nesting.py`.

  The process isolation that made those files survivable is still there and
  still the backstop for anything similar: a file that does crash the
  parser is identified, reported with its actual cause, and skipped, and
  the rest of the repository still builds. **`lcg build` prints every
  skipped file and why** — their declarations are genuinely absent from the
  graph, so `lcg find` will report "No match found" for symbols defined in
  them, and grep/read is the fallback for those files. See
  `src/local_code_graph/parser/isolated.py` and
  `src/local_code_graph/parser/_ts_utils.py`.
- **Local/anonymous declarations aren't extracted.** Classes/functions/
  objects declared inside a method or function body, and anonymous
  classes/objects, are out of scope for Java and Kotlin.
- **No call-graph or type-inference.** No CALLS edges exist, and no
  reference is ever inferred; see "Query semantics" above.
- **SQL dialect coverage is not universal.** The grammar targets
  mainstream SQL, so some dialect-specific syntax parses as a syntax error.
  Two that matter for Android/SQLite work: `AUTOINCREMENT` (reported as a
  syntax error, but the surrounding table and its other columns are still
  extracted) and a `CREATE TRIGGER ... BEGIN ... END` body (whose error can
  swallow the statements that follow it in the same file, leaving them
  absent). Errors are always reported by `lcg build`, never silent.
- **SQL is read as declarations, not replayed as migrations.**
  `DROP TABLE` records a dependency but never removes nodes, and
  `ALTER TABLE ... DROP COLUMN`/`RENAME` are not applied. The graph
  describes what the files declare; computing a final schema by replaying
  migrations in order would be a different tool.
- **Kotlin `extends` vs. `implements` inference has one known gap:** a
  superclass referenced without constructor parentheses (only legal when
  every constructor is a secondary constructor delegating via `super(...)`)
  is currently classified as IMPLEMENTS rather than EXTENDS.
- **One known Tree-sitter grammar quirk:** in the current
  `tree-sitter-kotlin` grammar, a class-level annotation immediately
  preceding `annotation class Foo` (e.g. `@Target(...)\nannotation class
  Foo`) is misparsed as an expression rather than a declaration, with no
  syntax error reported — the annotation class is silently absent from the
  graph in that specific case. Reproduced and regression-tested in
  `tests/test_kotlin_parser.py`; not worked around, per this project's
  policy against grammar-quirk hacks.
- **`lcg update`'s incremental resolution is one-directional.** Only
  newly reparsed (changed/new) files get their IMPORTS/EXTENDS/IMPLEMENTS
  references resolved against the current graph. An old, still-unresolved
  reference in an *unchanged* file is not retroactively fixed just because
  this update happens to add what it was missing elsewhere — re-checking
  every old reference would mean re-parsing everything, defeating the
  point of an incremental update. Run `lcg build` for a full,
  fully-resolved rebuild. See `graph/incremental.py`.
- **Java**: C-style array declarators (`int x[];`) don't carry `[]` into
  the recorded type text; static/instance initializer blocks aren't
  extracted as nodes.
- **Kotlin**: `init { }` blocks aren't extracted as nodes.

## Development

```bash
uv sync                 # install runtime + dev dependencies
uv run pytest           # run the full test suite
uv run lcg --help
```

Synthetic test fixtures live under `tests/fixtures/` — no real-world
repositories are used in this project's own test suite.
