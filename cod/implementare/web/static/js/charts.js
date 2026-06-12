// Chart.js global theming + helpers.
const COLOR = {
    accent: "#6ee7ff",
    accent2: "#b48bff",
    good: "#6efbb1",
    warn: "#ffc857",
    bad: "#ff7a90",
    grid: "rgba(255,255,255,0.06)",
    text: "rgba(230,236,255,0.7)",
};

const KIND_COLORS = {
    human: "#6ee7ff",
    assistant_text: "#b48bff",
    assistant_thinking: "#ffc857",
    tool_call: "#6efbb1",
    tool_result: "#7e9bff",
    system_prompt: "#cf7fff",
    compaction: "#ff7a90",
    model_switch: "#ff9b6e",
};

if (typeof Chart !== "undefined") {
    Chart.defaults.color = COLOR.text;
    Chart.defaults.borderColor = COLOR.grid;
    Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, Inter, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Chart.defaults.plugins.legend.labels.padding = 12;
} else {
    console.error("Chart.js failed to load — charts will be skipped.");
}

function gridScale(opts = {}) {
    return {
        grid: { color: COLOR.grid, drawBorder: false },
        ticks: { color: COLOR.text },
        ...opts,
    };
}

function destroyIfExists(id) {
    if (typeof Chart === "undefined") return;
    const c = Chart.getChart(id);
    if (c) c.destroy();
}

function fmtTokens(n) {
    if (n == null) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
}

function fmtMs(n) {
    if (!n) return "—";
    if (n >= 60000) return (n / 60000).toFixed(1) + "m";
    if (n >= 1000) return (n / 1000).toFixed(2) + "s";
    return Math.round(n) + "ms";
}

function fmtCost(n) {
    if (!n) return "$0";
    if (n < 0.01) return "$" + n.toFixed(5);
    if (n < 1) return "$" + n.toFixed(4);
    return "$" + n.toFixed(2);
}

window.__charts = { COLOR, KIND_COLORS, gridScale, destroyIfExists, fmtTokens, fmtMs, fmtCost };