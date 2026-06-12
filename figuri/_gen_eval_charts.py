"""Generate evaluation charts for teza_v1.

Produces five separate figures (PNG + PDF) saved into figuri/:
  chart_eval_cost.{png,pdf}    cost vs quality
  chart_eval_speed.{png,pdf}   speed vs quality
  chart_eval_tools.{png,pdf}   tool-use quality vs final score
  chart_eval_params.{png,pdf}  parameter count vs score (Ollama)
  chart_eval_turns.{png,pdf}   turns distribution per model
"""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

RUNS_DIR = Path("/Users/enanescu/.vibe-cli/eval/results/runs")
AGG_DIR = RUNS_DIR / "aggregated"
OUT_DIR = Path("/Users/enanescu/stuff/lucrare/figuri")

lb = pd.read_csv(AGG_DIR / "leaderboard.csv")
pt = pd.read_csv(AGG_DIR / "per_task.csv")


def _tool_match_f1(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        s = v.get("score", v)
        if isinstance(s, (int, float)):
            return float(s)
        if isinstance(s, dict):
            if "f1" in s:
                return s.get("f1")
            return s.get("mean")
    return None


records = []
for f in glob.glob(str(RUNS_DIR / "*/eval_*.json")):
    data = json.load(open(f))
    for c in data.get("cases", []):
        m = c.get("metrics") or {}
        records.append({
            "provider": c.get("provider"),
            "model": c.get("model"),
            "task_id": c.get("id"),
            "tool_match": _tool_match_f1(m.get("tool_match")),
        })
tool_df = pd.DataFrame(records)
merged = pt.merge(tool_df, on=["provider", "model", "task_id"], how="left")

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.labelsize": 11,
    "legend.fontsize": 9,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linestyle": "--",
})

PROVIDER_COLORS = {
    "bedrock": "#ff9933",
    "gemini": "#4285F4",
    "ollama": "#2ca02c",
}


def color_for(p):
    return PROVIDER_COLORS.get(p, "#777777")


def short_label(model):
    return (model.replace("us.anthropic.", "")
                 .replace("-20251001-v1:0", "")
                 .replace(":", "-"))


