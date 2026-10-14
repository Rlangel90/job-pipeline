/* Job pipeline: static front end.
 *
 * Reads docs/data/pipeline.enc (encrypted by the scanner), decrypts it in the browser with the
 * owner's passphrase, and renders a Sankey of the hiring pipeline plus a drill-down per application.
 * Without a passphrase it shows docs/data/demo.json so the page works for anyone who visits.
 */
(() => {
  "use strict";

  const ACTIVE_SINK = { id: "active", label: "In progress", kind: "sink" };
  const STORAGE_KEY = "job-pipeline.passphrase";

  // Stage colors: active stages use one ordinal blue ramp; terminal outcomes use status colors.
  // Values are selected per theme so each reads on its own surface.
  const COLORS = {
    light: {
      applied: "#86b6ef", recruiter_screen: "#5598e7", hiring_manager: "#2a78d6",
      technical: "#1c5cab", onsite: "#104281", offer: "#082651",
      accepted: "#0ca30c", rejected: "#d03b3b", withdrew: "#898781", ghosted: "#fab219",
      active: "#4a3aa7",
    },
    dark: {
      applied: "#cde2fb", recruiter_screen: "#9ec5f4", hiring_manager: "#6da7ec",
      technical: "#3987e5", onsite: "#256abf", offer: "#184f95",
      accepted: "#0ca30c", rejected: "#d03b3b", withdrew: "#898781", ghosted: "#fab219",
      active: "#9085e9",
    },
  };

  const state = {
    data: null,        // the pipeline JSON currently displayed
    mode: "demo",      // "demo" | "live"
    envelope: null,    // encrypted envelope when one exists
    selection: null,   // {type:"node", id} | {type:"link", source, target}
    view: "cards",
    query: "",
    flows: null,       // cached sankey inputs for the current data
    byId: null,        // lookups
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v; // only used with constant markup, never data
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c === null || c === undefined) continue;
      node.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return node;
  };

  const isDark = () => {
    const forced = document.documentElement.getAttribute("data-theme");
    if (forced === "dark") return true;
    if (forced === "light") return false;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  };
  const colorOf = (id) => (isDark() ? COLORS.dark : COLORS.light)[id] || "#898781";

  const fmtDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const fmtDateTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const ago = (iso) => {
    if (!iso) return "";
    const ms = Date.now() - new Date(iso).getTime();
    const days = Math.floor(ms / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    if (days < 30) return `${days}d ago`;
    const months = Math.floor(days / 30);
    return months === 1 ? "1 mo ago" : `${months} mo ago`;
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // ---------- data shaping ----------

  function index(data) {
    const stageById = Object.fromEntries(data.stages.map((s) => [s.id, s]));
    stageById[ACTIVE_SINK.id] = ACTIVE_SINK;
    return {
      stage: stageById,
      stageOrder: Object.fromEntries(data.stages.map((s, i) => [s.id, i])),
      contact: Object.fromEntries(data.contacts.map((c) => [c.id, c])),
      message: Object.fromEntries(data.messages.map((m) => [m.id, m])),
      event: Object.fromEntries(data.events.map((e) => [e.id, e])),
      app: Object.fromEntries(data.applications.map((a) => [a.id, a])),
    };
  }

  // The ordered, de-duplicated list of stage ids an application passed through,
  // ending in the virtual "active" sink when it is still open.
  function pathOf(app) {
    const seen = new Set();
    const out = [];
    for (const h of app.stage_history) {
      if (!seen.has(h.stage)) { seen.add(h.stage); out.push(h.stage); }
    }
    if (!app.terminal) out.push(ACTIVE_SINK.id);
    return out;
  }

  function buildFlows(data) {
    const active = data.stages.filter((s) => s.kind === "active");
    const terminal = data.stages.filter((s) => s.kind === "terminal");
    const nodeApps = new Map();
    const linkApps = new Map();
    const paths = {};
    for (const app of data.applications) {
      const path = pathOf(app);
      paths[app.id] = path;
      for (let i = 0; i < path.length; i++) {
        if (!nodeApps.has(path[i])) nodeApps.set(path[i], new Set());
        nodeApps.get(path[i]).add(app.id);
        if (i < path.length - 1) {
          const key = path[i] + ">" + path[i + 1];
          if (!linkApps.has(key)) linkApps.set(key, new Set());
          linkApps.get(key).add(app.id);
        }
      }
    }
    const presentActive = active.filter((s) => nodeApps.has(s.id));
    const column = {};
    presentActive.forEach((s, i) => { column[s.id] = i; });
    const lastCol = presentActive.length;
    const nodes = [];
    for (const s of presentActive) nodes.push({ id: s.id, label: s.label, kind: "active", column: column[s.id], apps: nodeApps.get(s.id) });
    if (nodeApps.has(ACTIVE_SINK.id)) nodes.push({ id: ACTIVE_SINK.id, label: ACTIVE_SINK.label, kind: "sink", column: lastCol, apps: nodeApps.get(ACTIVE_SINK.id) });
    for (const s of terminal) if (nodeApps.has(s.id)) nodes.push({ id: s.id, label: s.label, kind: "terminal", column: lastCol, apps: nodeApps.get(s.id) });
    const links = [];
    for (const [key, apps] of linkApps) {
      const [source, target] = key.split(">");
      links.push({ source, target, value: apps.size, apps });
    }
    return { nodes, links, paths, nodeApps, linkApps };
  }

  function computeStats(data) {
    const apps = data.applications;
    const n = apps.length;
    const active = apps.filter((a) => !a.terminal).length;
    const beyondApplied = apps.filter((a) => a.stage_history.some((h) => h.stage !== "applied" && h.stage !== "ghosted")).length;
    const interviews = apps.filter((a) => a.stage_history.some((h) => ["recruiter_screen", "hiring_manager", "technical", "onsite", "offer", "accepted"].includes(h.stage))).length;
    const offers = apps.filter((a) => a.stage_history.some((h) => h.stage === "offer" || h.stage === "accepted")).length;
    const rejected = apps.filter((a) => a.current_stage === "rejected").length;
    const ghosted = apps.filter((a) => a.current_stage === "ghosted").length;
    return { n, active, beyondApplied, interviews, offers, rejected, ghosted, responseRate: n ? beyondApplied / n : 0 };
  }

  // ---------- rendering: stats ----------

  function renderStats() {
    const s = computeStats(state.data);
    const tiles = [
      ["Applications", s.n, `since ${fmtDate(state.data.scan_window_start)}`],
      ["In progress", s.active, `${s.rejected} rejected, ${s.ghosted} ghosted`],
      ["Response rate", `${Math.round(s.responseRate * 100)}%`, `${s.beyondApplied} heard back`],
      ["Interviews", s.interviews, "reached a screen or later"],
      ["Offers", s.offers, s.offers === 1 ? "one offer" : "offers so far"],
    ];
    const wrap = $("#stats");
    wrap.replaceChildren(...tiles.map(([label, value, sub]) =>
      el("div", { class: "stat" }, [
        el("div", { class: "label", text: label }),
        el("div", { class: "value", text: String(value) }),
        el("div", { class: "sub", text: sub }),
      ])));
  }

  // ---------- rendering: sankey ----------

  function renderSankey() {
    const flows = state.flows;
    const svg = d3.select("#sankey");
    svg.selectAll("*").remove();
    const wrap = $("#sankey-wrap");
    const W = Math.max(300, wrap.clientWidth);
    if (!flows.nodes.length || !flows.links.length) {
      svg.attr("viewBox", `0 0 ${W} 80`).attr("height", 80);
      svg.append("text").attr("x", W / 2).attr("y", 46).attr("text-anchor", "middle").attr("class", "ncount").text("No applications yet");
      renderLegend();
      return;
    }
    const vertical = W < 640;
    const H = vertical ? 600 : 440;
    const margin = vertical ? { top: 26, right: 10, bottom: 34, left: 10 } : { top: 10, right: 118, bottom: 10, left: 10 };
    const innerW = W - margin.left - margin.right;
    const innerH = H - margin.top - margin.bottom;
    svg.attr("viewBox", `0 0 ${W} ${H}`).attr("height", H);

    const gen = d3.sankey()
      .nodeId((d) => d.id)
      .nodeWidth(vertical ? 14 : 16)
      .nodePadding(vertical ? 10 : 20)
      .nodeAlign((d) => d.column)
      .iterations(32)
      .extent(vertical ? [[0, 0], [innerH, innerW]] : [[0, 0], [innerW, innerH]]);
    const graph = gen({
      nodes: flows.nodes.map((n) => ({ ...n })),
      links: flows.links.map((l) => ({ ...l })),
    });
    // Pin every node to its pipeline column. The generator derives its column count from the
    // longest path in the data, so when nobody has reached the later stages it would otherwise
    // fold the terminal nodes into an earlier column.
    {
      const lastCol = Math.max(...graph.nodes.map((d) => d.column));
      const depthExtent = vertical ? innerH : innerW;
      const kx = lastCol ? (depthExtent - gen.nodeWidth()) / lastCol : 0;
      for (const n of graph.nodes) { n.x0 = n.column * kx; n.x1 = n.x0 + gen.nodeWidth(); }
      gen.update(graph);
    }

    // Screen geometry helpers. In vertical mode the layout is computed transposed.
    const nx = (n) => (vertical ? n.y0 : n.x0);
    const ny = (n) => (vertical ? n.x0 : n.y0);
    const nw = (n) => (vertical ? n.y1 - n.y0 : n.x1 - n.x0);
    const nh = (n) => (vertical ? n.x1 - n.x0 : n.y1 - n.y0);
    const linkPath = vertical
      ? (d) => {
          const y0 = d.source.x1, y1 = d.target.x0, x0 = d.y0, x1 = d.y1;
          const ym = (y0 + y1) / 2;
          return `M${x0},${y0}C${x0},${ym} ${x1},${ym} ${x1},${y1}`;
        }
      : d3.sankeyLinkHorizontal();

    const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);

    const isSelectedLink = (d) => state.selection && state.selection.type === "link" && state.selection.source === d.source.id && state.selection.target === d.target.id;
    const isSelectedNode = (d) => state.selection && state.selection.type === "node" && state.selection.id === d.id;
    const linkTouchesSel = (d) => {
      if (!state.selection) return true;
      if (state.selection.type === "link") return isSelectedLink(d);
      return d.source.id === state.selection.id || d.target.id === state.selection.id;
    };
    const nodeTouchesSel = (d) => {
      if (!state.selection) return true;
      if (state.selection.type === "node") return d.id === state.selection.id || graph.links.some((l) => linkTouchesSel(l) && (l.source.id === d.id || l.target.id === d.id));
      return d.id === state.selection.source || d.id === state.selection.target;
    };

    const link = g.append("g").selectAll("path").data(graph.links).join("path")
      .attr("class", (d) => "link" + (linkTouchesSel(d) ? "" : " dim"))
      .attr("d", linkPath)
      .attr("stroke", (d) => colorOf(d.target.id))
      .attr("stroke-opacity", (d) => (isSelectedLink(d) ? 0.85 : 0.42))
      .attr("stroke-width", (d) => Math.max(1.5, d.width))
      .on("click", (ev, d) => { ev.stopPropagation(); select({ type: "link", source: d.source.id, target: d.target.id }); })
      .on("mousemove", (ev, d) => showTip(ev, `${d.source.label} → ${d.target.label}: ${plural(d.value, "application")}`))
      .on("mouseleave", hideTip);
    link.append("title").text((d) => `${d.source.label} → ${d.target.label}: ${d.value}`);

    const node = g.append("g").selectAll("g").data(graph.nodes).join("g")
      .attr("class", (d) => "node" + (nodeTouchesSel(d) ? "" : " dim"))
      .on("click", (ev, d) => { ev.stopPropagation(); select({ type: "node", id: d.id }); })
      .on("mousemove", (ev, d) => showTip(ev, `${d.label}: ${plural(d.value, "application")}`))
      .on("mouseleave", hideTip);
    node.append("rect")
      .attr("x", nx).attr("y", ny).attr("width", nw).attr("height", (d) => Math.max(2, nh(d)))
      .attr("fill", (d) => colorOf(d.id))
      .attr("stroke-width", (d) => (isSelectedNode(d) ? 2.5 : 1.5))
      .attr("stroke", (d) => (isSelectedNode(d) ? "var(--ink)" : "var(--surface)"));

    if (vertical) {
      // One stage per row, so a label above its bar has nothing to collide with except the
      // chart edge. The bottom row holds every outcome side by side: a label that does not fit
      // its bar shrinks to the count, and the legend under the chart carries the names.
      const bottomRow = Math.max(...graph.nodes.map((d) => d.column));
      node.each(function (d) {
        const sel = d3.select(this);
        const below = d.column === bottomRow;
        const y = below ? ny(d) + nh(d) + 14 : ny(d) - 7;
        let cx = nx(d) + nw(d) / 2;
        const t = sel.append("text").attr("class", "nlabel").attr("y", y).attr("text-anchor", "middle").text(`${d.label} · ${d.value}`);
        const tw = t.node().getComputedTextLength();
        if (below && tw > nw(d) + 6) {
          t.text(String(d.value)).attr("class", "ncount");
        } else {
          cx = Math.max(tw / 2, Math.min(innerW - tw / 2, cx));
        }
        t.attr("x", cx);
      });
    } else {
      // Every label sits to the right of its node; the text halo keeps it readable over ribbons.
      node.each(function (d) {
        const sel = d3.select(this);
        const x = nx(d) + nw(d) + 6;
        const cy = ny(d) + nh(d) / 2;
        sel.append("text").attr("class", "nlabel").attr("x", x).attr("y", cy - 2).text(d.label);
        sel.append("text").attr("class", "ncount").attr("x", x).attr("y", cy + 12).text(plural(d.value, "application"));
      });
    }

    svg.on("click", () => select(null));
    renderLegend();
  }

  function renderLegend() {
    const legend = $("#legend");
    const nodes = state.flows.nodes;
    legend.replaceChildren(...nodes.map((n) =>
      el("button", {
        type: "button",
        class: "chip" + (state.selection && state.selection.type === "node" && state.selection.id === n.id ? " on" : ""),
        onclick: () => select(state.selection && state.selection.type === "node" && state.selection.id === n.id ? null : { type: "node", id: n.id }),
      }, [el("span", { class: "sw", style: `background:${colorOf(n.id)}` }), n.label, el("b", { text: String(n.apps.size) })])));
  }

  const tip = $("#tooltip");
  function showTip(ev, text) {
    if (window.matchMedia("(hover: none)").matches) return;
    tip.textContent = text;
    tip.hidden = false;
    const pad = 14;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    const r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 8) y = ev.clientY - r.height - pad;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.hidden = true; }

  function select(sel) {
    state.selection = sel;
    renderSankey();
    renderList();
    const box = $("#selection");
    if (!sel) { box.hidden = true; return; }
    const S = state.byId.stage;
    const text = sel.type === "node"
      ? `${S[sel.id].label}: ${plural(state.flows.nodeApps.get(sel.id).size, "application")}`
      : `${S[sel.source].label} → ${S[sel.target].label}: ${plural(state.flows.linkApps.get(sel.source + ">" + sel.target).size, "application")}`;
    $("#selection-text").textContent = text;
    box.hidden = false;
  }

  // ---------- rendering: list ----------

  function visibleApps() {
    let apps = state.data.applications.slice();
    if (state.selection) {
      const set = state.selection.type === "node"
        ? state.flows.nodeApps.get(state.selection.id)
        : state.flows.linkApps.get(state.selection.source + ">" + state.selection.target);
      apps = apps.filter((a) => set && set.has(a.id));
    }
    const q = state.query.trim().toLowerCase();
    if (q) {
      apps = apps.filter((a) => {
        const people = a.contact_ids.map((id) => state.byId.contact[id]).filter(Boolean).map((c) => `${c.name} ${c.email} ${c.title}`).join(" ");
        return `${a.company} ${a.role} ${a.notes} ${a.source} ${people}`.toLowerCase().includes(q);
      });
    }
    apps.sort((a, b) => (a.last_activity_at < b.last_activity_at ? 1 : -1));
    return apps;
  }

  function stagePill(app) {
    const stage = state.byId.stage[app.current_stage];
    return el("span", { class: "pill" }, [el("span", { class: "sw", style: `background:${colorOf(app.current_stage)}` }), stage ? stage.label : app.current_stage]);
  }

  function renderList() {
    const apps = visibleApps();
    const list = $("#list");
    $("#app-count").textContent = state.selection || state.query ? `${apps.length} of ${state.data.applications.length}` : `${apps.length}`;
    list.className = "list " + state.view;
    if (!apps.length) { list.replaceChildren(el("div", { class: "empty", text: "Nothing matches." })); return; }
    if (state.view === "table") {
      const rows = apps.map((a) => el("tr", { class: "row", onclick: () => openSheet(a.id) }, [
        el("td", { text: a.company }),
        el("td", { text: a.role || "—" }),
        el("td", {}, [stagePill(a)]),
        el("td", { class: "num", text: fmtDate(a.applied_at) }),
        el("td", { class: "num", text: fmtDate(a.last_activity_at) }),
        el("td", { class: "num", text: String(a.contact_ids.length) }),
      ]));
      const table = el("table", { class: "tbl" }, [
        el("thead", {}, el("tr", {}, ["Company", "Role", "Stage", "Applied", "Last activity", "People"].map((h) => el("th", { text: h })))),
        el("tbody", {}, rows),
      ]);
      list.replaceChildren(el("div", { class: "tbl-wrap" }, table));
      return;
    }
    list.replaceChildren(...apps.map((a) => el("button", { type: "button", class: "app", onclick: () => openSheet(a.id) }, [
      el("div", { class: "co", text: a.company }),
      stagePill(a),
      el("div", { class: "role", text: a.role || "Role not captured yet" }),
      el("div", { class: "when", text: `Applied ${fmtDate(a.applied_at)} · last activity ${ago(a.last_activity_at)} · ${a.contact_ids.length ? plural(a.contact_ids.length, "person") : "no contacts yet"}` }),
    ])));
  }

  // ---------- detail sheet ----------

  function openSheet(appId) {
    const a = state.byId.app[appId];
    if (!a) return;
    const S = state.byId.stage;
    const body = $("#sheet-body");
    const parts = [];
    parts.push(el("h2", { text: a.company }));
    parts.push(el("div", { class: "role", text: a.role || "Role not captured yet" }));

    const dl = el("dl", { class: "kv" });
    const kv = (k, v) => { if (v) { dl.append(el("dt", { text: k }), el("dd", {}, [v])); } };
    kv("Stage", stagePill(a));
    kv("Applied", fmtDate(a.applied_at));
    kv("Source", a.source.replace("_", " "));
    kv("Last activity", `${fmtDateTime(a.last_activity_at)} (${ago(a.last_activity_at)})`);
    if (a.origin === "manual") kv("Entered", "by hand, no email trail");
    parts.push(dl);

    if (a.notes) parts.push(el("div", { class: "sect" }, [el("h3", { text: "Notes" }), el("div", { class: "notes", text: a.notes })]));

    const hist = el("ul", { class: "timeline" }, a.stage_history.map((h) =>
      el("li", { style: `--dot:${colorOf(h.stage)}` }, [
        el("div", { text: (S[h.stage] || { label: h.stage }).label + (h.evidence && h.evidence !== "confirmation" ? ` · ${h.evidence.replace("_", " ")}` : "") }),
        el("div", { class: "d", text: fmtDateTime(h.at) }),
      ])));
    parts.push(el("div", { class: "sect" }, [el("h3", { text: "Stage history" }), hist]));

    const events = a.event_ids.map((id) => state.byId.event[id]).filter(Boolean).sort((x, y) => (x.at < y.at ? -1 : 1));
    if (events.length) {
      parts.push(el("div", { class: "sect" }, [el("h3", { text: "Interviews on the calendar" }),
        el("ul", { class: "timeline" }, events.map((e) => el("li", { style: `--dot:${colorOf(e.stage || "recruiter_screen")}` }, [
          el("div", { text: e.title }),
          el("div", { class: "d", text: fmtDateTime(e.at) + (e.attendees.length ? ` · with ${e.attendees.map((p) => p.name || p.email).join(", ")}` : "") }),
        ])))]));
    }

    const people = a.contact_ids.map((id) => state.byId.contact[id]).filter(Boolean);
    parts.push(el("div", { class: "sect" }, [el("h3", { text: "People" }),
      people.length
        ? el("div", {}, people.map((c) => el("div", { class: "person" }, [
            el("div", { class: "nm", text: c.name || c.email }),
            el("div", { class: "ls", text: `last ${ago(c.last_seen)}` }),
            el("div", { class: "ti", text: [c.title, c.company].filter(Boolean).join(" · ") }),
            el("div", { class: "em" }, [el("a", { href: `mailto:${c.email}`, text: c.email })]),
          ])))
        : el("div", { class: "muted", text: "No named person yet, only automated mail." })]));

    if (a.description && a.description.text) {
      const d = a.description;
      parts.push(el("div", { class: "sect" }, [el("h3", { text: "Job description" }),
        el("details", { class: "jd" }, [
          el("summary", { text: `${d.source === "manual" ? "Pasted by hand" : "Captured " + fmtDate(d.fetched_at)} · ${Math.round(d.text.length / 1000)}k chars` }),
          d.url ? el("div", {}, [el("a", { href: d.url, target: "_blank", rel: "noopener", text: "Open the posting" })]) : null,
          el("div", { class: "jdtext", text: d.text }),
        ])]));
    } else if (a.description && a.description.url) {
      parts.push(el("div", { class: "sect" }, [el("h3", { text: "Job description" }), el("a", { href: a.description.url, target: "_blank", rel: "noopener", text: "Open the posting" })]));
    } else {
      parts.push(el("div", { class: "sect" }, [el("h3", { text: "Job description" }), el("div", { class: "muted", text: "Not captured. Paste it into the overrides file to attach it." })]));
    }

    const msgs = a.message_ids.map((id) => state.byId.message[id]).filter(Boolean).sort((x, y) => (x.date < y.date ? -1 : 1));
    parts.push(el("div", { class: "sect" }, [el("h3", { text: `Emails (${msgs.length})` }),
      msgs.length ? el("div", {}, msgs.map((m) => el("details", { class: "mail" }, [
        el("summary", {}, [
          el("span", { class: "arrow", text: m.direction === "out" ? "→" : "←", title: m.direction === "out" ? "sent" : "received" }),
          el("span", { class: "sub", text: m.subject || "(no subject)" }),
          el("span", { class: "d", text: fmtDateTime(m.date) }),
          el("span", { class: "snip", text: (m.direction === "out" ? "to " + m.to.map((p) => p.name || p.email).join(", ") : "from " + (m.from.name || m.from.email)) + (m.snippet ? " · " + m.snippet : "") }),
        ]),
        el("div", { class: "body", text: m.body || m.snippet || "" }),
        m.gmail_url ? el("a", { class: "open", href: m.gmail_url, target: "_blank", rel: "noopener", text: "Open in Gmail" }) : null,
      ]))) : el("div", { class: "muted", text: "No emails attached." })]));

    body.replaceChildren(...parts);
    body.scrollTop = 0;
    $("#sheet").hidden = false;
    $("#sheet-backdrop").hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeSheet() {
    $("#sheet").hidden = true;
    $("#sheet-backdrop").hidden = true;
    document.body.style.overflow = "";
  }

  // ---------- data loading, decryption ----------

  const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

  async function decryptEnvelope(env, passphrase) {
    const enc = new TextEncoder();
    const keyMaterial = await crypto.subtle.importKey("raw", enc.encode(passphrase), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey(
      { name: "PBKDF2", salt: b64(env.salt), iterations: env.iterations, hash: "SHA-256" },
      keyMaterial, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv) }, key, b64(env.ct));
    let bytes = new Uint8Array(plain);
    if (env.compression === "gzip") {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      bytes = new Uint8Array(await new Response(stream).arrayBuffer());
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  function setData(data, mode) {
    state.data = data;
    state.mode = mode;
    state.byId = index(data);
    state.flows = buildFlows(data);
    state.selection = null;
    const badge = $("#mode-badge");
    badge.textContent = mode === "live" ? "Live" : "Demo data";
    badge.className = "badge " + (mode === "live" ? "badge-live" : "badge-demo");
    $("#generated").textContent = data.generated_at ? `updated ${ago(data.generated_at)}` : "";
    $("#selection").hidden = true;
    renderStats();
    renderSankey();
    renderList();
  }

  function remembered() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; } catch (e) { return ""; }
  }
  function remember(pass) {
    try { if (pass) localStorage.setItem(STORAGE_KEY, pass); else localStorage.removeItem(STORAGE_KEY); } catch (e) { /* storage unavailable */ }
  }

  async function tryUnlock(pass, persist) {
    const btn = $("#unlock-submit");
    btn.disabled = true;
    btn.textContent = "Decrypting…";
    try {
      const data = await decryptEnvelope(state.envelope, pass);
      if (persist) remember(pass);
      setData(data, "live");
      $("#unlock").hidden = true;
      $("#unlock-btn").hidden = true;
      $("#lock-btn").hidden = false;
      $("#notice").hidden = true;
      return true;
    } catch (e) {
      const err = $("#unlock-error");
      err.textContent = "That passphrase did not open the file.";
      err.hidden = false;
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Unlock";
    }
  }

  async function init() {
    const [demoRes, encRes] = await Promise.all([
      fetch("data/demo.json", { cache: "no-store" }).catch(() => null),
      fetch("data/pipeline.enc", { cache: "no-store" }).catch(() => null),
    ]);
    let demo = null;
    if (demoRes && demoRes.ok) demo = await demoRes.json();
    if (encRes && encRes.ok) {
      try { state.envelope = await encRes.json(); } catch (e) { state.envelope = null; }
    }
    if (demo) setData(demo, "demo");
    const notice = $("#notice");
    if (state.envelope) {
      $("#unlock-btn").hidden = false;
      const count = state.envelope.applications;
      notice.textContent = `Showing demo data. The real pipeline (${plural(count || 0, "application")}, refreshed ${ago(state.envelope.generated_at)}) is encrypted. Unlock to see it.`;
      notice.hidden = false;
      const saved = remembered();
      if (saved) {
        const ok = await tryUnlock(saved, false);
        if (!ok) remember("");
      }
    } else if (demo) {
      notice.textContent = "Showing demo data. No encrypted pipeline file has been published yet.";
      notice.hidden = false;
    } else {
      notice.textContent = "No data files found.";
      notice.hidden = false;
    }
  }

  // ---------- wiring ----------

  $("#unlock-btn").addEventListener("click", () => {
    $("#unlock-error").hidden = true;
    $("#unlock").hidden = false;
    setTimeout(() => $("#passphrase").focus(), 50);
  });
  $("#unlock-cancel").addEventListener("click", () => { $("#unlock").hidden = true; });
  $("#unlock-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    tryUnlock($("#passphrase").value, $("#remember").checked);
  });
  $("#lock-btn").addEventListener("click", async () => {
    remember("");
    $("#lock-btn").hidden = true;
    $("#unlock-btn").hidden = false;
    closeSheet();
    const res = await fetch("data/demo.json", { cache: "no-store" }).catch(() => null);
    if (res && res.ok) setData(await res.json(), "demo");
    $("#notice").textContent = "Locked. Showing demo data.";
    $("#notice").hidden = false;
  });
  $("#clear-sel").addEventListener("click", () => select(null));
  $("#search").addEventListener("input", (ev) => { state.query = ev.target.value; renderList(); });
  for (const b of document.querySelectorAll(".seg button")) {
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      for (const o of document.querySelectorAll(".seg button")) o.classList.toggle("on", o === b);
      renderList();
    });
  }
  $("#sheet-close").addEventListener("click", closeSheet);
  $("#sheet-backdrop").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeSheet(); $("#unlock").hidden = true; } });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.flows) renderSankey(); }, 120);
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state.data) { renderSankey(); renderList(); } });
  }

  init();
})();
