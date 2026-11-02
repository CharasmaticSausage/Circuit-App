# PCB Studio

A locally hosted, open-source PCB design assistant in the spirit of Flux AI, built on KiCad.
It has a chat pane (left or right, a setting), where you talk to a model of your choice about the board, and a live
preview with tabs for **Components**, **Placement**, **Routing**, the **PCB** (with layer and object filters), the **BOM** and **DRC**.
It opens on a **home screen** of project tiles.

Everything runs on your machine. The only network traffic is the model call, and with a local model there is none.

## Models: pick a provider in the header

| Provider | Cost | Setup |
|---|---|---|
| **Claude Code (your Claude plan)**, the default when `claude` is installed | Counts against your Claude Pro or Max **usage limits**; no API credits | Install Claude Code and run `claude` once to sign in. The model list uses aliases (Plan default, Opus, Sonnet, Haiku). |
| **Anthropic API** | Pay per token | An API key in Settings, or `ANTHROPIC_API_KEY`. |
| **OpenAI-compatible** | Free with local models | Base URL in Settings. Defaults to Ollama (`http://localhost:11434/v1`); LM Studio (`http://localhost:1234/v1`), llama.cpp server, vLLM, OpenRouter and OpenAI also work. Pick a model that supports tool calls. |

How the Claude Code provider works: each turn runs `claude -p` headless with stream-json output. All of Claude Code's own tools
(Bash, file edits, web) are switched off, and your user settings, hooks and plugins are not loaded. The only tools it gets are
PCB Studio's, served by `server/mcp_bridge.py`, an MCP stdio server written with the standard library. The bridge forwards each call
back to this server over localhost with a one-time token that is valid only for that turn. The CLI keeps the conversation, and the
chat resumes it with `--resume`. `ANTHROPIC_API_KEY` is removed from the CLI's environment so it can't switch to API billing.
The header shows your 5-hour and 7-day plan usage as the turn runs.

> This path is meant for personal use of **your own** signed-in Claude Code install. A public build of PCB Studio must not offer
> claude.ai sign-in to other people on their behalf. Check Anthropic's current terms before distributing anything that uses it.
> Models that need extra usage credits on your plan (Fable on Pro) are not listed.

## Run

```
run.bat            # first run creates the venv, then opens http://127.0.0.1:8765/
run.bat --no-browser --port 9000
```

Requirements:
- **KiCad 10** installed. PCB Studio uses KiCad's bundled Python for `pcbnew` and `kicad-cli` for DRC.
- One model provider (see above). Claude Code on your plan needs no key at all.

`run.bat` creates `%LOCALAPPDATA%\pcbstudio\venv` with `--system-site-packages`, which makes `pcbnew` importable.
It then installs `requirements.txt`, which lists three packages: `anthropic`, `fastapi` and `uvicorn`.

## Design flow: 1 Components, 2 Placement, 3 Routing

A stepper above the tabs shows each phase as locked, open, approved or *needs re-run*. **Only you approve a phase**
(a button; the model has no tool for it). Reopening a phase marks every later phase as needing a re-run.

