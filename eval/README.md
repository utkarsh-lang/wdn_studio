# WDN Studio benchmark

A question set with **engine-computed answers** for measuring how well the assistant answers questions about a water network, and how often it states numbers that no calculation produced.

## What is in it

- About 49 **dev** questions per network over 12 categories: summary, pipe stats, filters, topology, metrics, paths, simulation, time, spatial, resilience, definitions and unanswerable questions (pipe material, install year, cost and so on, which an `.inp` file does not contain).
- About 31 **held-out** questions per network: differently worded versions written before any rule testing. They are never used for tuning, so their score is the fair estimate.
- Default networks: Net3 (bundled with WNTR) and L-TOWN (`samples/L-TOWN.inp`, or set `LTOWN_INP`). Add your own with `--network NAME=path.inp`.

Element IDs, thresholds and expected values are chosen and computed from the model file, so nothing is typed by hand.

## Systems you can compare

| System | Meaning |
|---|---|
| `rules` | Built-in rule router. No LLM, no key. |
| `llm:groq` | Tool-calling assistant (the app's normal mode). Also `llm:openrouter`, `llm:gemini`, `llm:ollama`; add `:model` to choose a model, e.g. `llm:groq:openai/gpt-oss-20b`. |
| `plain:groq` | The same LLM **without tools**. It only sees the network summary. This is the hallucination baseline. |

## Run it

Rules only (no key needed, about 2 minutes):

```bat
python -m eval.run_eval --systems rules
```

LLM comparison on Windows (Command Prompt):

```bat
set GROQ_API_KEY=your_key_here
python -m eval.run_eval --systems rules,llm:groq,plain:groq --sleep 4
```

PowerShell: `$env:GROQ_API_KEY="your_key_here"`.

Useful options:

- `--split heldout` or `--split dev`: run one split only.
- `--categories simulation,time`: run some categories only.
- `--networks LT`: run one network.
- `--limit 20`: at most 20 questions per system (a quick trial).
- `--sleep 4`: pause between LLM questions to stay under free-tier per-minute limits.
- `--out eval/results_groq`: write to another folder.
- `--rerun`: start from scratch instead of resuming.

**Resuming:** every answer is appended to `results.jsonl` as soon as it finishes. If a free-tier daily limit stops the run, run the same command again the next day and it continues with the unanswered questions.

## Outputs (in `eval/results/` by default)

| File | Contents |
|---|---|
| `results.md` | Summary table, charts, method and every incorrect answer |
| `summary.csv` | One row per system and split: accuracy, right tool chosen, answers with unverified numbers, latency, tokens |
| `accuracy_by_category.csv` | Accuracy per category |
| `results.csv` | Every question with its answer and scoring details |
| `headline_metrics.png`, `accuracy_by_category.png` | Charts for a thesis or slides |
| `benchmark.json` | The questions and expected answers |

## Scoring

- **Correct:** the answer contains every expected number (counts exactly; measured values within 1% or within the rounding shown), every expected element ID or time, and the expected keywords. For unanswerable questions it must say the information is not in the model and must not invent numbers.
- **Right tool chosen:** at least one tool that can answer the question was called.
- **Answers with unverified numbers:** the answer contains a number that matches no tool result (for tool-using systems) or no true value (for the plain baseline).

## Honest reporting

The rules were improved using dev failures, so the dev score is optimistic. Report the held-out score. If you change the rules or prompts because of held-out failures, write a fresh held-out set before quoting a held-out number again.
