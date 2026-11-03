# WDN Studio

An end-to-end web app for analysing water distribution networks. You load an EPANET `.inp` file, see the network as an interactive map, run hydraulic simulations and scenarios, run topology and centrality analysis, and ask questions in plain language.

**Every number is computed by EPANET (through WNTR) or NetworkX.** The language model only chooses which calculation to run and explains the result. It never generates figures itself.

## Features

| Area | What you get |
|---|---|
| Load | Upload a `.inp` file (drag and drop works) or pick a bundled sample. A model check runs on load and flags bad diameters or lengths, roughness outliers, isolated or unsupplied nodes, junctions above the source head, tank level errors, closed and parallel links, and case-colliding IDs. If the file has no coordinates, a layout is generated. |
| Map | Pan, zoom (including pinch), hover tooltips and click-to-inspect. Pipe width follows diameter. Reservoirs, tanks, pumps and valves have their own symbols. Links closed in the current scenario are drawn dashed. ID search, highlighted result sets, and light and dark themes. |
| Colour by | Network attributes: diameter, length, roughness, elevation, base demand (all demand categories summed). Simulation results: pressure, head, demand, flow, velocity, headloss, water age, shown at the current time step or as the min, max, mean or largest absolute value over the period. Any computed metric. Junctions below your minimum pressure get a red ring. |
| Simulation | Demand-driven (DD) or pressure-driven (PDD) runs with EPANET, extended period or steady state. Scenarios: demand multiplier, links closed (controls acting on those links are removed), water age. Results include KPIs (pressure extremes, negative pressures, velocities, headloss, flow reversals, tank behaviour, pump hours and kWh, Todini resilience, share of demand delivered under PDD), a time slider with playback, flow-direction arrows, per-element time-series charts, and a dropdown to compare runs. |
| Criteria | Your own service limits: minimum and maximum pressure, minimum and maximum velocity, maximum headloss. No standard is assumed. The presets are starting points to edit. Violations can be listed and highlighted. |
| Topology | Characterization (loops, meshedness, degree distribution, assortativity), dead ends, bridges, articulation points, nodes with no open path to a source, and graph diameter and average path (sampled above 3,000 nodes). |
| Metrics | Betweenness, closeness, eigenvector, degree, clustering, articulation (nodes); edge betweenness and bridge flag (links). Weighted versions treat length as a distance where that's correct, and use 1/length as a strength for eigenvector centrality and clustering. |
| Queries | Pipe filters with mm/m/inch/ft units and between-ranges, and shortest paths by length or by hops. |
| Assistant | Tool-calling LLM over the same tools the UI uses. The map reacts to its answers (highlight, colour, select, new simulation). Providers: Groq, OpenRouter, Gemini, or local Ollama, all on free tiers. If no LLM is set up, or the provider fails, a built-in rule router answers instead. |
| Export | Download the network as a `.inp` file, the map as PNG, network/simulation tables as Excel, any result as CSV. |
| Questions | Combined filters over any attribute or simulation result (`query_table`), values at a clock time, *when* peaks/minima happen, tank events, nodes within a distance, upstream/downstream flow tracing. The assistant sees what is on screen ("this node", "now"). |
| Resilience | Pipe criticality (N-1, every pipe closed in turn, pressure-driven), valve isolation segments (assumed valve layouts), fire flow at one or all junctions, demand-growth capacity, leak scenarios with pressure signatures, chlorine residual, water age and share of water from each source. Long analyses run in the background with a progress bar; results colour the map and download as CSV. |
| Reasoning | One-call what-if studies (baseline → scenario → comparison → severity), "why is pressure low here?" diagnosis along the supply path, a whole-network assessment with transparent thresholds, a built-in glossary behind ⓘ icons, live step-by-step progress in the chat, and a check that every number in an LLM answer appears in the tool results (unmatched numbers are marked ⚠). |
| Reporting | Charts panel (pressure/velocity histograms, demand, pressure band, tanks, pumps; compare two runs; PNG/CSV per chart), editing pipes/nodes/tanks/valves/pumps in the inspector with undo, save/open `.wdnproj` projects (network + edits + criteria + runs + chat), one-click Word/PDF reports (technical or thesis structure, maps, tables, assessment, optional number-checked LLM discussion) and HTML export of the chat with map snapshots. |
| Evaluation | `python -m eval.run_eval` scores the rule router, the tool-calling LLM and a no-tools LLM baseline on questions whose answers the engine computes: accuracy, right tool chosen, answers with unverified numbers, latency and tokens. |
| Free-tier friendly | Only the tools relevant to a question are sent to the LLM (about 55% fewer tokens); if a provider hits its rate limit, your other saved keys take over; repeated questions are answered from a cache. |

