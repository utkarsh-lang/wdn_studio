"""Summary tables, charts and a Markdown report from eval results (results.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# categorical palette (fixed order) and text inks
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#dedcd6"


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def summarize(out: Path) -> pd.DataFrame | None:
    out = Path(out)
    path = out / "results.jsonl"
    if not path.exists():
        return None
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    df = pd.DataFrame(rows)
    # keep the latest attempt per (system, question)
    df = df.drop_duplicates(["system", "id"], keep="last")
    df["error"] = df["error"].where(df["error"].apply(lambda e: isinstance(e, str)), None)
    df["answered"] = df["error"].isna()
    df["hallucinated"] = (df["unverified_count"] > 0) | df["invented_numbers"]
    df["tokens"] = df["prompt_tokens"] + df["completion_tokens"]
    if "split" not in df.columns:
        df["split"] = "dev"
    df["system_split"] = df["system"] + " · " + df["split"]
    df.to_csv(out / "results.csv", index=False)

    g = df.groupby("system_split")
    summ = pd.DataFrame({
        "questions": g.size(),
        "answered": g["answered"].sum(),
        "accuracy_pct": (g["correct"].mean() * 100).round(1),
        "accuracy_answered_pct": g.apply(lambda x: x.loc[x["answered"], "correct"].mean() * 100).round(1),
        "tool_choice_pct": g.apply(lambda x: x["tool_ok"].dropna().astype(float).mean() * 100 if x["tool_ok"].notna().any() else float("nan")).round(1),
        "answers_with_unverified_numbers_pct": (g["hallucinated"].mean() * 100).round(1),
        "median_latency_s": g["latency_s"].median().round(2),
        "mean_tokens": g["tokens"].mean().round(0),
        "mean_llm_calls": g["llm_calls"].mean().round(2),
    }).reset_index().rename(columns={"system_split": "system"})
    summ.to_csv(out / "summary.csv", index=False)
    bycat = (df.pivot_table(index="category", columns="system_split", values="correct", aggfunc="mean") * 100).round(1)
    bycat.to_csv(out / "accuracy_by_category.csv")

    systems = list(summ["system"])
    colors = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(systems)}
    # Chart 1: accuracy by category (grouped bars)
    fig, ax = plt.subplots(figsize=(max(7, 0.9 * len(bycat) * max(1, len(systems)) * 0.6), 3.6))
    w = 0.8 / max(1, len(systems))
    for i, s in enumerate(systems):
        if s in bycat.columns:
            ax.bar([x + i * w for x in range(len(bycat))], bycat[s].fillna(0), width=w * 0.92, color=colors[s], label=s)
    ax.set_xticks([x + w * (len(systems) - 1) / 2 for x in range(len(bycat))])
    ax.set_xticklabels(bycat.index, rotation=30, ha="right")
    ax.set_ylabel("correct answers (%)", color=MUTED, fontsize=9)
    ax.set_ylim(0, 105)
    if len(systems) > 1:
        ax.legend(frameon=False, fontsize=8, ncol=min(3, len(systems)))
    _style(ax)
    fig.savefig(out / "accuracy_by_category.png", dpi=170, bbox_inches="tight")
    plt.close(fig)
    # Chart 2: headline metrics per system
    metrics = [("accuracy_pct", "Accuracy"), ("tool_choice_pct", "Right tool chosen"), ("answers_with_unverified_numbers_pct", "Answers with unverified numbers")]
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.0), sharey=True)
    for ax, (col, title) in zip(axes, metrics):
        vals = summ[col].fillna(0)
        ax.bar(range(len(systems)), vals, color=[colors[s] for s in systems], width=0.6)
        for x, v in enumerate(vals):
            ax.text(x, v + 1.5, f"{v:.0f}%", ha="center", fontsize=8, color=INK)
        ax.set_title(title, fontsize=10, color=INK)
        ax.set_xticks(range(len(systems)))
        ax.set_xticklabels([s.replace(":", "\n", 1) for s in systems], fontsize=7)
        ax.set_ylim(0, 110)
        _style(ax)
    axes[0].set_ylabel("%", color=MUTED)
    fig.savefig(out / "headline_metrics.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    md = ["# WDN Studio benchmark results", "",
          f"{df['id'].nunique()} questions over {df['network'].nunique()} network(s), {len(systems)} system(s).", "",
          "## Summary", "", summ.to_markdown(index=False), "", "![headline](headline_metrics.png)", "",
          "## Accuracy by category (%)", "", bycat.to_markdown(), "", "![by category](accuracy_by_category.png)", "",
          "## Method", "",
          "- Ground truth for every question is computed by the WDN Studio engine (WNTR/EPANET + NetworkX) from the model file; "
          "elements in the questions are selected programmatically, so no answer is typed by hand.",
          "- An answer is **correct** when it contains every expected number (counts exactly; measured values within 1%, or "
          "within the rounding the answer shows), every expected element ID or time, and the expected keywords; for "
          "'unanswerable' questions it must say the information is not in the model and invent no numbers.",
          "- **Right tool chosen**: at least one of the tools that can answer the question was called.",
          "- **Answers with unverified numbers**: the answer contains a number that matches no tool result (tool-using "
          "systems) or no true value (plain LLM baseline).",
          "- Each question runs in a fresh session; latency includes simulations and LLM calls.",
          "- **dev** questions were used while developing the built-in rules; **heldout** questions are paraphrases written "
          "before any rule testing and never tuned on. Report the held-out score as the fair estimate for the rules.", ""]
    wrong = df[~df["correct"]].sort_values(["system", "id"])
    if len(wrong):
        md += ["## Incorrect answers", "", "| system | id | category | question | numbers | ids | keywords | error |", "|---|---|---|---|---|---|---|---|"]
        for r in wrong.itertuples():
            md.append(f"| {r.system} | {r.id} | {r.category} | {r.question[:80]} | {r.numbers_ok} | {r.ids_ok} | {r.keywords_ok} | {(r.error if isinstance(r.error, str) else '')[:60]} |")
    (out / "results.md").write_text("\n".join(md), encoding="utf-8")
    print("\n" + summ.to_string(index=False))
    print(f"\nWrote {out}/summary.csv, accuracy_by_category.csv, results.csv, results.md and charts.")
    return summ


if __name__ == "__main__":
    import sys
    summarize(Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "results"))
