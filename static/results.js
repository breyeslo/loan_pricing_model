// The one chart on the results page: required-rate components in basis points.
// A separate file because the Content-Security-Policy forbids inline scripts.
const chartData = JSON.parse(document.getElementById("chart-data").textContent);
const css = getComputedStyle(document.documentElement);
const series = css.getPropertyValue("--series-1").trim();
// Neutral grays that read on light, dark and printed backgrounds alike
const ink = "#7d7c77", grid = "rgba(128, 128, 128, 0.25)", total = "#6b6a66";
const last = chartData.values.length - 1;
const chart = new Chart(document.getElementById("components-chart"), {
  type: "bar",
  data: {
    labels: chartData.labels,
    datasets: [{
      data: chartData.values,
      backgroundColor: chartData.values.map((_, i) => i === last ? total : series),
      borderRadius: 4,
      borderSkipped: "start",
      maxBarThickness: 36,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => `${ctx.parsed.y.toFixed(0)} bp` } },
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: ink, autoSkip: false, maxRotation: 45 } },
      y: { grid: { color: grid }, border: { display: false },
           ticks: { color: ink }, title: { display: true, text: "basis points", color: ink } },
    },
  },
});
// Redraw at the printed size so Print to PDF stays on one page
window.addEventListener("beforeprint", () => chart.resize());
window.addEventListener("afterprint", () => chart.resize());
