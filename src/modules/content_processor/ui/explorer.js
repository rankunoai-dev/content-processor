(function () {
  "use strict";
  // Every value from `data` is third-party text: it is only ever set as textContent.
  var data = JSON.parse(document.getElementById("data").textContent);
  var state = { badge: null, q: "", boiler: false, current: null };

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }

  function badge(b) {
    return el("span", b, "badge b-" + b);
  }

  function visible(r) {
    if (state.badge && r.badge !== state.badge) return false;
    if (!state.q) return true;
    var q = state.q.toLowerCase();
    return r.url.toLowerCase().indexOf(q) >= 0 || r.text.toLowerCase().indexOf(q) >= 0;
  }

  function chip(label, pressed, onClick) {
    var c = el("button", label, "chip");
    c.type = "button";
    c.setAttribute("aria-pressed", String(pressed));
    c.onclick = onClick;
    return c;
  }

  function chips() {
    var box = document.getElementById("chips");
    box.textContent = "";
    var counts = {};
    data.records.forEach(function (r) {
      counts[r.badge] = (counts[r.badge] || 0) + 1;
    });
    box.appendChild(
      chip("All " + data.records.length, state.badge === null, function () {
        state.badge = null;
        render();
      })
    );
    Object.keys(counts).sort().forEach(function (b) {
      box.appendChild(
        chip(b + " " + counts[b], state.badge === b, function () {
          state.badge = b;
          render();
        })
      );
    });
  }

  function list() {
    var box = document.getElementById("list");
    box.textContent = "";
    var shown = 0;
    data.records.forEach(function (r, i) {
      if (!visible(r)) return;
      shown++;
      var b = el("button");
      b.type = "button";
      b.appendChild(badge(r.badge));
      b.appendChild(el("div", r.url));
      b.appendChild(el("div", r.words + " words, " + r.gaps.length + " gaps", "meta"));
      b.setAttribute("aria-current", String(state.current === i));
      b.onclick = function () {
        state.current = i;
        render();
      };
      box.appendChild(b);
    });
    if (!shown) box.appendChild(el("p", "No URL matches the filters.", "muted"));
  }

  function table(headers, rows) {
    var t = el("table");
    if (headers) {
      var hr = el("tr");
      headers.forEach(function (h) {
        hr.appendChild(el("th", h));
      });
      t.appendChild(hr);
    }
    rows.forEach(function (cells) {
      var tr = el("tr");
      cells.forEach(function (v, i) {
        tr.appendChild(el(!headers && i === 0 ? "th" : "td", v));
      });
      t.appendChild(tr);
    });
    return t;
  }

  function detail() {
    var box = document.getElementById("detail");
    box.textContent = "";
    if (state.current === null) {
      box.appendChild(el("p", "Select a URL to see its content, gaps and evidence.", "muted"));
      return;
    }
    var r = data.records[state.current];
    var h = el("h2");
    h.appendChild(badge(r.badge));
    h.appendChild(document.createTextNode(" " + r.url));
    box.appendChild(h);
    box.appendChild(
      table(null, [
        ["Final URL", r.final_url || ""],
        ["Aliases", r.aliases.join(" | ")],
        ["Captured", r.captured_at],
        ["Tier reached", r.tier || ""],
        ["Acquisition", r.vector.acquisition],
        ["Fidelity", r.vector.fidelity],
        ["State exploration", r.vector.state_exploration],
        ["Discovery closed", String(r.vector.discovery_closed)],
        ["Reasons", r.vector.reasons.join("; ")],
        ["Warnings", r.warnings.join("; ")],
      ])
    );

    box.appendChild(el("h2", "Gaps (" + r.gaps.length + ")"));
    if (r.gaps.length) {
      box.appendChild(
        table(
          ["Type", "Status", "Reason", "Evidence"],
          r.gaps.map(function (g) {
            return [g.type, g.status, g.reason, g.evidence.join(" ")];
          })
        )
      );
    } else {
      box.appendChild(el("p", "No gaps recorded.", "muted"));
    }

    var blocks = r.blocks.filter(function (b) {
      return state.boiler || !b.boiler;
    });
    var note = state.boiler ? "" : ", boilerplate hidden";
    box.appendChild(el("h2", "Content (" + blocks.length + " blocks" + note + ")"));
    var last = "";
    blocks.forEach(function (b) {
      var hp = b.heading.join(" › ");
      if (hp && hp !== last) {
        box.appendChild(el("div", hp, "hp"));
        last = hp;
      }
      var d = el("div", b.text, "block l-" + b.layer + " r-" + b.role);
      d.title = [b.layer, b.role, b.id].concat(b.evidence).join(" · ");
      box.appendChild(d);
    });
    if (!blocks.length) box.appendChild(el("p", "No content blocks.", "muted"));
  }

  function render() {
    chips();
    list();
    detail();
  }

  document.getElementById("q").addEventListener("input", function (e) {
    state.q = e.target.value;
    list();
  });
  document.getElementById("boiler").addEventListener("change", function (e) {
    state.boiler = e.target.checked;
    detail();
  });
  render();
})();