def save(fig, name):
    png = OUT_DIR / f"{name}.png"
    pdf = OUT_DIR / f"{name}.pdf"
    fig.savefig(png, dpi=180, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    print(f"Saved {png}")
    print(f"Saved {pdf}")


def plot_cost():
    fig, ax = plt.subplots(figsize=(9, 6))
    lb_paid = lb[(lb["cost_usd"] > 0) & (lb["task_score_mean"] > 0)].copy()
    lb_free = lb[(lb["cost_usd"] == 0) & (lb["task_score_mean"] > 0)].copy()

    for prov, grp in lb_paid.groupby("provider"):
        ax.scatter(grp["cost_usd"], grp["task_score_mean"],
                   s=110 + grp["total_tokens"] / 4000.0,
                   c=color_for(prov), edgecolors="black", linewidths=0.6,
                   alpha=0.85, label=prov)
        for _, r in grp.iterrows():
            ax.annotate(short_label(r["model"]),
                        (r["cost_usd"], r["task_score_mean"]),
                        xytext=(6, 5), textcoords="offset points", fontsize=9)

    free_x = 1e-3
    ax.scatter([free_x] * len(lb_free), lb_free["task_score_mean"],
               s=90, c=[color_for("ollama")] * len(lb_free), marker="s",
               edgecolors="black", linewidths=0.5, alpha=0.7,
               label="ollama (gratuit, local)")
    free_sorted = lb_free.sort_values("task_score_mean", ascending=False).reset_index(drop=True)
    for i, r in free_sorted.iterrows():
        dy = 7 if (i % 2 == 0) else -12
        ax.annotate(short_label(r["model"]),
                    (free_x, r["task_score_mean"]),
                    xytext=(10, dy), textcoords="offset points", fontsize=8.5)

    ax.set_xscale("log")
    ax.set_xlabel("Cost total pe rulare (USD, scară log)")
    ax.set_ylabel("Scor mediu pe sarcină")
    ax.set_title("Cost vs. calitate")
    ax.set_ylim(-0.02, 1.0)
    ax.axvline(free_x, color="gray", linestyle=":", linewidth=0.8, alpha=0.6)
    ax.text(free_x * 1.05, 0.02, "modele gratuite\n(rulate local)",
            fontsize=8, color="gray", va="bottom")
    ax.legend(loc="lower right", framealpha=0.9)
    fig.tight_layout()
    save(fig, "chart_eval_cost")
    plt.close(fig)


def plot_speed():
    fig, ax = plt.subplots(figsize=(9, 6))
    lb_run = lb[lb["task_score_mean"] > 0].copy()
    lb_run["mean_dur_s"] = lb_run["duration_ms"] / lb_run["n_cases"] / 1000.0

    for prov, grp in lb_run.groupby("provider"):
        ax.scatter(grp["mean_dur_s"], grp["task_score_mean"],
                   s=120, c=color_for(prov), edgecolors="black", linewidths=0.6,
                   alpha=0.85, label=prov)
        for _, r in grp.iterrows():
            ax.annotate(short_label(r["model"]),
                        (r["mean_dur_s"], r["task_score_mean"]),
                        xytext=(6, 5), textcoords="offset points", fontsize=9)

    ax.set_xscale("log")
    ax.set_xlim(right=lb_run["mean_dur_s"].max() * 1.7)
    ax.set_xlabel("Durată medie pe sarcină (secunde, scară log)")
    ax.set_ylabel("Scor mediu pe sarcină")
    ax.set_title("Viteză vs. calitate")
    ax.set_ylim(-0.02, 1.0)
    ax.legend(loc="lower right", framealpha=0.9)
    fig.tight_layout()
    save(fig, "chart_eval_speed")
    plt.close(fig)


def plot_tools():
    fig, ax = plt.subplots(figsize=(9, 6))
    tu = merged.dropna(subset=["tool_match"]).copy()
    rng = np.random.default_rng(0)
    tu["tool_jit"] = tu["tool_match"] + rng.uniform(-0.012, 0.012, len(tu))
    tu["score_jit"] = tu["task_score"] + rng.uniform(-0.012, 0.012, len(tu))

    for prov, grp in tu.groupby("provider"):
        ax.scatter(grp["tool_jit"], grp["score_jit"],
                   s=55, c=color_for(prov), edgecolors="black", linewidths=0.4,
                   alpha=0.78, label=prov)

    if len(tu) >= 2:
        x = tu["tool_match"].to_numpy()
        y = tu["task_score"].to_numpy()
        m, b = np.polyfit(x, y, 1)
        r = np.corrcoef(x, y)[0, 1]
        xs = np.linspace(0, 1, 50)
        ax.plot(xs, m * xs + b, color="black", linestyle="--", linewidth=1.4,
                alpha=0.85, label=f"regresie liniară (r={r:.2f})")

    ax.set_xlabel("Tool match — F1 al apelurilor de unelte")
    ax.set_ylabel("Scor pe sarcină")
    ax.set_title("Calitatea utilizării uneltelor vs. scor final")
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.legend(loc="lower right", framealpha=0.9)
    fig.tight_layout()
    save(fig, "chart_eval_tools")
    plt.close(fig)


def parse_params(model):
    m = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", model.lower())
    if m:
        return float(m.group(1))
    heur = {
        "devstral-small-2": 24.0,
        "qwen3.5": 7.0,
        "gemma4": 9.0,
    }
    return heur.get(model)


def plot_params():
    fig, ax = plt.subplots(figsize=(9, 6))
    local = lb[lb["provider"] == "ollama"].copy()
    local["params_b"] = local["model"].map(parse_params)
    local_known = local.dropna(subset=["params_b"]).copy()
    local_known = local_known.sort_values("params_b")
    local_known["had_error"] = local_known["errors"] > 0

    for had_err, grp in local_known.groupby("had_error"):
        marker = "X" if had_err else "o"
        label = "eroare la rulare" if had_err else "rulare reușită"
        edge = "#d62728" if had_err else "black"
        ax.scatter(grp["params_b"], grp["task_score_mean"],
                   s=140, c=color_for("ollama"), marker=marker,
                   edgecolors=edge, linewidths=1.2, alpha=0.9, label=label)

    for _, r in local_known.iterrows():
        ax.annotate(short_label(r["model"]),
                    (r["params_b"], r["task_score_mean"]),
                    xytext=(7, 5), textcoords="offset points", fontsize=9)

    ok = local_known[~local_known["had_error"]].copy()
    if len(ok) >= 2:
        x = np.log10(ok["params_b"].to_numpy())
        y = ok["task_score_mean"].to_numpy()
        m, b = np.polyfit(x, y, 1)
        xs = np.linspace(ok["params_b"].min(), ok["params_b"].max(), 50)
        ax.plot(xs, m * np.log10(xs) + b, color="black", linestyle="--",
                linewidth=1.4, alpha=0.75, label="trend log-liniar")

    ax.set_xscale("log")
    ax.set_xlim(right=local_known["params_b"].max() * 1.7)
    ax.set_xlabel("Număr de parametri ai modelului local (miliarde, scară log)")
    ax.set_ylabel("Scor mediu pe sarcină")
    ax.set_title("Dimensiunea modelului local vs. scor (Ollama)")
    ax.set_ylim(-0.02, 1.0)
    ax.legend(loc="upper left", framealpha=0.9)
    fig.tight_layout()
    save(fig, "chart_eval_params")
    plt.close(fig)


def plot_turns():
    bins = [-0.5, 0.5, 5.5, 10.5, 20.5, np.inf]
    labels = ["0 (eșec)", "1-5", "6-10", "11-20", "21+"]
    bin_colors = ["#d62728", "#ff7f0e", "#2ca02c", "#1f77b4", "#9467bd"]

    order = lb.sort_values("task_score_mean", ascending=True)["model"].tolist()
    turns_by_model = {m: pt[pt["model"] == m]["turns"].to_numpy() for m in order}
    hist_matrix = np.zeros((len(order), len(labels)), dtype=float)
    for i, m in enumerate(order):
        counts, _ = np.histogram(turns_by_model[m], bins=bins)
        hist_matrix[i] = counts

    fig, ax = plt.subplots(figsize=(11, 7.5))
    y_pos = np.arange(len(order))
    left = np.zeros(len(order))
    for j, (lbl, color) in enumerate(zip(labels, bin_colors)):
        ax.barh(y_pos, hist_matrix[:, j], left=left, color=color,
                edgecolor="white", linewidth=0.6, label=lbl)
        left += hist_matrix[:, j]

    ax.set_yticks(y_pos)
    ax.set_yticklabels([short_label(m) for m in order], fontsize=10)
    ax.set_xlabel("Număr de sarcini (din 6)")
    ax.set_title("Distribuția numărului de pași per model")
    ax.set_xlim(0, 6.6)
    ax.grid(axis="y", visible=False)
    ax.legend(title="Pași per sarcină", loc="lower right", ncol=5,
              fontsize=9, bbox_to_anchor=(1.0, -0.16))

    mean_turns = [np.mean(turns_by_model[m]) for m in order]
    for i, mt in enumerate(mean_turns):
        ax.text(6.05, i, f"x̄={mt:.1f}", va="center", fontsize=9, color="#444")

    fig.tight_layout()
    save(fig, "chart_eval_turns")
    plt.close(fig)


def plot_score_per_dollar():
    paid = lb[(lb["cost_usd"] > 0) & (lb["task_score_mean"] > 0)].copy()
    paid["score_per_dollar"] = paid["task_score_mean"] / paid["cost_usd"]
    paid = paid.sort_values("score_per_dollar", ascending=True).reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = [color_for(p) for p in paid["provider"]]
    ax.barh(range(len(paid)), paid["score_per_dollar"],
            color=colors, edgecolor="black", linewidth=0.5, alpha=0.88)

    ax.set_yticks(range(len(paid)))
    ax.set_yticklabels([short_label(m) for m in paid["model"]], fontsize=10)
    ax.set_xscale("log")
    ax.set_xlabel("Scor per dolar (scară logaritmică)")
    ax.set_title("Eficiența cost-calitate a modelelor plătite")

    for i, row in paid.iterrows():
        spd = row["score_per_dollar"]
        ax.text(spd * 1.12, i, f"{spd:.1f}", va="center", fontsize=9)

    from matplotlib.patches import Patch
    seen = {}
    legend_elements = []
    for p in paid["provider"]:
        if p not in seen:
            seen[p] = True
            legend_elements.append(Patch(facecolor=color_for(p), label=p))
    ax.legend(handles=legend_elements, loc="lower right", framealpha=0.9)

    fig.tight_layout()
    save(fig, "chart_eval_score_per_dollar")
    plt.close(fig)


if __name__ == "__main__":
    plot_cost()
    plot_speed()
    plot_tools()
    plot_params()
    plot_turns()
    plot_score_per_dollar()
