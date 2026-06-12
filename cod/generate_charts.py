import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
})

# ── Chart 1: Overall Precision / Recall / F1 ────────────────────────────────

metrics = ["Precizie", "Recunoaștere", "F1"]
detailed = [66.5, 70.6, 68.5]
simple   = [20.9, 88.8, 33.8]

x = np.arange(len(metrics))
w = 0.32

fig, ax = plt.subplots(figsize=(6, 4.2))

bars_d = ax.bar(x - w/2, detailed, w, label="Prompt detaliat", color="#2c6fad", zorder=3)
bars_s = ax.bar(x + w/2, simple,   w, label="Prompt simplu",   color="#e07b39", zorder=3)

ax.set_ylabel("Valoare (%)")
ax.set_ylim(0, 105)
ax.set_xticks(x)
ax.set_xticklabels(metrics)
ax.yaxis.grid(True, linestyle="--", alpha=0.5, zorder=0)
ax.legend(frameon=False)

for bar in bars_d:
    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
            f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=9)
for bar in bars_s:
    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
            f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=9)

fig.tight_layout()
fig.savefig("figuri/chart_overall_metrics.pdf", bbox_inches="tight")
fig.savefig("figuri/chart_overall_metrics.png", dpi=180, bbox_inches="tight")
print("Saved chart_overall_metrics")

# ── Chart 2: Precision–Recall scatter per project ───────────────────────────

projects = [
    "cal.com", "discourse-\ngraphite", "grafana",
    "keycloak", "keycloak-\ngreptile", "sentry", "sentry-\ngreptile",
]
labels_short = [
    "cal.com", "disc.-graph.", "grafana",
    "keycloak", "kc-greptile", "sentry", "sentry-grept.",
]

detailed_pts = [
    (64.3, 80.6), (71.1, 75.0), (72.7, 68.2),
    (73.3, 63.6), (100.0, 100.0), (81.2, 52.6), (37.0, 75.0),
]
simple_pts = [
    (18.2, 83.9), (24.4, 100.0), (16.2, 90.0),
    (25.8, 95.5), (33.3, 100.0), (27.8, 73.7), (15.1, 83.3),
]

colors = [
    "#1f77b4", "#ff7f0e", "#2ca02c",
    "#d62728", "#9467bd", "#8c564b", "#e377c2",
]

fig2, ax2 = plt.subplots(figsize=(6.8, 5))

for i, (dp, sp, col, lbl) in enumerate(zip(detailed_pts, simple_pts, colors, labels_short)):
    ax2.scatter(*dp, color=col, marker="o", s=70, zorder=4)
    ax2.scatter(*sp, color=col, marker="s", s=70, zorder=4)
    ax2.annotate(lbl, xy=dp, xytext=(4, 3), textcoords="offset points", fontsize=7.5, color=col)
    ax2.annotate(lbl, xy=sp, xytext=(4, 3), textcoords="offset points", fontsize=7.5, color=col)
    ax2.plot([dp[0], sp[0]], [dp[1], sp[1]], color=col, lw=0.8, linestyle="--", alpha=0.5)

legend_handles = [
    mpatches.Patch(color="none", label=""),
    plt.Line2D([0], [0], marker="o", color="gray", linestyle="none", markersize=7, label="Prompt detaliat"),
    plt.Line2D([0], [0], marker="s", color="gray", linestyle="none", markersize=7, label="Prompt simplu"),
]
ax2.legend(handles=legend_handles[1:], frameon=False, fontsize=9)

ax2.set_xlabel("Precizie (%)")
ax2.set_ylabel("Recunoaștere (%)")
ax2.set_xlim(5, 110)
ax2.set_ylim(45, 110)
ax2.xaxis.grid(True, linestyle="--", alpha=0.4, zorder=0)
ax2.yaxis.grid(True, linestyle="--", alpha=0.4, zorder=0)

fig2.tight_layout()
fig2.savefig("figuri/chart_precision_recall_scatter.pdf", bbox_inches="tight")
fig2.savefig("figuri/chart_precision_recall_scatter.png", dpi=180, bbox_inches="tight")
print("Saved chart_precision_recall_scatter")
