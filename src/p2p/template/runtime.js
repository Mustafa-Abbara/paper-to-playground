/* Paper to Playground runtime: generic, topic-agnostic renderer.
 * Reads SPEC (JSON, already sanitized in Python) and calls compute(state), which the
 * page defines in a separate <script>. Owns all DOM work: controls, charts, readouts.
 * No network APIs, no external resources. ES2017. */
(function () {
  "use strict";

  var SPEC = {};
  try { SPEC = JSON.parse(document.getElementById("spec").textContent) || {}; }
  catch (e) { SPEC = {}; }
  var compute = typeof window.__P2P_COMPUTE === "function" ? window.__P2P_COMPUTE : null;
  var computeLoadError = window.__P2P_COMPUTE_ERROR || null;

  var CONTROLS = Array.isArray(SPEC.controls) ? SPEC.controls : [];
  // Invariants from the plan: boolean expressions over out (outputs) and s (state).
  var INVARIANTS = (Array.isArray(SPEC.invariants) ? SPEC.invariants : []).map(function (inv) {
    var fn = null;
    try { fn = new Function("out", "s", '"use strict"; return (' + String(inv.js) + ");"); } catch (e) { fn = null; }
    return { label: inv.name || String(inv.js), fn: fn };
  });
  var byId = {};
  CONTROLS.forEach(function (c) { if (c && c.id) byId[c.id] = c; });
  var state = {};
  var lastValues = {};
  var ctlNodes = {};
  var SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];
  var NS = "http://www.w3.org/2000/svg"; // XML namespace identifier, not a network request

  // ---------- small helpers ----------
  function $(id) { return document.getElementById(id); }
  function clone(v) { return v === undefined ? v : JSON.parse(JSON.stringify(v)); }
  function isNum(v) { return typeof v === "number"; }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "html") n.innerHTML = attrs[k] == null ? "" : String(attrs[k]);
      else if (k === "text") n.textContent = attrs[k] == null ? "" : String(attrs[k]);
      else if (k === "cls") n.className = attrs[k];
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (k) { if (k) n.appendChild(k); });
    return n;
  }
  function svg(tag, attrs, text) {
    var n = document.createElementNS(NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (attrs[k] !== undefined && attrs[k] !== null) n.setAttribute(k, attrs[k]);
    });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  var scratch = document.createElement("div");
  function plain(s) { scratch.innerHTML = s == null ? "" : String(s); return scratch.textContent; }
  function setHTML(id, s) { var n = $(id); if (n) n.innerHTML = s == null ? "" : String(s); }

  function fmt(v, d) {
    if (d === undefined || d === null) d = 3;
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (isNum(v)) {
      if (!isFinite(v)) return "undefined (see note)";
      var r = Number(v.toFixed(d));
      if (r === 0 && v !== 0 && Math.abs(v) < 1e-12) r = 0;
      if (Object.is(r, -0)) r = 0;
      var s = r.toFixed(d);
      if (d > 0) s = s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
      return s.replace("-", "−");
    }
    if (Array.isArray(v)) return "[" + v.map(function (x) { return fmt(x, d); }).join(", ") + "]";
    return String(v);
  }
  function path(obj, p) {
    return String(p).split(".").reduce(function (o, k) {
      return o == null ? undefined : o[/^\d+$/.test(k) ? Number(k) : k];
    }, obj);
  }
  var ctx = { outputs: {}, state: state };
  function resolve(ref) {
    if (typeof ref === "string" && /^(outputs|state)\.[\w.]+$/.test(ref)) return path(ctx, ref);
    return ref;
  }
  function num(ref, dflt) {
    var v = resolve(ref);
    if (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v))) v = Number(v);
    return isNum(v) && isFinite(v) ? v : dflt;
  }
  function template(s, d) {
    return String(s == null ? "" : s).replace(/\{((?:outputs|state)\.[\w.]+)(?::(\d))?\}/g,
      function (_, p, dd) { return fmt(path(ctx, p), dd === undefined ? d : Number(dd)); });
  }
  function reduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }
  function flash(node) {
    if (!node) return;
    node.classList.remove("changed"); void node.offsetWidth; node.classList.add("changed");
  }
  function say(msg) { var n = $("live"); if (n) n.textContent = msg; }

  // ---------- state ----------
  function fillOf(c) { return isNum(c.fill) ? c.fill : 0; }
  function resizeVector(a, n, fill) {
    a = Array.isArray(a) ? a.slice(0, n) : [];
    while (a.length < n) a.push(fill);
    return a;
  }
  function resizeMatrix(m, r, cc, fill) {
    m = Array.isArray(m) ? m.slice(0, r) : [];
    while (m.length < r) m.push([]);
    return m.map(function (row) { return resizeVector(row, cc, fill); });
  }
  function dims(c) {
    var v = state[c.id];
    var r = c.rows_from ? Math.round(num("state." + c.rows_from, 1)) : (Array.isArray(v) ? v.length : (c.rows || 1));
    var k = c.cols_from ? Math.round(num("state." + c.cols_from, 1))
      : (Array.isArray(v) && Array.isArray(v[0]) ? v[0].length : (c.cols || 1));
    return [Math.max(1, r), Math.max(1, k)];
  }
  function applyBindings() {
    CONTROLS.forEach(function (c) {
      if (c.type === "vector" && c.length_from) {
        var n = Math.max(1, Math.round(num("state." + c.length_from, 1)));
        state[c.id] = resizeVector(state[c.id], n, fillOf(c));
      } else if (c.type === "matrix" && (c.rows_from || c.cols_from)) {
        var d = dims(c);
        state[c.id] = resizeMatrix(state[c.id], d[0], d[1], fillOf(c));
      }
    });
  }
  function dependents(id) {
    return CONTROLS.filter(function (c) {
      return c.length_from === id || c.rows_from === id || c.cols_from === id;
    });
  }
  function resetState() {
    Object.keys(state).forEach(function (k) { delete state[k]; });
    CONTROLS.forEach(function (c) { state[c.id] = clone(c["default"]); });
    applyBindings();
  }
  function clamp(c, v) {
    if (isNum(c.min) && v < c.min) v = c.min;
    if (isNum(c.max) && v > c.max) v = c.max;
    return v;
  }
  function parseNum(s) {
    var v = Number(String(s).replace("−", "-").trim());
    return String(s).trim() === "" || !isFinite(v) ? null : v;
  }

  // ---------- controls ----------
  function helpNode(c) { return c.help ? el("p", { cls: "help", html: c.help }) : null; }
  function changed(c, rerenderSelf) {
    var deps = dependents(c.id);
    if (deps.length) {
      applyBindings();
      deps.forEach(function (d) { renderControl(d); });
    }
    if (rerenderSelf) renderControl(c);
    update();
  }

  function scalarControl(c) {
    var id = "ctl-" + c.id;
    var wrap = el("div", { cls: "ctl" });
    wrap.appendChild(el("label", { "for": id, html: c.label || c.id }));
    var row = el("div", { cls: "ctl-row" });
    var step = isNum(c.step) ? c.step : "any";
    var numIn = el("input", { type: "number", id: id, step: step, min: c.min, max: c.max,
                              value: state[c.id], "aria-describedby": c.help ? id + "-help" : null });
    if (c.type === "slider") {
      var range = el("input", { type: "range", min: isNum(c.min) ? c.min : 0, max: isNum(c.max) ? c.max : 1,
                                step: step, value: state[c.id], "aria-label": plain(c.label || c.id) });
      numIn.setAttribute("aria-label", plain(c.label || c.id) + " value");
      numIn.removeAttribute("id");
      range.id = id;
      range.addEventListener("input", function () {
        var v = parseNum(range.value); if (v === null) return;
        state[c.id] = v; numIn.value = v; numIn.removeAttribute("aria-invalid"); changed(c);
      });
      row.appendChild(range);
      numIn.addEventListener("input", function () {
        var v = parseNum(numIn.value);
        if (v === null) { numIn.setAttribute("aria-invalid", "true"); return; }
        numIn.removeAttribute("aria-invalid");
        v = clamp(c, v); state[c.id] = v; range.value = v; changed(c);
      });
      numIn.addEventListener("change", function () { numIn.value = state[c.id]; });
    } else {
      numIn.addEventListener("input", function () {
        var v = parseNum(numIn.value);
        if (v === null) { numIn.setAttribute("aria-invalid", "true"); return; }
        numIn.removeAttribute("aria-invalid"); state[c.id] = clamp(c, v); changed(c);
      });
      numIn.addEventListener("change", function () { numIn.value = state[c.id]; });
    }
    row.appendChild(numIn);
    if (c.unit) row.appendChild(el("span", { cls: "sum", html: c.unit }));
    wrap.appendChild(row);
    var h = helpNode(c); if (h) { h.id = id + "-help"; wrap.appendChild(h); }
    return wrap;
  }

  function toggleControl(c) {
    var id = "ctl-" + c.id;
    var input = el("input", { type: "checkbox", id: id });
    input.checked = !!state[c.id];
    input.addEventListener("change", function () { state[c.id] = input.checked; changed(c); });
    var lab = el("label", { cls: "switch", "for": id }, [input, el("span", { html: c.label || c.id })]);
    return el("div", { cls: "ctl" }, [lab, helpNode(c)]);
  }

  function selectControl(c) {
    var id = "ctl-" + c.id;
    var sel = el("select", { id: id });
    (c.options || []).forEach(function (o) {
      var val = o && typeof o === "object" ? o.value : o;
      var lab = o && typeof o === "object" ? (o.label || String(o.value)) : String(o);
      var opt = el("option", { value: String(val), text: o && typeof o === "object" ? plain(lab) : String(o) });
      if (String(val) === String(state[c.id])) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () {
      var o = (c.options || []).filter(function (x) {
        return String(x && typeof x === "object" ? x.value : x) === sel.value;
      })[0];
      state[c.id] = o && typeof o === "object" ? o.value : (o !== undefined ? o : sel.value);
      changed(c);
    });
    return el("div", { cls: "ctl" }, [el("label", { "for": id, html: c.label || c.id }), sel, helpNode(c)]);
  }

  function labelAt(labels, i, prefix) {
    if (Array.isArray(labels) && labels[i] !== undefined) return String(labels[i]);
    return (prefix || "") + (i + 1);
  }
  function cellInput(c, aria, get, set) {
    var inp = el("input", { cls: "grid-in", type: "text", inputmode: "decimal", value: fmt(get(), 6).replace("−", "-"),
                            "aria-label": aria });
    inp.addEventListener("input", function () {
      var v = parseNum(inp.value);
      if (v === null) { inp.setAttribute("aria-invalid", "true"); return; }
      inp.removeAttribute("aria-invalid"); set(clamp(c, v)); changed(c);
    });
    inp.addEventListener("change", function () { inp.value = fmt(get(), 6).replace("−", "-"); });
    return inp;
  }

  function vectorControl(c) {
    var v = state[c.id] = Array.isArray(state[c.id]) ? state[c.id] : [];
    var wrap = el("div", { cls: "ctl", role: "group", "aria-labelledby": "lab-" + c.id });
    wrap.appendChild(el("div", { cls: "ctl-label", id: "lab-" + c.id, html: c.label || c.id }));
    var t = el("table", { cls: "grid" });
    var hr = el("tr"), br = el("tr");
    v.forEach(function (_, i) {
      var name = labelAt(c.labels, i, c.label_prefix || "");
      hr.appendChild(el("th", { scope: "col", html: name }));
      br.appendChild(el("td", null, [cellInput(c, plain(c.label || c.id) + " " + plain(name),
        function () { return state[c.id][i]; }, function (x) { state[c.id][i] = x; })]));
    });
    t.appendChild(hr); t.appendChild(br);
    wrap.appendChild(el("div", { cls: "mwrap" }, [t]));
    var tools = el("div", { cls: "ctl-tools" });
    if (c.normalize || c.show_sum) {
      var s = v.reduce(function (a, b) { return a + (isNum(b) ? b : 0); }, 0);
      var off = c.normalize && Math.abs(s - 1) > 1e-6;
      tools.appendChild(el("span", { cls: "sum" + (off ? " off" : ""), id: "sum-" + c.id,
                                      text: "Sum = " + fmt(s, 4) + (off ? " (not 1)" : "") }));
    }
    if (c.normalize) {
      var nb = el("button", { type: "button", cls: "btn small", text: "Scale so the sum is 1" });
      nb.addEventListener("click", function () {
        var sum = state[c.id].reduce(function (a, b) { return a + (isNum(b) && b > 0 ? b : 0); }, 0);
        if (sum > 0) state[c.id] = state[c.id].map(function (x) { return isNum(x) && x > 0 ? x / sum : 0; });
        changed(c, true); say("Values scaled so they sum to 1.");
      });
      tools.appendChild(nb);
    }
    if (c.resizable && !c.length_from) {
      var lim = Array.isArray(c.resizable) ? c.resizable : [1, 8];
      var add = el("button", { type: "button", cls: "btn small", text: "Add entry" });
      var rem = el("button", { type: "button", cls: "btn small", text: "Remove entry" });
      if (v.length >= lim[1]) add.disabled = true;
      if (v.length <= lim[0]) rem.disabled = true;
      add.addEventListener("click", function () { state[c.id].push(fillOf(c)); changed(c, true); });
      rem.addEventListener("click", function () { state[c.id].pop(); changed(c, true); });
      tools.appendChild(add); tools.appendChild(rem);
    }
    if (tools.childNodes.length) wrap.appendChild(tools);
    var h = helpNode(c); if (h) wrap.appendChild(h);
    return wrap;
  }

  function matrixControl(c) {
    var d = dims(c);
    state[c.id] = resizeMatrix(state[c.id], d[0], d[1], fillOf(c));
    var m = state[c.id];
    var wrap = el("div", { cls: "ctl", role: "group", "aria-labelledby": "lab-" + c.id });
    wrap.appendChild(el("div", { cls: "ctl-label", id: "lab-" + c.id, html: c.label || c.id }));
    var t = el("table", { cls: "grid" });
    var head = el("tr", null, [el("th")]);
    for (var j = 0; j < d[1]; j++) head.appendChild(el("th", { scope: "col", html: labelAt(c.col_labels, j, "c") }));
    t.appendChild(head);
    m.forEach(function (row, i) {
      var rname = labelAt(c.row_labels, i, "r");
      var tr = el("tr", null, [el("th", { scope: "row", html: rname })]);
      row.forEach(function (_, jj) {
        tr.appendChild(el("td", null, [cellInput(c,
          plain(c.label || c.id) + " row " + plain(rname) + " column " + plain(labelAt(c.col_labels, jj, "c")),
          function () { return state[c.id][i][jj]; }, function (x) { state[c.id][i][jj] = x; })]));
      });
      t.appendChild(tr);
    });
    wrap.appendChild(el("div", { cls: "mwrap" }, [t]));
    var rz = c.resizable;
    if (rz && typeof rz === "object") {
      var tools = el("div", { cls: "ctl-tools" });
      [["rows", "row", 0], ["cols", "column", 1]].forEach(function (spec) {
        var lim = rz[spec[0]]; var bound = spec[2] === 0 ? c.rows_from : c.cols_from;
        if (!Array.isArray(lim) || bound) return;
        var cur = d[spec[2]];
        var a = el("button", { type: "button", cls: "btn small", text: "Add " + spec[1] });
        var r = el("button", { type: "button", cls: "btn small", text: "Remove " + spec[1] });
        if (cur >= lim[1]) a.disabled = true;
        if (cur <= lim[0]) r.disabled = true;
        a.addEventListener("click", function () {
          var nd = dims(c); nd[spec[2]] += 1;
          state[c.id] = resizeMatrix(state[c.id], nd[0], nd[1], fillOf(c)); changed(c, true);
        });
        r.addEventListener("click", function () {
          var nd = dims(c); nd[spec[2]] -= 1;
          state[c.id] = resizeMatrix(state[c.id], nd[0], nd[1], fillOf(c)); changed(c, true);
        });
        tools.appendChild(a); tools.appendChild(r);
      });
      if (tools.childNodes.length) wrap.appendChild(tools);
    }
    var h = helpNode(c); if (h) wrap.appendChild(h);
    return wrap;
  }

  function renderControl(c) {
    var node;
    if (c.type === "slider" || c.type === "number") node = scalarControl(c);
    else if (c.type === "toggle") node = toggleControl(c);
    else if (c.type === "select") node = selectControl(c);
    else if (c.type === "vector") node = vectorControl(c);
    else if (c.type === "matrix") node = matrixControl(c);
    else node = el("div", { cls: "ctl" }, [el("p", { cls: "help", text: "Unsupported control: " + c.type })]);
    node.setAttribute("data-control", c.id);
    var old = ctlNodes[c.id];
    if (old && old.parentNode) {
      var focusedLabel = document.activeElement && old.contains(document.activeElement)
        ? document.activeElement.getAttribute("aria-label") || document.activeElement.textContent : null;
      old.parentNode.replaceChild(node, old);
      if (focusedLabel) {
        var again = Array.prototype.filter.call(node.querySelectorAll("input,button,select"), function (n) {
          return (n.getAttribute("aria-label") || n.textContent) === focusedLabel;
        })[0];
        if (again && !again.disabled) again.focus();
      }
    } else {
      $("controls-list").appendChild(node);
    }
    ctlNodes[c.id] = node;
  }
  function renderAllControls() {
    $("controls-list").innerHTML = ""; ctlNodes = {};
    CONTROLS.forEach(renderControl);
  }

  // ---------- charts ----------
  function niceTicks(lo, hi, count) {
    if (!(isFinite(lo) && isFinite(hi))) return [0, 1];
    if (lo === hi) { var pad = Math.abs(lo) * 0.5 || 1; lo -= pad; hi += pad; }
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = (count * step) / span;
    if (err <= 0.15) step *= 10; else if (err <= 0.35) step *= 5; else if (err <= 0.75) step *= 2;
    var ticks = [], start = Math.ceil(lo / step - 1e-9) * step;
    for (var t = start; t <= hi + step * 1e-9; t += step) ticks.push(Math.abs(t) < step * 1e-9 ? 0 : t);
    return ticks;
  }
  function tickFmt(t) {
    var a = Math.abs(t);
    if (a !== 0 && (a < 1e-3 || a >= 1e5)) return t.toExponential(1).replace("-", "−");
    return fmt(t, a < 1 ? 3 : (a < 10 ? 2 : 1));
  }
  function frame(title, caption, body, note) {
    var fig = el("figure", { cls: "figure" });
    if (title) fig.appendChild(el("h3", { html: template(title) }));
    fig.appendChild(body && body.namespaceURI === NS ? el("div", { cls: "chart" }, [body]) : body);
    if (note) fig.appendChild(el("p", { cls: "note", text: note }));
    if (caption) fig.appendChild(el("figcaption", { html: template(caption) }));
    return fig;
  }
  function axes(g, x0, y0, w, h, yTicks, ys, xl, yl) {
    yTicks.forEach(function (t) {
      var y = ys(t);
      g.appendChild(svg("line", { "class": "gridline", x1: x0, x2: x0 + w, y1: y, y2: y }));
      g.appendChild(svg("text", { "class": "tick", x: x0 - 6, y: y + 4, "text-anchor": "end" }, tickFmt(t)));
    });
    var ax = svg("g", { "class": "axis" });
    ax.appendChild(svg("line", { x1: x0, x2: x0, y1: y0, y2: y0 + h }));
    g.appendChild(ax);
    if (yl) g.appendChild(svg("text", { "class": "axis-label", x: 14, y: y0 + h / 2,
      transform: "rotate(-90 14 " + (y0 + h / 2) + ")", "text-anchor": "middle" }, plain(template(yl))));
    if (xl) g.appendChild(svg("text", { "class": "axis-label", x: x0 + w / 2, y: y0 + h + 40,
      "text-anchor": "middle" }, plain(template(xl))));
  }
  function seriesList(v) {
    if (Array.isArray(v.series) && v.series.length) return v.series;
    return [{ values: v.values || v.source, label: v.label }];
  }

  function barChart(v) {
    var series = seriesList(v).map(function (s) {
      var vals = resolve(s.values || s.y || s.source);
      return { label: s.label, vals: Array.isArray(vals) ? vals : (isNum(vals) ? [vals] : []) };
    });
    var n = Math.max.apply(null, series.map(function (s) { return s.vals.length; }).concat([0]));
    if (!n) return frame(v.title, v.caption, el("p", { cls: "note", text: "No values to show." }));
    var labels = resolve(v.labels);
    var all = [];
    series.forEach(function (s) { s.vals.forEach(function (x) { if (isNum(x) && isFinite(x)) all.push(x); }); });
    var lo = isNum(v.y_min) ? v.y_min : Math.min(0, Math.min.apply(null, all.concat([0])));
    var hi = isNum(v.y_max) ? v.y_max : Math.max(0, Math.max.apply(null, all.concat([lo + 1e-9])));
    var ticks = niceTicks(lo, hi, 5); lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    var W = 600, H = v.x_label || series.length > 1 ? 280 : 256, x0 = 54, y0 = 16, w = W - x0 - 12, h = H - y0 - (v.x_label || series.length > 1 ? 58 : 34);
    var ys = function (t) { return y0 + h - ((t - lo) / (hi - lo || 1)) * h; };
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
    var g = svg("g"); root.appendChild(g);
    axes(g, x0, y0, w, h, ticks, ys, v.x_label, v.y_label);
    var band = w / n, gw = band * 0.72, bw = gw / series.length;
    var maxIdx = -1;
    if (v.highlight === "max" && series.length === 1) {
      var best = -Infinity;
      series[0].vals.forEach(function (x, i) { if (isNum(x) && x > best) { best = x; maxIdx = i; } });
    }
    var summary = [];
    for (var i = 0; i < n; i++) {
      var cx = x0 + band * i + (band - gw) / 2;
      series.forEach(function (s, si) {
        var val = s.vals[i];
        if (!(isNum(val) && isFinite(val))) return;
        var yv = ys(val), yz = ys(Math.max(lo, Math.min(hi, 0)));
        var col = series.length > 1 ? SERIES[si % SERIES.length] : (maxIdx === i ? "var(--s2)" : "var(--s1)");
        g.appendChild(svg("rect", { x: cx + bw * si + 1, y: Math.min(yv, yz), width: Math.max(1, bw - 2),
                                     height: Math.max(0.5, Math.abs(yz - yv)), fill: col, rx: 1.5 }));
        if (v.value_labels !== false && bw > 18) {
          var inside = val < 0 && Math.abs(yz - yv) > 18;
          g.appendChild(svg("text", { "class": "val", x: cx + bw * si + bw / 2, "text-anchor": "middle",
            y: val >= 0 ? yv - 5 : (inside ? yv - 5 : yv + 13), style: inside ? "fill:#fff" : null },
            fmt(val, isNum(v.decimals) ? v.decimals : 3)));
        }
        summary.push(plain(labelAt(labels, i, "")) + " " + fmt(val, 3));
      });
      g.appendChild(svg("text", { "class": "tick", x: x0 + band * i + band / 2, y: y0 + h + 16,
                                   "text-anchor": "middle" }, plain(labelAt(labels, i, ""))));
    }
    g.appendChild(svg("line", { x1: x0, x2: x0 + w, y1: ys(Math.max(lo, Math.min(hi, 0))),
                                 y2: ys(Math.max(lo, Math.min(hi, 0))), stroke: "var(--muted)" }));
    if (series.length > 1) legend(g, series, x0, H - 6);
    root.setAttribute("aria-label", plain(template(v.title || "Bar chart")) + ": " + summary.join(", "));
    return frame(v.title, v.caption, root);
  }

  function legend(g, series, x, y) {
    var cx = x;
    series.forEach(function (s, i) {
      g.appendChild(svg("rect", { x: cx, y: y - 9, width: 10, height: 10, fill: SERIES[i % SERIES.length] }));
      var label = plain(template(s.label || "series " + (i + 1)));
      g.appendChild(svg("text", { "class": "tick", x: cx + 14, y: y }, label));
      cx += 26 + label.length * 6.5;
    });
  }

  function lineChart(v) {
    var shared = resolve(v.x);
    var series = (Array.isArray(v.series) ? v.series : [{ y: v.y, x: v.x, label: v.label }]).map(function (s) {
      var ys = resolve(s.y || s.values), xs = s.x !== undefined ? resolve(s.x) : shared;
      ys = Array.isArray(ys) ? ys : [];
      if (!Array.isArray(xs)) xs = ys.map(function (_, i) { return i; });
      var pts = [];
      for (var i = 0; i < Math.min(xs.length, ys.length); i++)
        if (isNum(xs[i]) && isNum(ys[i]) && isFinite(xs[i]) && isFinite(ys[i])) pts.push([xs[i], ys[i]]);
      return { label: s.label, pts: pts, dashed: !!s.dashed };
    });
    var marks = (v.points || []).map(function (p) {
      return { x: num(p.x, NaN), y: num(p.y, NaN), label: p.label };
    }).filter(function (p) { return isFinite(p.x) && isFinite(p.y); });
    var X = [], Y = [];
    series.forEach(function (s) { s.pts.forEach(function (p) { X.push(p[0]); Y.push(p[1]); }); });
    marks.forEach(function (p) { X.push(p.x); Y.push(p.y); });
    if (!X.length) return frame(v.title, v.caption, el("p", { cls: "note", text: "No points to plot." }));
    var xlo = isNum(v.x_min) ? v.x_min : Math.min.apply(null, X), xhi = isNum(v.x_max) ? v.x_max : Math.max.apply(null, X);
    var ylo = isNum(v.y_min) ? v.y_min : Math.min.apply(null, Y), yhi = isNum(v.y_max) ? v.y_max : Math.max.apply(null, Y);
    var yt = niceTicks(ylo, yhi, 5); ylo = Math.min(ylo, yt[0]); yhi = Math.max(yhi, yt[yt.length - 1]);
    var xt = niceTicks(xlo, xhi, 6); if (xlo === xhi) { xlo = xt[0]; xhi = xt[xt.length - 1]; }
    var W = 600, H = 290, x0 = 54, y0 = 16, w = W - x0 - 14, h = H - y0 - 62;
    var sx = function (t) { return x0 + ((t - xlo) / (xhi - xlo || 1)) * w; };
    var sy = function (t) { return y0 + h - ((t - ylo) / (yhi - ylo || 1)) * h; };
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
    var g = svg("g"); root.appendChild(g);
    axes(g, x0, y0, w, h, yt, sy, v.x_label, v.y_label);
    g.appendChild(svg("line", { x1: x0, x2: x0 + w, y1: y0 + h, y2: y0 + h, stroke: "var(--muted)" }));
    xt.forEach(function (t) {
      if (t < xlo - 1e-9 || t > xhi + 1e-9) return;
      g.appendChild(svg("text", { "class": "tick", x: sx(t), y: y0 + h + 16, "text-anchor": "middle" }, tickFmt(t)));
    });
    if (ylo < 0 && yhi > 0) g.appendChild(svg("line", { x1: x0, x2: x0 + w, y1: sy(0), y2: sy(0), stroke: "var(--muted)", "stroke-dasharray": "2 3" }));
    series.forEach(function (s, i) {
      if (!s.pts.length) return;
      var d = s.pts.map(function (p, k) { return (k ? "L" : "M") + sx(p[0]).toFixed(2) + " " + sy(p[1]).toFixed(2); }).join(" ");
      g.appendChild(svg("path", { d: d, fill: "none", stroke: SERIES[i % SERIES.length], "stroke-width": 2.2,
                                   "stroke-dasharray": s.dashed ? "6 4" : null, "stroke-linejoin": "round" }));
      if (s.pts.length <= 24) s.pts.forEach(function (p) {
        g.appendChild(svg("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 2.6, fill: SERIES[i % SERIES.length] }));
      });
    });
    marks.forEach(function (p) {
      g.appendChild(svg("circle", { cx: sx(p.x), cy: sy(p.y), r: 6, fill: "var(--sheet)", stroke: "var(--s6)", "stroke-width": 2.5 }));
      if (p.label) g.appendChild(svg("text", { "class": "val", x: sx(p.x) + 9, y: sy(p.y) - 8 }, plain(template(p.label))));
    });
    if (series.length > 1) legend(g, series, x0, H - 6);
    root.setAttribute("aria-label", plain(template(v.title || "Line chart")) + ", " + series.length + " series");
    return frame(v.title, v.caption, root);
  }

  function mix(a, b, t) { return a.map(function (x, i) { return Math.round(x + (b[i] - x) * t); }); }
  function rgb(c) { return "rgb(" + c.join(",") + ")"; }
  function heatmap(v) {
    var data = resolve(v.values || v.source);
    if (!Array.isArray(data) || !data.length) return frame(v.title, v.caption, el("p", { cls: "note", text: "No values to show." }));
    data = data.map(function (r) { return Array.isArray(r) ? r : [r]; });
    var flat = [];
    data.forEach(function (r) { r.forEach(function (x) { if (isNum(x) && isFinite(x)) flat.push(x); }); });
    var lo = isNum(v.v_min) ? v.v_min : Math.min.apply(null, flat.concat([0]));
    var hi = isNum(v.v_max) ? v.v_max : Math.max.apply(null, flat.concat([0]));
    var diverging = v.scale === "diverging" || (v.scale !== "sequential" && lo < 0 && hi > 0);
    var LOW = [244, 247, 251], HIGH = [31, 95, 168], NEG = [179, 88, 6];
    function color(x) {
      if (!(isNum(x) && isFinite(x))) return "var(--paper)";
      if (diverging) {
        var m = Math.max(Math.abs(lo), Math.abs(hi)) || 1, t = Math.max(-1, Math.min(1, x / m));
        return rgb(t >= 0 ? mix(LOW, HIGH, t) : mix(LOW, NEG, -t));
      }
      return rgb(mix(LOW, HIGH, hi === lo ? 0.5 : (x - lo) / (hi - lo)));
    }
    function dark(x) {
      if (!(isNum(x) && isFinite(x))) return false;
      var t = diverging ? Math.abs(x) / (Math.max(Math.abs(lo), Math.abs(hi)) || 1) : (hi === lo ? 0.5 : (x - lo) / (hi - lo));
      return t > 0.55;
    }
    var rows = data.length, cols = Math.max.apply(null, data.map(function (r) { return r.length; }));
    var sums = v.show_row_sums;
    var cw = Math.max(46, Math.min(84, 480 / (cols + (sums ? 1 : 0)))), ch = Math.max(30, Math.min(44, 300 / rows));
    var x0 = 64, y0 = 26, W = x0 + cw * (cols + (sums ? 1.2 : 0)) + 8, H = y0 + ch * rows + 8;
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", style: "max-width:" + Math.round(W * 1.15) + "px" });
    var dec = isNum(v.decimals) ? v.decimals : 2;
    for (var j = 0; j < cols; j++)
      root.appendChild(svg("text", { "class": "tick", x: x0 + cw * j + cw / 2, y: y0 - 8, "text-anchor": "middle" },
        plain(labelAt(resolve(v.col_labels), j, ""))));
    if (sums) root.appendChild(svg("text", { "class": "tick", x: x0 + cw * cols + cw * 0.6, y: y0 - 8, "text-anchor": "middle" }, "row sum"));
    var desc = [];
    data.forEach(function (r, i) {
      root.appendChild(svg("text", { "class": "tick", x: x0 - 8, y: y0 + ch * i + ch / 2 + 4, "text-anchor": "end" },
        plain(labelAt(resolve(v.row_labels), i, ""))));
      var s = 0;
      for (var k = 0; k < cols; k++) {
        var x = r[k]; if (isNum(x)) s += x;
        root.appendChild(svg("rect", { x: x0 + cw * k + 1, y: y0 + ch * i + 1, width: cw - 2, height: ch - 2, fill: color(x), rx: 2 }));
        root.appendChild(svg("text", { "class": "val", x: x0 + cw * k + cw / 2, y: y0 + ch * i + ch / 2 + 4,
          "text-anchor": "middle", style: dark(x) ? "fill:#fff" : "fill:#17202e" }, fmt(x, dec)));
      }
      desc.push("row " + (i + 1) + ": " + r.map(function (x) { return fmt(x, dec); }).join(" "));
      if (sums) root.appendChild(svg("text", { "class": "val", x: x0 + cw * cols + cw * 0.6, y: y0 + ch * i + ch / 2 + 4,
        "text-anchor": "middle" }, fmt(s, dec)));
    });
    root.setAttribute("aria-label", plain(template(v.title || "Heatmap")) + ". " + desc.join("; "));
    return frame(v.title, v.caption, root);
  }

  function matrixTable(m, dec, rl, cl) {
    m = m.map(function (r) { return Array.isArray(r) ? r : [r]; });
    var t = el("table", { cls: "mtable" });
    var cols = Math.max.apply(null, m.map(function (r) { return r.length; }));
    if (rl || cl) {
      var hr = el("tr", null, [el("th")]);
      for (var j = 0; j < cols; j++) hr.appendChild(el("th", { scope: "col", html: labelAt(cl, j, "") }));
      t.appendChild(hr);
    }
    m.forEach(function (r, i) {
      var tr = el("tr");
      if (rl || cl) tr.appendChild(el("th", { scope: "row", html: labelAt(rl, i, "") }));
      r.forEach(function (x) { tr.appendChild(el("td", { text: fmt(x, dec) })); });
      t.appendChild(tr);
    });
    return t;
  }
  function matrixVisual(v) {
    var data = resolve(v.values || v.source);
    if (!Array.isArray(data)) return frame(v.title, v.caption, el("p", { cls: "note", text: "No values to show." }));
    if (!Array.isArray(data[0])) data = [data];
    return frame(v.title, v.caption, el("div", { cls: "mwrap" },
      [matrixTable(data, isNum(v.decimals) ? v.decimals : 3, resolve(v.row_labels), resolve(v.col_labels))]));
  }

  function primitives(v) {
    var W = num(v.width, 600), H = num(v.height, 220);
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
    var defs = svg("defs");
    var mk = svg("marker", { id: "arrowhead-" + (v._idx || 0), viewBox: "0 0 10 10", refX: 9, refY: 5,
                             markerUnits: "userSpaceOnUse", markerWidth: 11, markerHeight: 11,
                             orient: "auto-start-reverse" });
    mk.appendChild(svg("path", { d: "M0 0 L10 5 L0 10 z", style: "fill:var(--ink)" }));
    defs.appendChild(mk); root.appendChild(defs);
    var texts = [];
    (v.items || []).forEach(function (it) {
      var cls = it.style === "accent" ? " accent" : "";
      var dec = isNum(it.decimals) ? it.decimals : 2;
      var label = it.label !== undefined ? plain(template(it.label, dec)) : (it.text !== undefined ? plain(template(it.text, dec)) : "");
      var sw = num(it.stroke_width, null);
      if (it.shape === "box") {
        var x = num(it.x, 0), y = num(it.y, 0), w = num(it.w, 80), h = num(it.h, 40);
        root.appendChild(svg("rect", { "class": "prim-box" + cls, x: x, y: y, width: Math.max(0, w), height: Math.max(0, h), rx: 4,
                                        style: sw ? "stroke-width:" + sw : null }));
        if (label) root.appendChild(svg("text", { x: x + w / 2, y: y + h / 2 + 5, "text-anchor": "middle", "font-size": num(it.size, 14) }, label));
      } else if (it.shape === "circle") {
        var cx = num(it.x, 0), cy = num(it.y, 0), r = Math.max(0, num(it.r, 20));
        root.appendChild(svg("circle", { "class": "prim-box" + cls, cx: cx, cy: cy, r: r }));
        if (label) root.appendChild(svg("text", { x: cx, y: cy + 5, "text-anchor": "middle", "font-size": num(it.size, 13) }, label));
      } else if (it.shape === "arrow" || it.shape === "line") {
        var x1 = num(it.x1, 0), y1 = num(it.y1, 0), x2 = num(it.x2, 0), y2 = num(it.y2, 0);
        root.appendChild(svg("line", { "class": "prim-line" + cls, x1: x1, y1: y1, x2: x2, y2: y2,
          style: sw ? "stroke-width:" + Math.max(0.5, Math.min(14, sw)) : null,
          "marker-end": it.shape === "arrow" ? "url(#arrowhead-" + (v._idx || 0) + ")" : null }));
        if (label) root.appendChild(svg("text", { "class": "val", x: (x1 + x2) / 2 + num(it.dx, 0), y: (y1 + y2) / 2 - 6 + num(it.dy, 0),
                                                    "text-anchor": "middle" }, label));
      } else if (it.shape === "text") {
        root.appendChild(svg("text", { x: num(it.x, 0), y: num(it.y, 0), "text-anchor": it.anchor || "start",
          "font-size": num(it.size, 14), "font-weight": it.bold ? 650 : null }, label));
      }
      if (label) texts.push(label);
    });
    root.setAttribute("aria-label", plain(template(v.title || "Diagram")) + ": " + texts.join("; "));
    return frame(v.title, v.caption, root);
  }

  function renderVisuals() {
    var host = $("visuals"); host.innerHTML = "";
    (Array.isArray(SPEC.visuals) ? SPEC.visuals : []).forEach(function (v, i) {
      if (!v || typeof v !== "object") return;
      v._idx = i;
      var node;
      try {
        if (v.type === "bar") node = barChart(v);
        else if (v.type === "line") node = lineChart(v);
        else if (v.type === "heatmap") node = heatmap(v);
        else if (v.type === "matrix") node = matrixVisual(v);
        else if (v.type === "svg") node = primitives(v);
        else node = frame(v.title, null, el("p", { cls: "note", text: "Unsupported visual type: " + v.type }));
      } catch (e) {
        node = frame(v.title, null, el("p", { cls: "note", text: "This picture could not be drawn: " + e.message }));
      }
      host.appendChild(node);
    });
  }

  // ---------- results ----------
  function valueNode(val, dec) {
    if (Array.isArray(val) && Array.isArray(val[0])) {
      var t = matrixTable(val, dec); t.className = "mini"; return t;
    }
    return document.createTextNode(fmt(val, dec));
  }
  function renderReadout(outputs) {
    var host = $("readout"); host.innerHTML = "";
    var specs = Array.isArray(SPEC.outputs) ? SPEC.outputs : [];
    specs.forEach(function (o) {
      if (!o || !o.key || o.show === false) return;
      var val = outputs[o.key];
      if (!(isNum(val) || typeof val === "boolean")) return;
      var dd = el("dd", { text: fmt(val, isNum(o.decimals) ? o.decimals : 3) });
      if (o.unit) dd.appendChild(el("span", { cls: "unit", html: o.unit }));
      var key = "out:" + o.key, sig = JSON.stringify(val);
      if (key in lastValues && lastValues[key] !== sig) flash(dd);
      lastValues[key] = sig;
      host.appendChild(el("div", { title: o.help ? plain(o.help) : null }, [el("dt", { html: o.label || o.key }), dd]));
    });
  }
  function renderIntermediates(list) {
    var body = $("intermediates"); body.innerHTML = "";
    var panel = $("intermediates-panel");
    if (!Array.isArray(list) || !list.length) { panel.hidden = true; return; }
    panel.hidden = false;
    list.forEach(function (it, i) {
      if (!it) return;
      var dec = isNum(it.decimals) ? it.decimals : 4;
      var v = el("td", { cls: "v" }, [valueNode(it.value, dec)]);
      var key = "int:" + (it.label || i), sig = JSON.stringify(it.value);
      if (key in lastValues && lastValues[key] !== sig) flash(v);
      lastValues[key] = sig;
      body.appendChild(el("tr", null, [el("td", { html: it.label || "" }), v, el("td", { cls: "n", html: it.note || "" })]));
    });
  }
  function renderChecks(list) {
    var host = $("checks"); host.innerHTML = "";
    var panel = $("checks-panel");
    if (!Array.isArray(list) || !list.length) { panel.hidden = true; return; }
    panel.hidden = false;
    var bad = 0;
    list.forEach(function (c) {
      if (!c) return;
      var ok = c.pass === true;
      if (!ok) bad++;
      host.appendChild(el("li", { cls: ok ? "ok" : "bad" }, [
        el("span", { cls: "mark", "aria-hidden": "true", text: ok ? "✓" : "✗" }),
        el("span", null, [el("span", { cls: "sr-only", text: ok ? "Passes: " : "Fails: " }),
          el("span", { html: c.label || "" }), c.detail ? el("span", { cls: "detail", html: " — " + c.detail }) : null])
      ]));
    });
    $("checks-summary").textContent = bad ? ("Live checks: " + bad + " of " + list.length + " failing")
      : ("Live checks: all " + list.length + " pass");
  }
  function showError(msg) { var e = $("error"); e.textContent = msg; e.hidden = false; }
  function hideError() { var e = $("error"); e.hidden = true; e.textContent = ""; }

  function update() {
    if (!compute) {
      showError(computeLoadError ? "The calculation code could not be loaded: " + computeLoadError
        : "The calculation code could not be loaded, so no values can be shown.");
      return;
    }
    var res;
    try { res = compute(clone(state)); }
    catch (e) { showError("The calculation stopped with an error: " + (e && e.message ? e.message : e) + ". Try different input values or reset."); return; }
    if (!res || typeof res !== "object") { showError("The calculation returned no result."); return; }
    hideError();
    ctx.outputs = res.outputs && typeof res.outputs === "object" ? res.outputs : {};
    ctx.state = state;
    renderReadout(ctx.outputs);
    renderVisuals();
    renderIntermediates(res.intermediates);
    var checks = Array.isArray(res.checks) ? res.checks.slice() : [];
    INVARIANTS.forEach(function (inv) {
      var ok = false, detail = "";
      if (!inv.fn) detail = "could not be read";
      else {
        try { ok = !!inv.fn(ctx.outputs, clone(state)); } catch (e) { detail = "could not be evaluated"; }
      }
      checks.push({ label: inv.label, pass: ok, detail: detail });
    });
    renderChecks(checks);
    CONTROLS.forEach(function (c) {
      if (c.type === "vector" && (c.normalize || c.show_sum)) {
        var n = $("sum-" + c.id); if (!n) return;
        var s = (state[c.id] || []).reduce(function (a, b) { return a + (isNum(b) ? b : 0); }, 0);
        var off = c.normalize && Math.abs(s - 1) > 1e-6;
        n.textContent = "Sum = " + fmt(s, 4) + (off ? " (not 1)" : "");
        n.className = "sum" + (off ? " off" : "");
      }
    });
  }

  // ---------- static text sections ----------
  function renderText() {
    var s = SPEC.sections || {}, meta = SPEC.meta || {};
    setHTML("title", SPEC.title || "Interactive explainer");
    setHTML("subtitle", SPEC.subtitle || "");
    var src = [];
    if (meta.paper_title) src.push("Based on " + meta.paper_title);
    if (meta.section_label) src.push(meta.section_label);
    setHTML("source-line", src.join(", "));
    var idea = s.idea || {};
    setHTML("idea-what", idea.what || "");
    setHTML("idea-why", idea.why || "");
    if (s.equation) {
      $("equation").hidden = false;
      setHTML("equation-body", s.equation);
      $("equation-label").textContent = plain(meta.equation_label || "");
    }
    var tb = $("symbols-body");
    (s.symbols || []).forEach(function (r) {
      if (!r) return;
      tb.appendChild(el("tr", null, [el("td", { html: r.symbol }), el("td", { html: r.meaning }), el("td", { html: r.shape || "" })]));
    });
    if (SPEC.playground_intro) setHTML("playground-intro", SPEC.playground_intro);

    var ex = Array.isArray(s.explorations) ? s.explorations : [];
    ["explore-1", "explore-2"].forEach(function (id, i) {
      var host = $(id), e = ex[i];
      if (!e) { host.hidden = true; return; }
      host.appendChild(el("h2", { id: id + "-h", html: "Exploration " + (i + 1) + ": " + (e.title || "") }));
      var ol = el("ol", { cls: "steps" });
      [["Change", e.change], ["Observe", e.observe], ["Why", e.why]].forEach(function (p) {
        if (p[1]) ol.appendChild(el("li", null, [el("b", { text: p[0] }), el("span", { html: p[1] })]));
      });
      host.appendChild(ol);
      if (e.preset && typeof e.preset === "object" && Object.keys(e.preset).length) {
        var b = el("button", { type: "button", cls: "btn", text: "Set up this example in the playground" });
        b.addEventListener("click", function () { applyPreset(e.preset, i + 1); });
        host.appendChild(b);
      }
    });

    var lim = s.limitation || {};
    var heads = { limitation: "A limitation", assumption: "An assumption to keep in mind",
                  misconception: "A common misunderstanding" };
    $("limitation-h").textContent = heads[lim.kind] || "A limitation";
    setHTML("limitation-body", lim.text ? "<p>" + lim.text + "</p>" : "");

    var gr = SPEC.grounding || {};
    var cite = $("cite");
    [["Paper", meta.paper_title], ["Section", meta.section_label], ["Equation", meta.equation_label],
     ["Source", meta.source_url]].forEach(function (p) {
      if (!p[1]) return;
      cite.appendChild(el("dt", { text: p[0] }));
      // source_url is raw (not sanitized): always inserted as text, never parsed as HTML
      cite.appendChild(el("dd", p[0] === "Source" ? { text: String(p[1]) } : { html: p[1] }));
    });
    var fe = $("from-excerpt");
    var quotes = Array.isArray(gr.from_excerpt) ? gr.from_excerpt : [];
    if (quotes.length) quotes.forEach(function (q) { fe.appendChild(el("blockquote", { html: q })); });
    else fe.appendChild(el("p", { cls: "empty-note", text: gr.no_excerpt_note ||
      "No excerpt was supplied with this brief, so nothing here is quoted from the paper." }));
    var ul = $("our-simplifications");
    (Array.isArray(gr.our_simplifications) ? gr.our_simplifications : []).forEach(function (t) {
      ul.appendChild(el("li", { html: t }));
    });
    document.title = plain(SPEC.title || document.title);
  }

  function applyPreset(preset, n) {
    resetState(); // every exploration starts from the same known values
    Object.keys(preset).forEach(function (k) { if (k in byId) state[k] = clone(preset[k]); });
    // an explicitly given vector/matrix keeps its size: its count controls follow it
    CONTROLS.forEach(function (c) {
      var v = preset[c.id];
      if (!Array.isArray(v) || !v.length) return;
      if (c.type === "vector" && c.length_from && !(c.length_from in preset)) state[c.length_from] = v.length;
      if (c.type === "matrix" && Array.isArray(v[0])) {
        if (c.rows_from && !(c.rows_from in preset)) state[c.rows_from] = v.length;
        if (c.cols_from && !(c.cols_from in preset)) state[c.cols_from] = v[0].length;
      }
    });
    applyBindings();
    renderAllControls();
    update();
    var pg = $("playground");
    pg.scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth", block: "start" });
    pg.focus({ preventScroll: true });
    say("Exploration " + n + " is set up in the playground.");
  }

  // ---------- boot ----------
  function boot() {
    try { renderText(); } catch (e) { showError("Part of the text could not be shown: " + e.message); }
    resetState();
    renderAllControls();
    $("reset").addEventListener("click", function () {
      resetState(); renderAllControls(); update(); say("Inputs reset to their starting values.");
    });
    update();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