## Run locally

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
# open http://localhost:8000   (or http://localhost:8000/?sample=Net3)
```

On Windows, open a Command Prompt in this folder (type `cmd` in File Explorer's address bar) and run the same two commands. The install line is only needed once, or after updating the code.

Optional: drop more `.inp` files into `samples/` and they appear in the **Samples** menu.

## Deploy as a website (free, Render)

1. Put this folder in a GitHub repository.
2. On [render.com](https://render.com), sign in with GitHub and choose **New + → Web Service** and this repository. Render detects the `Dockerfile`; pick the **Free** instance type.
3. Wait for the build (5–10 minutes). The app is then served at `https://<name>.onrender.com`.

The free instance sleeps after about 15 minutes without visitors and wakes on the next visit (about a minute). It has 512 MB of memory, so very heavy analyses on large networks may need a local run. Sessions are kept in memory: networks are dropped after 3 hours of inactivity and are never written to disk. The Docker image also runs on any other container host (it listens on `$PORT`, default 7860).

## Free LLM setup

Users can paste their own key in the chat ⚙️ panel. The key goes to the provider through the server with each question and is never stored on the server. You can also set server-wide keys as environment variables (on Render: **Environment** tab). On a public site, a server-wide key is shared by every visitor and uses up your free quota.

| Secret | Provider | Default model (editable in the UI, and **List** shows current models) |
|---|---|---|
| `GROQ_API_KEY` | Groq free tier | `openai/gpt-oss-120b` |
| `OPENROUTER_API_KEY` | OpenRouter free models | `openai/gpt-oss-120b:free` |
| `GEMINI_API_KEY` | Google AI Studio free tier | `gemini-3.6-flash` |
| `LLM_PROVIDER` | force the default provider | `groq` / `openrouter` / `gemini` / `ollama` / `rules` |

Model IDs change often; the model box in the ⚙️ panel can be edited, and **List** shows the provider's current models. If a model is rejected, the app falls back to the built-in rule router and says why.

`ALLOW_CUSTOM_LLM_URL=1` lets users point the server at any OpenAI-compatible URL. It is off by default because, on a public deployment, it would let users make the server request arbitrary URLs.

## Architecture

```
app/main.py              FastAPI: REST API + static frontend
app/static/              Vanilla JS + Canvas 2D UI (no build step, no CDN)
wdn_engine/model.py      Loading, unit-consistent element tables, graphs (parallel-link aware)
wdn_engine/topology.py   Characterization, metrics, paths, pipe filters
wdn_engine/validation.py Model health checks
wdn_engine/hydraulics.py EPANET runs, scenarios, KPIs, frames/series
wdn_engine/criteria.py   User design criteria + presets
wdn_engine/tools.py      Tool registry shared by UI, LLM and rule router
wdn_engine/llm.py        OpenAI-compatible tool-calling loop + fallback
wdn_engine/router.py     Offline rule-based question router
tests/                   pytest: engine (Net1/Net3/L-TOWN) + API + mocked LLM loop + eval harness
eval/                    benchmark questions, scoring, runner, summary tables and charts
```

Units used throughout: lengths in m, diameters in mm, flows and demands in L/s, pressure in m, velocity in m/s, headloss in m/km.

## Future work

- District metered area (DMA) design and sensor placement
- Model calibration against measured data
- Leak localisation (for example with the BattLeDIM data for L-TOWN)

## Test

```bash
pip install pytest
LTOWN_INP=/path/to/L-TOWN.inp pytest -q
```
