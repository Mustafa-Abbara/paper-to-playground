function compute(state) {
  var a = state.a, b = state.b;
  var xs = state.x.slice();
  var raw = xs.map(function (x) { return a * x + b; });
  var ys = state.clip ? raw.map(function (y) { return Math.max(0, y); }) : raw;
  var n = ys.length;
  var mean = ys.reduce(function (s, y) { return s + y; }, 0) / n;
  var wsum = state.w.reduce(function (s, w) { return s + w; }, 0);
  var wmean = wsum > 0 ? ys.reduce(function (s, y, i) { return s + y * state.w[i]; }, 0) / wsum : NaN;
  var lim = state.range === "wide" ? 10 : 3;
  var lx = [], ly = [];
  for (var i = 0; i <= 40; i++) {
    var x = -lim + (2 * lim * i) / 40;
    var y = a * x + b;
    lx.push(x); ly.push(state.clip ? Math.max(0, y) : y);
  }
  var M = state.M;
  var v = [xs[0] || 0, xs[1] || 0];
  var Mx = M.map(function (row) { return [row[0] * v[0] + (row[1] || 0) * v[1]]; });
  var meanX = xs.reduce(function (s, x) { return s + x; }, 0) / n;
  var labels = xs.map(function (_, i) { return "s" + (i + 1); });
  return {
    outputs: {
      ys: ys, xs: xs, labels: labels, mean_y: mean, wmean_y: wmean,
      max_y: Math.max.apply(null, ys), line_x: lx, line_y: ly,
      x_first: xs[0], y_first: ys[0], M: M, Mx: Mx,
      arrow_w: 1 + Math.min(8, Math.abs(a) * 2)
    },
    intermediates: [
      {label: "a·x for each sample", value: xs.map(function (x) { return a * x; }), note: "scale step"},
      {label: "a·x + b", value: raw, note: "shift step"},
      {label: "Sum of weights", value: wsum, note: wsum > 0 ? "" : "weights are all zero"},
      {label: "M·[x₁, x₂]", value: Mx, decimals: 3}
    ],
    checks: [
      {label: "Mean of y equals a·(mean of x) + b", pass: state.clip || Math.abs(mean - (a * meanX + b)) < 1e-9,
       detail: state.clip ? "not expected to hold while clipping" : "linearity of the mean"},
      {label: "Weights sum to 1", pass: Math.abs(wsum - 1) < 1e-6, detail: "sum = " + wsum.toFixed(4)}
    ]
  };
}