| Phase | What happens | Where |
|---|---|---|
| **1 Components** | Requirements text. The assistant turns it into part roles and searches JLCPCB data; code ranks the candidates (enough stock for N boards, then package, then order cost including the Extended-part loading fee) and you choose. **New designs** also need a netlist: import a `.kicad_sch` (via `kicad-cli sch export netlist`) or a `.net`, or let the assistant draft one with `find_footprint`, `footprint_pads` and `symbol_pins` (pinouts from KiCad's symbol library). Every pad is checked against its footprint; you check pin → net per part in the Netlist table. | `server/phases.py`, `jlc.py`, `selection.py`, `netlist.py` |
| **2 Placement** | Board outline as a rectangle (with corner radius), circle or oval, in mm. A fab profile (`jlc_2layer`, `jlc_2layer_fine`, values from JLCPCB's capabilities page) is written to the project's `.kicad_pro`. **Build** loads every footprint from the netlist and assigns nets; **Run placer** does seeded simulated annealing (numpy) on wirelength, courtyard overlap, outline and connector-to-edge terms. The result is judged by **KiCad DRC** (courtyard overlaps, edge clearance), shown under the placer's before/after table. | `server/fab.py`, `layout.py` |
| **3 Routing** | A background job of N rounds: copy the board → export Specctra DSN → **fastroute** (`--max-time`) → import the SES → KiCad DRC → score. Scores are compared in order: DRC errors, unconnected, vias, length. Errors between pads of one footprint are listed, not scored. Between rounds, parts named in DRC problems get more spacing and unrouted nets more weight, and the placer refines. Stops early when a round is clean. Each round is a candidate you can **Preview** in the PCB tab and **Adopt** (snapshotted, so Undo works). | `server/routing.py`, `ops.py` |

**fastroute** is a separate program under the GPL-3.0. PCB Studio never bundles or links it: set the path to
`fastroute.exe` in Settings → Router and PCB Studio runs it as a subprocess. Only one routing job runs at a time,
guarded by a lock file (`fastroute.lck` in the data folder, plus `%LOCALAPPDATA%\tally_fastroute.lck` when present),
and **Stop** sends Ctrl+Break so fastroute saves the best result so far.

**Changelist.** When a tool fails or misleads it, the assistant logs the problem with evidence to `docs/CHANGELIST.md`
(tool `log_improvement`; Settings links to it).

## Home screen

The app opens on a grid of tiles, one per project, newest edit first. Each tile shows the board (KiCad's own plot of
Edge.Cuts and copper, `kicad-cli pcb export svg`, cached as `.thumb.svg` and redrawn when the board changes), the name and
a three-segment bar for the phases with the current one written out. Hover (or Tab to) a tile: the others dim and it folds
out the details: last edited, parts cost, scope (the requirements text), parts chosen, netlist, outline, best routing
result, chats and the creation date. Click or Enter opens it; **Projects** in the header comes back, Esc returns.

**Parts cost** is the chosen JLC parts for the order quantity (unit price at that quantity, plus the Extended-part
loading fee), from cached snapshot prices. Parts not in the cache are fetched once per project (then cached for a day)
and shown as "not priced" until then. The line always says what the total covers ("covers 7 of 10 roles, 3 not chosen,
1 not priced yet"), so a partial total is never read as the whole board. On an imported board the roles are not its full
BOM; the tile says so and points to "Check board BOM". It does not include the PCB, assembly or shipping. Code: `server/home.py`.

## How it works

| Part | File | What it does |
|---|---|---|
| Board model | `server/board.py` | Loads the board with `pcbnew` and extracts compact geometry for the viewer. Answers queries (parts, nets, BOM) and makes edits. Each edit takes a snapshot first, so you can undo it. |
| Projects | `server/workspace.py` | Opening a board **copies** it (with its `.kicad_pro`, `.kicad_dru` and `.kicad_prl`) into `%LOCALAPPDATA%\pcbstudio\projects\<id>\`. The original file is never written. |
| DRC | `server/drc.py` | Runs `kicad-cli pcb drc` with the board's own rules, the same engine as the KiCad GUI. |
| Tools | `server/tools.py` | The 32 tools the model can call. Board: `board_summary`, `find_components`, `get_component`, `components_near`, `measure`, `list_nets`, `get_net`, `get_bom`, `run_drc`, `move_component`, `set_component_field`, `undo_last_edit`, `show_in_viewer`. Phase 1: `phase_status`, `jlc_search`, `jlc_part`, `check_bom_availability`, `set_part_role`, `set_board_requirements`, `choose_part`, `find_footprint`, `footprint_pads`, `symbol_pins`, `get_netlist`, `draft_netlist`. Phase 2: `placement_status`, `set_board_outline`, `build_board`, `run_placer`. Phase 3: `start_routing`, `routing_status`. Other: `log_improvement`. |
| Providers | `server/providers/` | `claude_code_provider.py` (the `claude` CLI on your plan, plus `server/mcp_bridge.py`); `anthropic_provider.py` (a streaming tool loop with adaptive thinking, prompt caching and server-side refusal fallbacks); `openai_provider.py` (`/v1/chat/completions` with streamed tool calls, standard library only). `common.py` holds the shared system prompt and tool execution. |
| Server | `server/app.py` | FastAPI bound to `127.0.0.1`. It rejects other Host headers, and cross-site Origins on POST. The chat streams over SSE. All `pcbnew` calls run on one thread. |
| Home | `server/home.py` | Tile summaries from project files and the JLC cache (no network), the parts-cost rule, and the cached board thumbnail. |
| UI | `web/` | Plain HTML, CSS and ES modules, with no build step. `viewer.js` is a Canvas 2D board renderer with cached `Path2D` per layer. |

Viewer controls:
- Wheel to zoom, drag to pan, double-click to fit.
- Click to select, shift-click to add to the selection.
- Drag an already-selected part to move it (0.05 mm grid).
- Click a copper layer's name to make it the active layer.

Whatever you have selected in the viewer (parts or a net) is sent to the assistant as context.

Toolbar extras:
- **Download** saves the working copy, with every accepted edit, as a `.kicad_pcb` file.
- **KiCad** opens the working copy in KiCad's PCB editor. When you save there, the preview reloads.
- `?project=<id>&chat=<id>&tab=<parts|place|route|pcb|bom|drc>` in the URL opens a given project, conversation and tab.

## Tests
```
"%LOCALAPPDATA%\pcbstudio\venv\Scripts\python.exe" tests\test_core.py      # offline: 15 checks, prints ALL 15 CHECKS PASSED
"%LOCALAPPDATA%\pcbstudio\venv\Scripts\python.exe" tests\e2e_flow.py       # against a running server with fastroute set:
                                                                             # new design -> import tests/fixtures/tiny_ldo_mcu.net -> outline
                                                                             # -> build -> place -> route 3 rounds -> adopt; prints RESULT
```
The fixture is a 12-part USB-C / AP2112K / ATtiny85 board, written for these tests.

## Data locations
- `%LOCALAPPDATA%\pcbstudio\settings.json`: the provider, models, effort, API keys, base URL and tool paths. Keys stay local and are never sent to the browser.
- `%LOCALAPPDATA%\pcbstudio\projects\<id>\`: the board copy, `.snapshots\` (one per edit) and `chats\` (the transcripts).
- Set `PCBSTUDIO_DATA` to use a different folder.

## Limits (current MVP)
- **Edits:** the assistant can move parts, flip their side, rotate them and set fields, and run the placer and router. It can't draw individual tracks, change zones or edit the schematic.
- **Placer:** top side only, 90° steps; locked, flipped and odd-angle parts stay where they are. After placing, it moves each reference designator clear of pads, silkscreen and the edge (text is never shrunk; a reference with no clear spot is reported).
- **JLC data:** a daily mirror (jlcsearch). There is no reliable MPN search, so parts with unknown stock (e.g. ATtiny85-20SU, C89852) only appear by LCSC number.
- **Moves don't drag tracks.** Attached tracks stay where they were; run DRC to see what broke.
- **Zones:** they are shown as last filled. DRC can refill them in memory (the "Refill zones first" option); the copy on disk is not refilled.
- **Claude Code provider:** the CLI starts once per turn, which adds a couple of seconds. Thinking text is not streamed on this path.
  **Stop** only stops watching: the CLI turn keeps running (and using plan quota) until it finishes.
- **Local models:** small models often call tools badly. Use one trained for tool use (for example Qwen 3 or Llama 3.1+ at 8B or larger).
