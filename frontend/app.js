// Same-origin: works locally (Flask serves both frontend and API on one
// port) and in production on Render (single web service, no CORS needed).
const API = "/api";

const TYPE_COLORS = { Opening: "#2563eb", Closing: "#0f1115", Remodel: "#9ca3af", Bankruptcy: "#b91c1c" };
const COMPLETION_STATUSES = [
  "Add", "Edit", "Already Updated", "Not Relevant", "Not Accessible", "Send to the Calling Team",
];
const SOURCE_LABELS = {
  banner: "Store News", ct_scoop: "CT Scoop", restaurant: "Restaurant News",
  daily_news: "Daily News", daily_news_bankruptcy: "Distress Signals",
  businessdebut: "BusinessDebut",
};

// The Dashboard tab is just another "source" as far as the grid engine is
// concerned: fetching store_events with no source param already returns
// everything, so the Dashboard reuses the exact same resize/wrap/fullscreen/
// pagination/per-column-filter grid as every other tab instead of a
// separate, parallel implementation -- its filter bar *is* the grid's
// existing Excel-style column filters (Source, Date Added, Event, Status,
// Assigned to, Completion all already fit that pattern), and the summary
// cards/charts/analyst-activity table below just recompute from whatever
// rows are currently filtered.
const DASHBOARD_SOURCE = "__dashboard__";
SOURCE_LABELS[DASHBOARD_SOURCE] = "All Events";

// Distress Signals is one nav tab covering two different pipelines that
// happen to share a UI: bankruptcy filings (store_events, workflow-enabled)
// and WARN Act layoff notices (scraped_articles, read-only reference data —
// the DB's own source CHECK constraint only allows 'warn' in scraped_articles,
// not store_events, which is why this isn't just "the Articles view of the
// same source" like every other tab).
const ARTICLES_SOURCE_OVERRIDE = { daily_news_bankruptcy: "warn" };
const SUBTAB_LABEL_OVERRIDE = {
  daily_news_bankruptcy: { extraction: "Bankruptcy Filings", articles: "WARN Layoffs" },
};

let analystsCache = [];
let marksCache = {};
let currentUser = null; // { analyst_id, analyst_name, email, role }

function loadStoredUser() {
  try {
    const raw = localStorage.getItem("demoUser");
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}
function storeUser(u) { localStorage.setItem("demoUser", JSON.stringify(u)); }
function clearStoredUser() { localStorage.removeItem("demoUser"); }

function analystName(id) {
  if (!id) return "Unassigned";
  const a = analystsCache.find((x) => x.analyst_id === id);
  return a ? a.analyst_name : id;
}

// A single article_link can cover several companies at once (a roundup story
// like "8 restaurants opening in Frisco"), producing several store_events rows
// that all share the same article_link. article_marks is keyed by a single
// text key, so keying it off article_link alone made every row on that
// article share one mark/assignment — checking one company as done or
// assigning it checked/assigned all of them. Keying off (article_link, company)
// instead gives each row its own mark.
function markKey(r) {
  return `${r.article_link}::${r.company_name || ""}`;
}

// ---- source table state -------------------------------------------------
let currentSource = null;
let currentRows = [];          // raw rows from the API for the active source
let filters = {};              // colKey -> filter state (shape depends on col.type)
let globalSearchText = "";
let sortState = { key: null, dir: 1 };
let hiddenColumns = new Set(); // colKeys hidden via the Columns menu
let selectedIds = new Set();   // event_id values checked for bulk actions
let selectionAnchorId = null;  // event_id of the last row clicked, for shift-click ranges
// Default to 500 — big enough that this analyst team's actual data volumes
// (dozens to a few hundred rows per source) render as a single page in
// practice, while the pagination bar stays in place as a safety net if any
// source's history grows past that.
let sourcePage = { page: 1, pageSize: 500 };
let articlesPage = { page: 1, pageSize: 500 };
let sourceWrap = false;
let articlesWrap = false;
let gridFullscreen = false;

// How much wider each normally-clipped column gets, on top of its current
// (possibly user-resized) width, while wrap mode is on — makes the toggle
// visibly do something even for columns whose content already fit on one
// line, not just the already-wrapping Description/Company columns.
const WRAP_WIDTH_BUMP = 90;

const ICON_EXPAND = '<svg class="icon-svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
const ICON_COLLAPSE = '<svg class="icon-svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>';

function renderFullscreenButtons() {
  const label = gridFullscreen
    ? `${ICON_COLLAPSE} Exit fullscreen`
    : `${ICON_EXPAND} Fullscreen`;
  ["sourceFullscreenBtn", "articlesFullscreenBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.innerHTML = label;
    btn.classList.toggle("active", gridFullscreen);
  });
}

function toggleFullscreen() {
  gridFullscreen = !gridFullscreen;
  document.getElementById("sourcePanel").classList.toggle("grid-fullscreen", gridFullscreen);
  document.body.classList.toggle("grid-fullscreen-active", gridFullscreen);
  renderFullscreenButtons();
}

function toggleSourceWrap() {
  sourceWrap = !sourceWrap;
  document.getElementById("sourceTableScroll").classList.toggle("wrap-active", sourceWrap);
  document.getElementById("sourceWrapBtn").classList.toggle("active", sourceWrap);
  renderTableHead(); // rebuilds the colgroup with/without the wrap width bump
}

function toggleArticlesWrap() {
  articlesWrap = !articlesWrap;
  document.getElementById("articlesTableScroll").classList.toggle("wrap-active", articlesWrap);
  document.getElementById("articlesWrapBtn").classList.toggle("active", articlesWrap);
  renderArticlesTableHead(); // rebuilds the colgroup with/without the wrap width bump
}

// ---- header type icons + pagination glyphs ---------------------------------
const TYPE_ICONS = {
  date: '<svg class="icon-svg th-type-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  select: '<svg class="icon-svg th-type-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
  text: '<svg class="icon-svg th-type-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="17" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="17" y1="18" x2="3" y2="18"/></svg>',
  link: '<svg class="icon-svg th-type-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
};
function typeIcon(colKey, colType) {
  if (colKey === "article" || colKey === "link") return TYPE_ICONS.link;
  if (colKey === "published" || colKey === "date") return TYPE_ICONS.date;
  return TYPE_ICONS[colType] || TYPE_ICONS.text;
}

// ---- column widths (resizable columns, persisted per browser) -------------
function loadColWidths(storageKey) {
  try {
    const raw = localStorage.getItem(storageKey);
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}
function saveColWidths(storageKey, widths) {
  try { localStorage.setItem(storageKey, JSON.stringify(widths)); } catch (e) { /* ignore */ }
}

const SOURCE_DEFAULT_WIDTHS = {
  source: 130, company: 190, event: 100, status: 150, date: 120, location: 150,
  description: 320, article: 90, published: 110, dateappended: 110, markdone: 190, assignedto: 160,
  __assign: 170, __action: 190,
};
const ARTICLE_DEFAULT_WIDTHS = {
  title: 260, company: 170, summary: 320, location: 140, published: 110,
  employees: 140, layoffdate: 120, closuretype: 140, link: 90,
};

let sourceColWidths = loadColWidths("colWidths_source_v1");
let articlesColWidths = loadColWidths("colWidths_articles_v1");

function colWidth(widths, defaults, key) {
  return widths[key] || defaults[key] || 140;
}

// Adds a drag handle to `th` that resizes the matching <col data-colkey>
// inside the colgroup with id `colGroupId`. Reads the starting width from the
// header cell's actual rendered size (reliable across browsers, unlike
// reading a <col> element's own box), and persists the final width.
function attachColResize(th, colKey, colGroupId, widthsState, storageKey) {
  const handle = document.createElement("span");
  handle.className = "col-resize-handle";
  th.appendChild(handle);
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const colEl = document.querySelector(`#${colGroupId} col[data-colkey="${CSS.escape(colKey)}"]`);
    if (!colEl) return;
    const startX = e.clientX;
    const startWidth = th.getBoundingClientRect().width;
    handle.classList.add("resizing");
    document.body.classList.add("col-resizing");
    function onMove(ev) {
      colEl.style.width = `${Math.max(60, startWidth + (ev.clientX - startX))}px`;
    }
    function onUp() {
      handle.classList.remove("resizing");
      document.body.classList.remove("col-resizing");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      widthsState[colKey] = parseInt(colEl.style.width, 10);
      saveColWidths(storageKey, widthsState);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// ---- pagination -------------------------------------------------------------
// Renders a first/prev/next/last + go-to-page + page-size bar into
// `containerId`, operating on `state` ({page, pageSize}), and calls
// `onChange()` (the owning table's render function) after any interaction.
function renderPaginationBar(containerId, state, totalRows, onChange) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const totalPages = Math.max(1, Math.ceil(totalRows / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;
  if (state.page < 1) state.page = 1;
  const startRow = totalRows === 0 ? 0 : (state.page - 1) * state.pageSize + 1;
  const endRow = Math.min(totalRows, state.page * state.pageSize);
  const atFirst = state.page <= 1;
  const atLast = state.page >= totalPages;

  container.innerHTML = `
    <span class="count-badge">${startRow}–${endRow} of ${totalRows} rows</span>
    <div class="pagination-controls">
      <button type="button" class="page-btn" data-act="first" ${atFirst ? "disabled" : ""} title="First page">«</button>
      <button type="button" class="page-btn" data-act="prev" ${atFirst ? "disabled" : ""} title="Previous page">‹</button>
      <span class="page-goto">Page <input type="number" min="1" max="${totalPages}" value="${state.page}" /> of ${totalPages}</span>
      <button type="button" class="page-btn" data-act="next" ${atLast ? "disabled" : ""} title="Next page">›</button>
      <button type="button" class="page-btn" data-act="last" ${atLast ? "disabled" : ""} title="Last page">»</button>
    </div>
    <select class="page-size-select">
      <option value="25">Show 25</option>
      <option value="50">Show 50</option>
      <option value="100">Show 100</option>
      <option value="250">Show 250</option>
      <option value="500">Show 500</option>
    </select>`;

  container.querySelector('[data-act="first"]').addEventListener("click", () => { state.page = 1; onChange(); });
  container.querySelector('[data-act="prev"]').addEventListener("click", () => { state.page = Math.max(1, state.page - 1); onChange(); });
  container.querySelector('[data-act="next"]').addEventListener("click", () => { state.page = Math.min(totalPages, state.page + 1); onChange(); });
  container.querySelector('[data-act="last"]').addEventListener("click", () => { state.page = totalPages; onChange(); });
  container.querySelector(".page-goto input").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10) || 1;
    state.page = Math.min(totalPages, Math.max(1, v));
    onChange();
  });
  const sizeSelect = container.querySelector(".page-size-select");
  sizeSelect.value = String(state.pageSize);
  sizeSelect.addEventListener("change", (e) => {
    state.pageSize = parseInt(e.target.value, 10);
    state.page = 1;
    onChange();
  });
}

function locationText(r) {
  return [r.address_line1, r.city, r.state, r.zip_code].filter(Boolean).join(", ") || "—";
}

// Column config drives the header, the filters, the sort, and CSV export.
// type: "select" -> Excel-style checkbox autofilter over unique values
//       "text"   -> contains-text filter
//       "date"   -> from/to date range filter
//       null     -> not filterable / not sortable
const COLUMNS = [
  {
    key: "source", label: "Source", type: "select", sortable: true,
    getValue: (r) => SOURCE_LABELS[r.source] || r.source || "—",
  },
  {
    key: "company", label: "Company", type: "text", sortable: true,
    getValue: (r) => r.company_name || "—",
    getSearch: (r) => `${r.company_name || ""} ${r.store_name || ""}`,
    render: (r) => `${r.company_name || "—"}${r.store_name && r.store_name !== r.company_name ? `<br><span style="color:var(--muted);font-size:11px">${r.store_name}</span>` : ""}`,
  },
  {
    key: "event", label: "Event", type: "select", sortable: true,
    getValue: (r) => r.event_type_name || "—",
    render: (r) => `<span class="badge ${r.event_type_name || ""}">${r.event_type_name || "—"}</span>`,
  },
  {
    key: "status", label: "Status", type: "select", sortable: true,
    getValue: (r) => r.status_label || "—",
  },
  {
    key: "date", label: "Date", type: "text", sortable: true,
    getValue: (r) => r.event_date_raw || r.event_date || "—",
  },
  {
    key: "location", label: "Location", type: "select", sortable: true,
    getValue: (r) => r.state || "—",
    getSearch: (r) => locationText(r),
    render: (r) => locationText(r),
  },
  {
    key: "description", label: "Description", type: "text", sortable: false,
    getValue: (r) => r.comment || "—",
  },
  {
    key: "article", label: "Article", type: null, sortable: false,
    getValue: () => "",
    render: (r) => `<a class="link" href="${r.article_link}" target="_blank" rel="noopener">open ↗</a>`,
  },
  {
    key: "published", label: "Published", type: "date", sortable: true,
    getValue: (r) => r.published_date || "",
  },
  {
    // published_date's format varies by source (some are clean ISO
    // timestamps, some are strings like "August 27, 2026"), which breaks a
    // simple string date-range comparison for the messier ones. date_appended
    // is a real Postgres DATE column -- always YYYY-MM-DD, every source --
    // so it's the one to actually rely on for date-range filtering.
    key: "dateappended", label: "Date Added", type: "date", sortable: true,
    getValue: (r) => r.date_appended || "",
  },
  {
    key: "markdone", label: "Completion", type: "select", sortable: true,
    getValue: (r) => {
      const m = marksCache[markKey(r)];
      return (m && m.completion_status) ? m.completion_status : "Not started";
    },
  },
  {
    key: "assignedto", label: "Assigned to", type: "select", sortable: true,
    getValue: (r) => {
      const m = marksCache[markKey(r)];
      return m && m.assigned_to ? analystName(m.assigned_to) : "Unassigned";
    },
  },
];

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let message = `${url} -> ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.error) message = body.error;
    } catch (e) { /* body wasn't JSON */ }
    throw new Error(message);
  }
  return res.json();
}

function setStatus(text, ok) {
  const el = document.getElementById("statusLine");
  el.textContent = text;
  el.style.color = ok ? "#34d399" : "#f87171";
}

async function loadAnalysts() {
  analystsCache = await fetchJSON(`${API}/analysts`);
}

async function loadMarks() {
  const rows = await fetchJSON(`${API}/article_marks`);
  marksCache = {};
  rows.forEach((r) => { marksCache[r.article_key] = r; });
}

// There's no live push (websockets) between browser sessions -- assignment
// and status changes from other analysts only become visible on the next
// refresh. This keeps that window short: a periodic poll while the app is
// open, plus an immediate refresh when the tab regains focus (the moment
// someone's most likely to actually look at stale data), rather than only
// finding out a row was already taken when an action gets rejected.
let marksPollTimer = null;

async function refreshMarksAndRerender() {
  try {
    await loadMarks();
  } catch (e) {
    return; // transient failure -- next poll or focus event will retry
  }
  if (currentSource && currentSubTab === "extraction") {
    renderTableHead();
    applyFiltersAndRender();
  }
}

function startMarksPolling() {
  if (marksPollTimer) return;
  marksPollTimer = setInterval(refreshMarksAndRerender, 20000);
}

function renderBarList(container, entries, colorFn) {
  container.innerHTML = "";
  const max = Math.max(1, ...entries.map((e) => e[1]));
  entries.forEach(([label, count]) => {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `
      <div class="bar-label">${label}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(count / max) * 100}%;background:${colorFn(label)}"></div></div>
      <div class="bar-count">${count}</div>`;
    container.appendChild(row);
  });
}

const PIE_COLORS = ["#2563eb", "#b91c1c", "#059669", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#9333ea", "#6b7280"];

// CSS conic-gradient pie -- no chart library needed for one shape. entries:
// [label, count][], already sorted the way they should appear.
function renderPieChart(circleId, legendId, entries) {
  const circle = document.getElementById(circleId);
  const legend = document.getElementById(legendId);
  if (!circle || !legend) return;

  const total = entries.reduce((sum, [, c]) => sum + c, 0);
  legend.innerHTML = "";

  if (!total) {
    circle.style.background = "var(--panel-border)";
    legend.innerHTML = `<div class="pie-legend-row"><span class="pie-legend-label">No matching events</span></div>`;
    return;
  }

  let acc = 0;
  const stops = entries.map(([label, count], i) => {
    const color = PIE_COLORS[i % PIE_COLORS.length];
    const start = (acc / total) * 360;
    acc += count;
    const end = (acc / total) * 360;
    return `${color} ${start}deg ${end}deg`;
  });
  circle.style.background = `conic-gradient(${stops.join(", ")})`;

  entries.forEach(([label, count], i) => {
    const pct = Math.round((count / total) * 100);
    const row = document.createElement("div");
    row.className = "pie-legend-row";
    row.innerHTML = `<span class="pie-swatch" style="background:${PIE_COLORS[i % PIE_COLORS.length]}"></span>
      <span class="pie-legend-label">${label}</span>
      <span class="pie-legend-count">${count} (${pct}%)</span>`;
    legend.appendChild(row);
  });
}

// The dashboard's own analyst selector -- distinct from the grid's "Assigned
// to" column filter popover, but drives the exact same underlying filter
// state, so picking a name here filters the cards/charts/table AND the grid
// beneath them together, consistently.
function populateDashboardAnalystFilter() {
  const sel = document.getElementById("dashAnalystFilter");
  if (!sel) return;
  const current = sel.value;
  let opts = `<option value="">All analysts</option>`;
  analystsCache.forEach((a) => { opts += `<option value="${a.analyst_name}">${a.analyst_name}</option>`; });
  opts += `<option value="Unassigned">Unassigned</option>`;
  sel.innerHTML = opts;
  sel.value = current || "";
}

function applyDashboardAnalystFilter(chosenName) {
  const col = COLUMNS.find((c) => c.key === "assignedto");
  if (!chosenName) {
    delete filters.assignedto;
  } else {
    const allValues = new Set(currentRows.map((r) => col.getValue(r)));
    allValues.delete(chosenName);
    filters.assignedto = { exclude: allValues };
  }
  sourcePage.page = 1;
  renderTableHead();
  applyFiltersAndRender();
}

// Same underlying filters.dateappended state the grid's own "Date Added"
// column filter popover writes to -- reads directly from date_appended, a
// real DATE column set on every insert, so this is reliable regardless of
// how inconsistently published_date is formatted across sources.
function applyDashboardDateFilter() {
  const from = document.getElementById("dashDateFrom").value;
  const to = document.getElementById("dashDateTo").value;
  if (!from && !to) {
    delete filters.dateappended;
  } else {
    filters.dateappended = { from, to };
  }
  sourcePage.page = 1;
  renderTableHead();
  applyFiltersAndRender();
}

// Keeps the dashboard's own analyst/date controls showing whatever the
// underlying filter state actually is, even when it was changed via the
// grid's own column-filter popovers instead of these controls -- one
// filter state, reflected consistently everywhere it's shown.
function syncDashboardFilterControls() {
  const sel = document.getElementById("dashAnalystFilter");
  if (sel) {
    const f = filters.assignedto;
    if (!f || !f.exclude || !f.exclude.size) {
      sel.value = "";
    } else {
      const col = COLUMNS.find((c) => c.key === "assignedto");
      const allValues = new Set(currentRows.map((r) => col.getValue(r)));
      const included = [...allValues].filter((v) => !f.exclude.has(v));
      sel.value = included.length === 1 ? included[0] : "";
    }
  }
  const fromInput = document.getElementById("dashDateFrom");
  const toInput = document.getElementById("dashDateTo");
  if (fromInput && toInput) {
    const f = filters.dateappended || {};
    fromInput.value = f.from || "";
    toInput.value = f.to || "";
  }
}

// Recomputed client-side from whatever rows are currently filtered (not a
// fixed server-side aggregate) -- this is what makes the dashboard's cards/
// charts/analyst-activity table actually react to the grid's filters
// instead of always showing lifetime totals. Called from
// applyFiltersAndRender() whenever the dashboard/all-events view is active.
function renderDashboardWidgets(rows) {
  const cards = document.getElementById("cards");
  cards.innerHTML = "";

  const uniqueCompanies = new Set(rows.map((r) => r.company_name).filter(Boolean)).size;
  const unassigned = rows.filter((r) => {
    const m = marksCache[markKey(r)];
    return !(m && m.assigned_to);
  }).length;
  const completed = rows.filter((r) => {
    const m = marksCache[markKey(r)];
    return m && m.is_done;
  }).length;

  const cardDefs = [
    ["Events (filtered)", rows.length],
    ["Unique companies", uniqueCompanies],
    ["Unassigned", unassigned],
    ["Completed", completed],
  ];
  cardDefs.forEach(([label, value]) => {
    const c = document.createElement("div");
    c.className = "card";
    c.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
    cards.appendChild(c);
  });

  const byType = {};
  const bySource = {};
  rows.forEach((r) => {
    const t = r.event_type_name || "Unspecified";
    byType[t] = (byType[t] || 0) + 1;
    const s = SOURCE_LABELS[r.source] || r.source || "Unspecified";
    bySource[s] = (bySource[s] || 0) + 1;
  });

  renderBarList(document.getElementById("typeChart"), Object.entries(byType), (label) => TYPE_COLORS[label] || "#5b8cff");
  renderBarList(document.getElementById("sourceChart"), Object.entries(bySource), () => "#2563eb");

  const byAnalystWorkload = {};
  rows.forEach((r) => {
    const m = marksCache[markKey(r)];
    const name = m && m.assigned_to ? analystName(m.assigned_to) : "Unassigned";
    byAnalystWorkload[name] = (byAnalystWorkload[name] || 0) + 1;
  });
  const workloadEntries = Object.entries(byAnalystWorkload).sort((a, b) => b[1] - a[1]);
  renderPieChart("analystPieChart", "analystPieLegend", workloadEntries);

  const body = document.getElementById("analystActivityBody");
  body.innerHTML = "";
  analystsCache.forEach((a) => {
    let entered = 0, completedCount = 0, assignedOpen = 0;
    rows.forEach((r) => {
      if (r.entered_by === a.analyst_id) entered++;
      const m = marksCache[markKey(r)];
      if (m) {
        if (m.is_done && m.marked_by === a.analyst_id) completedCount++;
        if (m.assigned_to === a.analyst_id && !m.is_done) assignedOpen++;
      }
    });
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${a.analyst_name}<br><span style="color:var(--muted);font-size:11px">${a.analyst_id} — ${a.role}</span></td>
      <td>${entered}</td>
      <td>${completedCount}</td>
      <td>${assignedOpen}</td>`;
    body.appendChild(tr);
  });
}

async function setCompletionStatus(articleKey, companyName, status) {
  try {
    const mark = await fetchJSON(`${API}/article_marks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        article_key: articleKey,
        company_name: companyName,
        completion_status: status,
        actor_analyst_id: currentUser.analyst_id,
      }),
    });
    marksCache[mark.article_key] = mark;
    renderTableHead();
    applyFiltersAndRender();
  } catch (e) {
    // Refresh in case this got rejected because the article's assignment
    // changed since this page last loaded (server is authoritative).
    await loadMarks().catch(() => {});
    renderTableHead();
    applyFiltersAndRender();
    alert(`Could not update status: ${e.message}`);
  }
}

// Clear a completion status back to blank. Only an admin, or whichever
// analyst is currently assigned to the article, may do this (enforced
// server-side).
async function resetCompletionStatus(articleKey, companyName) {
  try {
    const mark = await fetchJSON(`${API}/article_marks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        article_key: articleKey,
        company_name: companyName,
        completion_status: "",
        actor_analyst_id: currentUser.analyst_id,
      }),
    });
    marksCache[mark.article_key] = mark;
    renderTableHead();
    applyFiltersAndRender();
  } catch (e) {
    await loadMarks().catch(() => {});
    renderTableHead();
    applyFiltersAndRender();
    alert(`Could not reset status: ${e.message}`);
  }
}

// assignedTo: an analyst_id to assign to, or null/"" to unassign.
// Admins may assign to anyone; analysts may only assign/unassign themselves
// (also enforced server-side in /api/article_assignments).
async function assignArticle(articleKey, companyName, assignedTo) {
  try {
    const mark = await fetchJSON(`${API}/article_assignments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        article_key: articleKey,
        company_name: companyName,
        assigned_to: assignedTo || null,
        actor_analyst_id: currentUser.analyst_id,
      }),
    });
    marksCache[mark.article_key] = mark;
    renderTableHead();
    applyFiltersAndRender();
  } catch (e) {
    // Someone else may have claimed this since the page last loaded (the
    // server is the source of truth and just rejected this write) --
    // refresh from it so the UI shows who actually has it now, instead of
    // leaving a stale dropdown that looks unclaimed.
    await loadMarks().catch(() => {});
    renderTableHead();
    applyFiltersAndRender();
    alert(`Could not update assignment: ${e.message}`);
  }
}

// Assign several selected rows at once. assignedTo: analyst_id, or null to unassign.
async function bulkAssign(assignedTo) {
  const rows = currentRows.filter((r) => selectedIds.has(r.event_id));
  if (!rows.length) return;

  const results = await Promise.allSettled(
    rows.map((r) =>
      fetchJSON(`${API}/article_assignments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          article_key: markKey(r),
          company_name: r.company_name,
          assigned_to: assignedTo || null,
          actor_analyst_id: currentUser.analyst_id,
        }),
      })
    )
  );

  let okCount = 0;
  let conflictCount = 0;
  let otherFailCount = 0;
  results.forEach((res) => {
    if (res.status === "fulfilled") {
      marksCache[res.value.article_key] = res.value;
      okCount++;
    } else if (/already assigned/i.test(res.reason && res.reason.message || "")) {
      conflictCount++;
    } else {
      otherFailCount++;
    }
  });

  // Refresh from the server regardless of outcome -- rows someone else
  // claimed (including the ones that just caused conflicts above) need
  // marksCache to reflect their real current assignee, not what this
  // client thought it was before the batch ran.
  await loadMarks().catch(() => {});

  selectedIds.clear();
  renderTableHead();
  applyFiltersAndRender();
  updateBulkBar();

  if (conflictCount || otherFailCount) {
    const parts = [`Assigned ${okCount} article(s).`];
    if (conflictCount) parts.push(`${conflictCount} already claimed by someone else.`);
    if (otherFailCount) parts.push(`${otherFailCount} failed for another reason.`);
    alert(parts.join(" "));
  }
}

function updateBulkBar() {
  const bar = document.getElementById("bulkBar");
  if (!bar) return;
  if (!selectedIds.size || !currentUser) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "flex";
  document.getElementById("bulkCount").textContent = `${selectedIds.size} selected`;

  const actions = document.getElementById("bulkActions");
  actions.innerHTML = "";

  if (currentUser.role === "admin") {
    const select = document.createElement("select");
    select.className = "analyst-select";
    let opts = `<option value="">Assign selected to…</option><option value="__unassign__">— Unassign —</option>`;
    analystsCache.filter((a) => a.role === "analyst").forEach((a) => {
      opts += `<option value="${a.analyst_id}">${a.analyst_id} — ${a.analyst_name}</option>`;
    });
    select.innerHTML = opts;
    select.addEventListener("change", (e) => {
      const v = e.target.value;
      if (!v) return;
      bulkAssign(v === "__unassign__" ? null : v);
      select.value = "";
    });
    actions.appendChild(select);
  } else {
    const btn = document.createElement("button");
    btn.className = "tool-btn small primary";
    btn.textContent = "Assign selected to me";
    btn.addEventListener("click", () => bulkAssign(currentUser.analyst_id));
    actions.appendChild(btn);
  }
}

// ---- filtering / sorting engine ------------------------------------------

function resetTableState() {
  filters = {};
  globalSearchText = "";
  sortState = { key: null, dir: 1 };
  selectedIds.clear();
  selectionAnchorId = null;
  const search = document.getElementById("globalSearch");
  if (search) search.value = "";
  closePopover();
  const addMenu = document.getElementById("addMenu");
  if (addMenu) addMenu.style.display = "none";
  const columnsMenu = document.getElementById("columnsMenu");
  if (columnsMenu) columnsMenu.style.display = "none";
  updateBulkBar();
}

function isColFiltered(col) {
  const f = filters[col.key];
  if (!f) return false;
  if (col.type === "select") return f.exclude && f.exclude.size > 0;
  if (col.type === "text") return !!f.text;
  if (col.type === "date") return !!(f.from || f.to);
  return false;
}

function anyFilterActive() {
  return globalSearchText || COLUMNS.some(isColFiltered);
}

// Different sources write published_date in different shapes -- some clean
// ISO-ish timestamps, some plain strings like "August 29, 2026", some null.
// A naive first-10-characters slice only works for the ISO ones. This
// normalizes anything parseable to YYYY-MM-DD; date_appended is always
// already in that shape so the fast path below covers it too.
function toISODateOnly(value) {
  const s = (value || "").toString().trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const d = new Date(s);
  if (isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function rowMatchesFilters(r) {
  for (const col of COLUMNS) {
    const f = filters[col.key];
    if (!f) continue;
    if (col.type === "select" && f.exclude && f.exclude.size) {
      if (f.exclude.has(col.getValue(r))) return false;
    } else if (col.type === "text" && f.text) {
      const hay = (col.getSearch ? col.getSearch(r) : col.getValue(r)).toString().toLowerCase();
      if (!hay.includes(f.text.toLowerCase())) return false;
    } else if (col.type === "date" && (f.from || f.to)) {
      const raw = toISODateOnly(col.getValue(r));
      if (!raw) return false; // unparseable/missing -- can't confirm it's in range
      if (f.from && raw < f.from) return false;
      if (f.to && raw > f.to) return false;
    }
  }
  if (globalSearchText) {
    const needle = globalSearchText.toLowerCase();
    const hay = COLUMNS.map((c) => (c.getSearch ? c.getSearch(r) : c.getValue(r)) || "").join(" ").toLowerCase();
    if (!hay.includes(needle)) return false;
  }
  return true;
}

function getFilteredSortedRows() {
  let rows = currentRows.filter(rowMatchesFilters);
  if (sortState.key) {
    const col = COLUMNS.find((c) => c.key === sortState.key);
    if (col) {
      rows = rows.slice().sort((a, b) => {
        const av = (col.getValue(a) || "").toString();
        const bv = (col.getValue(b) || "").toString();
        return sortState.dir * av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" });
      });
    }
  }
  return rows;
}

// ---- popover (Excel-style autofilter dropdown) ---------------------------

function closePopover() {
  const existing = document.getElementById("activePopover");
  if (existing) existing.remove();
  document.removeEventListener("mousedown", onDocMouseDown, true);
}

function onDocMouseDown(e) {
  const pop = document.getElementById("activePopover");
  if (pop && !pop.contains(e.target) && !e.target.closest(".filter-icon") && !e.target.closest("#columnsBtn")) {
    closePopover();
  }
}

function openPopoverAt(anchorEl, contentEl) {
  closePopover();
  contentEl.id = "activePopover";
  contentEl.className = "popover";
  document.body.appendChild(contentEl);
  const rect = anchorEl.getBoundingClientRect();
  contentEl.style.position = "fixed";
  contentEl.style.top = `${rect.bottom + 4}px`;
  let left = rect.left;
  contentEl.style.left = `${left}px`;
  contentEl.style.display = "block";
  // keep on-screen
  requestAnimationFrame(() => {
    const w = contentEl.offsetWidth;
    if (left + w > window.innerWidth - 10) {
      contentEl.style.left = `${Math.max(10, window.innerWidth - w - 10)}px`;
    }
  });
  setTimeout(() => document.addEventListener("mousedown", onDocMouseDown, true), 0);
}

function openSelectFilter(col, anchorEl) {
  const counts = new Map();
  currentRows.forEach((r) => {
    const v = col.getValue(r);
    counts.set(v, (counts.get(v) || 0) + 1);
  });
  const values = Array.from(counts.keys()).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const excluded = (filters[col.key] && filters[col.key].exclude) || new Set();

  const box = document.createElement("div");
  box.innerHTML = `
    <div class="popover-search"><input type="text" placeholder="Search values…" class="pf-search" /></div>
    <div class="popover-actions">
      <button class="mini-link" data-act="all">Select all</button>
      <button class="mini-link" data-act="none">Clear</button>
    </div>
    <div class="popover-list"></div>
    <div class="popover-footer">
      <button class="tool-btn small" data-act="apply">Apply</button>
    </div>`;

  const listEl = box.querySelector(".popover-list");
  function renderList(filterText) {
    listEl.innerHTML = "";
    values
      .filter((v) => !filterText || v.toLowerCase().includes(filterText.toLowerCase()))
      .forEach((v) => {
        const id = `pf_${col.key}_${v}`.replace(/\W+/g, "_");
        const row = document.createElement("label");
        row.className = "popover-row";
        row.innerHTML = `<input type="checkbox" id="${id}" ${excluded.has(v) ? "" : "checked"} />
          <span>${v}</span><span class="pf-count">${counts.get(v)}</span>`;
        row.querySelector("input").addEventListener("change", (e) => {
          if (e.target.checked) excluded.delete(v);
          else excluded.add(v);
        });
        listEl.appendChild(row);
      });
  }
  renderList("");

  box.querySelector(".pf-search").addEventListener("input", (e) => renderList(e.target.value));
  box.querySelector('[data-act="all"]').addEventListener("click", () => { excluded.clear(); renderList(box.querySelector(".pf-search").value); });
  box.querySelector('[data-act="none"]').addEventListener("click", () => { values.forEach((v) => excluded.add(v)); renderList(box.querySelector(".pf-search").value); });
  box.querySelector('[data-act="apply"]').addEventListener("click", () => {
    filters[col.key] = { exclude: excluded };
    sourcePage.page = 1;
    closePopover();
    renderTableHead();
    applyFiltersAndRender();
  });

  openPopoverAt(anchorEl, box);
}

function openTextFilter(col, anchorEl) {
  const current = (filters[col.key] && filters[col.key].text) || "";
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="popover-search">
      <input type="text" class="pf-text" placeholder="Contains…" value="${current.replace(/"/g, "&quot;")}" />
    </div>
    <div class="popover-footer">
      <button class="mini-link" data-act="clear">Clear</button>
      <button class="tool-btn small" data-act="apply">Apply</button>
    </div>`;
  const input = box.querySelector(".pf-text");
  const commit = (val) => { filters[col.key] = { text: val }; sourcePage.page = 1; closePopover(); renderTableHead(); applyFiltersAndRender(); };
  box.querySelector('[data-act="apply"]').addEventListener("click", () => commit(input.value));
  box.querySelector('[data-act="clear"]').addEventListener("click", () => commit(""));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(input.value); });
  openPopoverAt(anchorEl, box);
  input.focus();
}

function openDateFilter(col, anchorEl) {
  const f = filters[col.key] || {};
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="popover-row-plain">From <input type="date" class="pf-from" value="${f.from || ""}" /></div>
    <div class="popover-row-plain">To <input type="date" class="pf-to" value="${f.to || ""}" /></div>
    <div class="popover-footer">
      <button class="mini-link" data-act="clear">Clear</button>
      <button class="tool-btn small" data-act="apply">Apply</button>
    </div>`;
  const commit = (from, to) => { filters[col.key] = { from, to }; sourcePage.page = 1; closePopover(); renderTableHead(); applyFiltersAndRender(); };
  box.querySelector('[data-act="apply"]').addEventListener("click", () => {
    commit(box.querySelector(".pf-from").value, box.querySelector(".pf-to").value);
  });
  box.querySelector('[data-act="clear"]').addEventListener("click", () => commit("", ""));
  openPopoverAt(anchorEl, box);
}

function openColumnFilter(col, anchorEl) {
  if (col.type === "select") openSelectFilter(col, anchorEl);
  else if (col.type === "text") openTextFilter(col, anchorEl);
  else if (col.type === "date") openDateFilter(col, anchorEl);
}

function openColumnsMenu() {
  const menu = document.getElementById("columnsMenu");
  menu.innerHTML = "";
  COLUMNS.forEach((col) => {
    const row = document.createElement("label");
    row.className = "popover-row";
    row.innerHTML = `<input type="checkbox" ${hiddenColumns.has(col.key) ? "" : "checked"} /><span>${col.label}</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) hiddenColumns.delete(col.key);
      else hiddenColumns.add(col.key);
      renderTableHead();
      applyFiltersAndRender();
    });
    menu.appendChild(row);
  });
  const isOpen = menu.style.display === "block";
  menu.style.display = isOpen ? "none" : "block";
}

// ---- rendering ------------------------------------------------------------

function renderSourceColGroup(visibleCols) {
  const cg = document.getElementById("sourceColGroup");
  let html = `<col style="width:34px">`;
  visibleCols.forEach((col) => {
    let w = colWidth(sourceColWidths, SOURCE_DEFAULT_WIDTHS, col.key);
    if (sourceWrap && CLIP_COLUMN_KEYS.has(col.key)) w += WRAP_WIDTH_BUMP;
    html += `<col data-colkey="${col.key}" style="width:${w}px">`;
  });
  html += `<col data-colkey="__assign" style="width:${colWidth(sourceColWidths, SOURCE_DEFAULT_WIDTHS, "__assign")}px">`;
  html += `<col data-colkey="__action" style="width:${colWidth(sourceColWidths, SOURCE_DEFAULT_WIDTHS, "__action")}px">`;
  cg.innerHTML = html;
}

function renderTableHead() {
  const visibleCols = COLUMNS.filter((c) => !hiddenColumns.has(c.key));
  renderSourceColGroup(visibleCols);

  const pill = document.getElementById("colCountPill");
  if (pill) pill.textContent = `${visibleCols.length}/${COLUMNS.length}`;

  const thead = document.getElementById("sourceTableHead");
  const tr = document.createElement("tr");

  const selectTh = document.createElement("th");
  const selectAllCb = document.createElement("input");
  selectAllCb.type = "checkbox";
  selectAllCb.id = "selectAllCb";
  selectAllCb.addEventListener("change", (e) => {
    const rows = getFilteredSortedRows();
    rows.forEach((r) => {
      if (e.target.checked) selectedIds.add(r.event_id);
      else selectedIds.delete(r.event_id);
    });
    applyFiltersAndRender();
    updateBulkBar();
  });
  selectTh.appendChild(selectAllCb);
  tr.appendChild(selectTh);

  visibleCols.forEach((col) => {
    const th = document.createElement("th");
    th.innerHTML = `<span class="th-inner">
        ${typeIcon(col.key, col.type)}
        <span class="th-label">${col.label}</span>
        ${col.sortable ? `<button class="sort-btn" data-key="${col.key}" title="Sort">${sortState.key === col.key ? (sortState.dir === 1 ? "▲" : "▼") : "⇅"}</button>` : ""}
        ${col.type ? `<button class="filter-icon ${isColFiltered(col) ? "active" : ""}" data-key="${col.key}" title="Filter">▾</button>` : ""}
      </span>`;
    if (col.sortable) {
      th.querySelector(".sort-btn").addEventListener("click", () => {
        sortState = sortState.key === col.key ? { key: col.key, dir: -sortState.dir } : { key: col.key, dir: 1 };
        sourcePage.page = 1;
        renderTableHead();
        applyFiltersAndRender();
      });
    }
    if (col.type) {
      th.querySelector(".filter-icon").addEventListener("click", (e) => openColumnFilter(col, e.currentTarget));
    }
    attachColResize(th, col.key, "sourceColGroup", sourceColWidths, "colWidths_source_v1");
    tr.appendChild(th);
  });

  const assignTh = document.createElement("th");
  assignTh.textContent = "Assign";
  attachColResize(assignTh, "__assign", "sourceColGroup", sourceColWidths, "colWidths_source_v1");
  tr.appendChild(assignTh);

  const actionTh = document.createElement("th");
  actionTh.textContent = "Action";
  attachColResize(actionTh, "__action", "sourceColGroup", sourceColWidths, "colWidths_source_v1");
  tr.appendChild(actionTh);

  thead.innerHTML = "";
  thead.appendChild(tr);
}

function buildAssignCell(r, existingMark) {
  const td = document.createElement("td");
  const assignedTo = existingMark ? existingMark.assigned_to : null;

  if (currentUser.role === "admin") {
    const select = document.createElement("select");
    select.className = "analyst-select";
    let opts = `<option value="">— Unassigned —</option>`;
    analystsCache.filter((a) => a.role === "analyst").forEach((a) => {
      opts += `<option value="${a.analyst_id}" ${a.analyst_id === assignedTo ? "selected" : ""}>${a.analyst_id} — ${a.analyst_name}</option>`;
    });
    select.innerHTML = opts;
    select.addEventListener("change", (e) => assignArticle(markKey(r), r.company_name, e.target.value));
    td.appendChild(select);
    return td;
  }

  if (!assignedTo) {
    const btn = document.createElement("button");
    btn.className = "tool-btn small";
    btn.textContent = "Assign to me";
    btn.addEventListener("click", () => assignArticle(markKey(r), r.company_name, currentUser.analyst_id));
    td.appendChild(btn);
  } else if (assignedTo === currentUser.analyst_id) {
    const wrap = document.createElement("span");
    wrap.className = "assign-self";
    wrap.textContent = "You ";
    const btn = document.createElement("button");
    btn.className = "mini-link";
    btn.textContent = "(unassign)";
    btn.addEventListener("click", () => assignArticle(markKey(r), r.company_name, null));
    wrap.appendChild(btn);
    td.appendChild(wrap);
  } else {
    const span = document.createElement("span");
    span.className = "assign-readonly";
    span.textContent = analystName(assignedTo);
    td.appendChild(span);
  }
  return td;
}

function buildSelectCell(r) {
  const td = document.createElement("td");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = selectedIds.has(r.event_id);
  cb.addEventListener("change", (e) => {
    if (e.target.checked) selectedIds.add(r.event_id);
    else selectedIds.delete(r.event_id);
    selectionAnchorId = r.event_id;
    updateBulkBar();
    const selectAllCb = document.getElementById("selectAllCb");
    if (selectAllCb) {
      const rows = getFilteredSortedRows();
      const allSelected = rows.length > 0 && rows.every((row) => selectedIds.has(row.event_id));
      selectAllCb.checked = allSelected;
      selectAllCb.indeterminate = !allSelected && rows.some((row) => selectedIds.has(row.event_id));
    }
  });
  td.appendChild(cb);
  return td;
}

function buildStatusCell(r, existingMark) {
  const td = document.createElement("td");
  const currentStatus = existingMark ? existingMark.completion_status : null;

  const select = document.createElement("select");
  select.className = "analyst-select status-select";
  select.dataset.status = currentStatus || "";
  let opts = `<option value="">— Select status —</option>`;
  COMPLETION_STATUSES.forEach((s) => {
    opts += `<option value="${s}" ${s === currentStatus ? "selected" : ""}>${s}</option>`;
  });
  select.innerHTML = opts;

  // Only an admin, or whichever analyst is currently assigned to this
  // article, may set or clear its status (enforced server-side too — this
  // just avoids showing an editable control that would get rejected).
  const assignedTo = existingMark ? existingMark.assigned_to : null;
  const canEditStatus = currentUser.role === "admin" || assignedTo === currentUser.analyst_id;
  if (!canEditStatus) {
    select.disabled = true;
    select.title = assignedTo
      ? `Only ${analystName(assignedTo)} or an admin can update this article's status`
      : "This article must be assigned before its status can be updated";
  }

  select.addEventListener("change", (e) => {
    const value = e.target.value;
    if (!value) resetCompletionStatus(markKey(r), r.company_name);
    else setCompletionStatus(markKey(r), r.company_name, value);
  });
  td.appendChild(select);

  if (currentStatus) {
    const meta = document.createElement("div");
    meta.className = "status-meta";
    meta.textContent = `${analystName(existingMark.marked_by)}`;
    if (canEditStatus) {
      const uncheckBtn = document.createElement("button");
      uncheckBtn.className = "mini-link";
      uncheckBtn.textContent = "uncheck";
      uncheckBtn.addEventListener("click", () => resetCompletionStatus(markKey(r), r.company_name));
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(uncheckBtn);
    }
    td.appendChild(meta);
  }
  return td;
}

// Columns whose values are always short/single-line — clipped with ellipsis
// so a resized-narrow column behaves like the reference grid. Company and
// Description can run long and stay as normal wrapping text instead.
const CLIP_COLUMN_KEYS = new Set(["source", "company", "event", "status", "date", "location", "description", "published", "dateappended", "markdone", "assignedto"]);

function applyFiltersAndRender() {
  const allRows = getFilteredSortedRows();
  const body = document.getElementById("sourceTableBody");
  const empty = document.getElementById("sourceEmpty");
  const rowCount = document.getElementById("rowCount");
  const visibleCols = COLUMNS.filter((c) => !hiddenColumns.has(c.key));

  rowCount.textContent = anyFilterActive()
    ? `${allRows.length} of ${currentRows.length} events match`
    : `${currentRows.length} events`;
  document.getElementById("clearFiltersBtn").classList.toggle("active", anyFilterActive());

  // The dashboard's cards/charts/analyst-activity reflect every currently
  // filtered row (not just the visible page), so they stay in sync with
  // whatever the grid's column filters are doing.
  if (currentSource === DASHBOARD_SOURCE) {
    renderDashboardWidgets(allRows);
    syncDashboardFilterControls();
  }

  const totalPages = Math.max(1, Math.ceil(allRows.length / sourcePage.pageSize));
  if (sourcePage.page > totalPages) sourcePage.page = totalPages;
  const start = (sourcePage.page - 1) * sourcePage.pageSize;
  const rows = allRows.slice(start, start + sourcePage.pageSize);

  renderPaginationBar("sourcePagination", sourcePage, allRows.length, applyFiltersAndRender);

  body.innerHTML = "";
  if (!rows.length) {
    const selectAllCb = document.getElementById("selectAllCb");
    if (selectAllCb) { selectAllCb.checked = false; selectAllCb.indeterminate = false; }
    empty.style.display = "block";
    empty.textContent = currentRows.length ? "No events match the current filters." : "No extracted events for this source yet.";
    return;
  }
  empty.style.display = "none";

  rows.forEach((r, index) => {
    const tr = document.createElement("tr");
    const existingMark = marksCache[markKey(r)];
    if (existingMark && existingMark.is_done) tr.classList.add("marked-done");
    if (selectedIds.has(r.event_id)) tr.classList.add("row-selected");

    tr.appendChild(buildSelectCell(r));

    visibleCols.forEach((col) => {
      const td = document.createElement("td");
      if (CLIP_COLUMN_KEYS.has(col.key)) td.classList.add("cell-clip");
      if (col.cellStyle) td.setAttribute("style", col.cellStyle);
      td.innerHTML = col.render ? col.render(r) : col.getValue(r);
      tr.appendChild(td);
    });

    tr.appendChild(buildAssignCell(r, existingMark));
    tr.appendChild(buildStatusCell(r, existingMark));

    // Ctrl/Cmd+click toggles one row; Shift+click selects the range from the
    // last-clicked row. Clicks on real controls (links, selects, buttons, the
    // checkbox itself) fall through to their normal behavior untouched.
    tr.addEventListener("mousedown", (e) => {
      if (e.shiftKey) e.preventDefault(); // stop the browser's native text-selection drag
    });
    tr.addEventListener("click", (e) => {
      if (e.target.closest("input, select, button, a")) return;
      if (e.ctrlKey || e.metaKey) {
        if (selectedIds.has(r.event_id)) selectedIds.delete(r.event_id);
        else selectedIds.add(r.event_id);
        selectionAnchorId = r.event_id;
        applyFiltersAndRender();
        updateBulkBar();
      } else if (e.shiftKey) {
        const anchorIndex = rows.findIndex((row) => row.event_id === selectionAnchorId);
        const [start, end] = anchorIndex === -1
          ? [index, index]
          : anchorIndex < index ? [anchorIndex, index] : [index, anchorIndex];
        for (let i = start; i <= end; i++) selectedIds.add(rows[i].event_id);
        selectionAnchorId = r.event_id;
        applyFiltersAndRender();
        updateBulkBar();
      }
    });

    body.appendChild(tr);
  });

  const selectAllCb = document.getElementById("selectAllCb");
  if (selectAllCb) {
    const allSelected = rows.length > 0 && rows.every((r) => selectedIds.has(r.event_id));
    selectAllCb.checked = allSelected;
    selectAllCb.indeterminate = !allSelected && rows.some((r) => selectedIds.has(r.event_id));
  }
}

function exportCSV() {
  const rows = getFilteredSortedRows();
  const visibleCols = COLUMNS.filter((c) => !hiddenColumns.has(c.key));
  const esc = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const lines = [visibleCols.map((c) => esc(c.label)).join(",")];
  rows.forEach((r) => {
    lines.push(visibleCols.map((c) => esc(c.getValue(r))).join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${currentSource || "export"}_events.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---- CSV import (upload + quick URL add) ----------------------------------

// Minimal RFC4180-ish CSV parser: handles quoted fields, embedded commas,
// escaped quotes ("") and CRLF/LF line endings.
function parseCsvLines(text) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  const s = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inQuotes) {
      if (c === '"') {
        if (s[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      row.push(field); field = "";
    } else if (c === "\n") {
      row.push(field); field = "";
      rows.push(row); row = [];
    } else {
      field += c;
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

const CSV_FIELD_MAP = {
  company: "company_name", companyname: "company_name",
  articlelink: "article_link", link: "article_link", url: "article_link",
  event: "event_type", eventtype: "event_type",
  status: "status",
  date: "event_date", eventdate: "event_date",
  location: "location", city: "location",
};

function parseCsvToRows(text) {
  const lines = parseCsvLines(text);
  if (!lines.length) return [];
  const header = lines[0].map((h) => h.trim().toLowerCase().replace(/[\s_]+/g, ""));
  const cols = header.map((h) => CSV_FIELD_MAP[h] || null);
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const row = {};
    cols.forEach((key, idx) => {
      if (key && lines[i][idx] != null) row[key] = lines[i][idx].trim();
    });
    if (row.article_link) rows.push(row);
  }
  return rows;
}

async function submitBulkRows(rows) {
  const result = await fetchJSON(`${API}/store_events/bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source: currentSource,
      actor_analyst_id: currentUser.analyst_id,
      rows,
    }),
  });
  await loadSourceTable(currentSource);
  return result;
}

// ---- Articles sub-tab (raw scraped_articles, read-only) -------------------
// Deliberately lighter than the Extraction table: a global search and simple
// click-to-sort, no per-column filter popovers — this is a reference view of
// what was scraped, not a workflow surface.
let currentSubTab = "extraction";
let articleRows = [];
let articlesSearchText = "";
let articlesSortState = { key: null, dir: 1 };

const ARTICLE_COLUMNS = [
  {
    key: "title", label: "Title",
    // WARN notices don't have an article title — fall back to something
    // readable built from their extra_data instead of a bare "—".
    getValue: (a) => a.title || (a.extra_data && a.extra_data.closure_type ? `WARN Notice — ${a.extra_data.closure_type}` : "—"),
  },
  { key: "company", label: "Company", getValue: (a) => a.company_name || "—" },
  { key: "summary", label: "Summary", getValue: (a) => a.summary || "—" },
  {
    key: "location", label: "Location",
    getValue: (a) => [a.city, a.state].filter(Boolean).join(", ") || "—",
  },
  { key: "published", label: "Published", getValue: (a) => a.published_date || "—" },
  {
    key: "employees", label: "Employees Affected",
    getValue: (a) => (a.extra_data && a.extra_data.employees_affected) || "—",
  },
  {
    key: "layoffdate", label: "Layoff Date",
    getValue: (a) => (a.extra_data && a.extra_data.layoff_date) || "—",
  },
  {
    key: "closuretype", label: "Closure Type",
    getValue: (a) => (a.extra_data && a.extra_data.closure_type) || "—",
  },
  {
    key: "link", label: "Link",
    getValue: (a) => a.link || "",
    render: (a) => (a.link ? `<a class="link" href="${a.link}" target="_blank" rel="noopener">open ↗</a>` : "—"),
  },
];

function renderArticlesColGroup() {
  const cg = document.getElementById("articlesColGroup");
  if (!cg) return;
  cg.innerHTML = ARTICLE_COLUMNS.map((col) => {
    let w = colWidth(articlesColWidths, ARTICLE_DEFAULT_WIDTHS, col.key);
    if (articlesWrap && ARTICLE_CLIP_COLUMN_KEYS.has(col.key)) w += WRAP_WIDTH_BUMP;
    return `<col data-colkey="${col.key}" style="width:${w}px">`;
  }).join("");
}

function renderArticlesTableHead() {
  renderArticlesColGroup();
  const thead = document.getElementById("articlesTableHead");
  const tr = document.createElement("tr");
  ARTICLE_COLUMNS.forEach((col) => {
    const th = document.createElement("th");
    th.innerHTML = `<span class="th-inner">
        ${typeIcon(col.key, col.type)}
        <span class="th-label">${col.label}</span>
        <button class="sort-btn" title="Sort">${articlesSortState.key === col.key ? (articlesSortState.dir === 1 ? "▲" : "▼") : "⇅"}</button>
      </span>`;
    th.querySelector(".sort-btn").addEventListener("click", () => {
      articlesSortState = articlesSortState.key === col.key
        ? { key: col.key, dir: -articlesSortState.dir }
        : { key: col.key, dir: 1 };
      articlesPage.page = 1;
      renderArticlesTableHead();
      renderArticlesTableBody();
    });
    attachColResize(th, col.key, "articlesColGroup", articlesColWidths, "colWidths_articles_v1");
    tr.appendChild(th);
  });
  thead.innerHTML = "";
  thead.appendChild(tr);
}

const ARTICLE_CLIP_COLUMN_KEYS = new Set(["title", "company", "summary", "location", "published", "employees", "layoffdate", "closuretype"]);

function renderArticlesTableBody() {
  const body = document.getElementById("articlesTableBody");
  const empty = document.getElementById("articlesEmpty");
  const rowCount = document.getElementById("articlesRowCount");

  let allRows = articleRows;
  if (articlesSearchText) {
    const needle = articlesSearchText.toLowerCase();
    allRows = allRows.filter((a) =>
      ARTICLE_COLUMNS.map((c) => c.getValue(a) || "").join(" ").toLowerCase().includes(needle)
    );
  }
  if (articlesSortState.key) {
    const col = ARTICLE_COLUMNS.find((c) => c.key === articlesSortState.key);
    allRows = allRows.slice().sort((a, b) =>
      articlesSortState.dir * (col.getValue(a) || "").toString().localeCompare(
        (col.getValue(b) || "").toString(), undefined, { numeric: true, sensitivity: "base" }
      )
    );
  }

  rowCount.textContent = articlesSearchText
    ? `${allRows.length} of ${articleRows.length} articles match`
    : `${articleRows.length} articles`;

  const totalPages = Math.max(1, Math.ceil(allRows.length / articlesPage.pageSize));
  if (articlesPage.page > totalPages) articlesPage.page = totalPages;
  const start = (articlesPage.page - 1) * articlesPage.pageSize;
  const rows = allRows.slice(start, start + articlesPage.pageSize);

  renderPaginationBar("articlesPagination", articlesPage, allRows.length, renderArticlesTableBody);

  body.innerHTML = "";
  if (!rows.length) {
    empty.style.display = "block";
    empty.textContent = articleRows.length ? "No articles match your search." : "No raw articles scraped for this source yet.";
    return;
  }
  empty.style.display = "none";

  rows.forEach((a) => {
    const tr = document.createElement("tr");
    ARTICLE_COLUMNS.forEach((col) => {
      const td = document.createElement("td");
      if (ARTICLE_CLIP_COLUMN_KEYS.has(col.key)) td.classList.add("cell-clip");
      if (col.cellStyle) td.setAttribute("style", col.cellStyle);
      td.innerHTML = col.render ? col.render(a) : col.getValue(a);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

async function loadArticlesTable(source) {
  articleRows = await fetchJSON(`${API}/scraped_articles?source=${encodeURIComponent(source)}`);
  articlesSearchText = "";
  articlesSortState = { key: null, dir: 1 };
  articlesPage.page = 1;
  const search = document.getElementById("articlesSearch");
  if (search) search.value = "";
  renderArticlesTableHead();
  renderArticlesTableBody();
}

function showSubTab(subtab) {
  currentSubTab = subtab;
  document.querySelectorAll(".subtab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.subtab === subtab);
  });
  document.getElementById("pane-extraction").style.display = subtab === "extraction" ? "block" : "none";
  document.getElementById("pane-articles").style.display = subtab === "articles" ? "block" : "none";

  if (subtab === "articles") {
    const articlesSource = ARTICLES_SOURCE_OVERRIDE[currentSource] || currentSource;
    loadArticlesTable(articlesSource).catch((e) => setStatus(`error: ${e.message}`, false));
  }
}

async function loadSourceTable(source) {
  currentSource = source;
  const isDashboard = source === DASHBOARD_SOURCE;

  document.getElementById("sourceTitle").textContent = isDashboard
    ? "All Events — every source, filtered below"
    : `${SOURCE_LABELS[source] || source} — extracted events`;

  // The dashboard's summary widgets only make sense above the unified
  // "every source" grid; the Articles/Extraction toggle and "+Add articles"
  // (which needs one specific source to post to) don't apply there.
  document.getElementById("dashboardWidgets").style.display = isDashboard ? "block" : "none";
  document.getElementById("subtabs").style.display = isDashboard ? "none" : "inline-flex";
  const addMenuWrap = document.getElementById("addMenuWrap");
  if (addMenuWrap) addMenuWrap.style.display = isDashboard ? "none" : "";
  if (isDashboard) {
    populateDashboardAnalystFilter();
    document.getElementById("dashAnalystFilter").value = "";
  }

  const labels = SUBTAB_LABEL_OVERRIDE[source] || { extraction: "Extraction", articles: "Articles" };
  document.querySelector('.subtab-btn[data-subtab="extraction"]').textContent = labels.extraction;
  document.querySelector('.subtab-btn[data-subtab="articles"]').textContent = labels.articles;

  resetTableState();
  sourcePage.page = 1;
  // Refresh assignment/status state alongside the rows every time a tab is
  // opened, not just once at login -- otherwise switching to a tab someone
  // else has been actively working in shows stale "Assigned to" values.
  const fetchUrl = isDashboard ? `${API}/store_events` : `${API}/store_events?source=${encodeURIComponent(source)}`;
  const [rows] = await Promise.all([
    fetchJSON(fetchUrl),
    loadMarks().catch(() => {}),
  ]);
  currentRows = rows;
  renderTableHead();
  applyFiltersAndRender();
  showSubTab("extraction");
}

function showView(tab) {
  document.getElementById("view-source").style.display = tab !== "crawl" ? "block" : "none";
  document.getElementById("view-crawl").style.display = tab === "crawl" ? "block" : "none";

  document.querySelectorAll("nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });

  if (tab === "crawl") {
    // static form — nothing to preload
  } else {
    const source = tab === "dashboard" ? DASHBOARD_SOURCE : tab;
    loadSourceTable(source).catch((e) => setStatus(`error: ${e.message}`, false));
  }
}

// ---- Article Extractor (Crawl4AI) -----------------------------------------

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : s;
  return div.innerHTML;
}

function renderCrawlResults(results) {
  const container = document.getElementById("crawlResults");
  container.innerHTML = "";
  results.forEach((r) => {
    const card = document.createElement("div");
    card.className = "crawl-card";
    if (r.success) {
      card.innerHTML = `
        <div class="crawl-card-head">
          <a href="${r.url}" target="_blank" rel="noopener" class="link">${escapeHtml(r.title) || r.url}</a>
          <button type="button" class="mini-link crawl-copy-btn">Copy text</button>
        </div>
        <pre class="crawl-markdown">${escapeHtml(r.markdown) || "(no article content found on this page)"}</pre>`;
      card.querySelector(".crawl-copy-btn").addEventListener("click", (e) => {
        navigator.clipboard.writeText(r.markdown || "").then(() => {
          e.target.textContent = "Copied!";
          setTimeout(() => { e.target.textContent = "Copy text"; }, 1200);
        });
      });
    } else {
      card.innerHTML = `
        <div class="crawl-card-head">
          <a href="${r.url}" target="_blank" rel="noopener" class="link">${r.url}</a>
          <span class="crawl-error-tag">failed</span>
        </div>
        <div class="crawl-error-msg">${escapeHtml(r.error) || "Could not extract this article."}</div>`;
    }
    container.appendChild(card);
  });
}

async function submitCrawl(urls) {
  const statusEl = document.getElementById("crawlStatus");
  const resultsEl = document.getElementById("crawlResults");
  const btn = document.getElementById("crawlSubmitBtn");

  btn.disabled = true;
  btn.textContent = "Extracting…";
  statusEl.style.display = "block";
  statusEl.className = "crawl-status";
  statusEl.textContent = `Extracting ${urls.length} article${urls.length > 1 ? "s" : ""}… this can take up to a minute.`;
  resultsEl.innerHTML = "";

  try {
    const data = await fetchJSON(`${API}/crawl`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    statusEl.style.display = "none";
    renderCrawlResults(data.results);
  } catch (e) {
    statusEl.className = "crawl-status crawl-status-error";
    statusEl.textContent = `Extraction failed: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Extract";
  }
}

function renderUserBadge() {
  document.getElementById("userBadge").textContent =
    `${currentUser.analyst_name} · ${currentUser.role === "admin" ? "Admin" : "Analyst " + currentUser.analyst_id}`;
}

async function bootAfterLogin() {
  document.getElementById("loginScreen").style.display = "none";
  document.getElementById("app").style.display = "block";
  renderUserBadge();

  try {
    await loadAnalysts();
    await loadMarks();
    setStatus("connected to demo backend", true);
  } catch (e) {
    setStatus("backend not reachable — is app.py running on :5000?", false);
  }

  startMarksPolling();
  showView("dashboard");
}

async function doLogin(identifier, password) {
  const errorEl = document.getElementById("loginError");
  errorEl.style.display = "none";
  try {
    const user = await fetchJSON(`${API}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier, password }),
    });
    currentUser = user;
    storeUser(user);
    await bootAfterLogin();
  } catch (e) {
    errorEl.textContent = e.message === "invalid credentials"
      ? "Invalid analyst ID/email or password."
      : `Login failed: ${e.message}`;
    errorEl.style.display = "block";
  }
}

function doLogout() {
  clearStoredUser();
  currentUser = null;
  if (marksPollTimer) { clearInterval(marksPollTimer); marksPollTimer = null; }
  document.getElementById("app").style.display = "none";
  document.getElementById("loginScreen").style.display = "flex";
  document.getElementById("loginPassword").value = "";
}

async function init() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && currentUser) {
      refreshMarksAndRerender();
    }
  });

  document.querySelectorAll("nav button").forEach((b) => {
    b.addEventListener("click", () => showView(b.dataset.tab));
  });
  document.querySelectorAll(".subtab-btn").forEach((b) => {
    b.addEventListener("click", () => showSubTab(b.dataset.subtab));
  });
  document.getElementById("articlesSearch").addEventListener("input", (e) => {
    articlesSearchText = e.target.value.trim();
    articlesPage.page = 1;
    renderArticlesTableBody();
  });

  document.getElementById("globalSearch").addEventListener("input", (e) => {
    globalSearchText = e.target.value.trim();
    sourcePage.page = 1;
    applyFiltersAndRender();
  });
  document.getElementById("clearFiltersBtn").addEventListener("click", () => {
    resetTableState();
    sourcePage.page = 1;
    const dashSel = document.getElementById("dashAnalystFilter");
    if (dashSel) dashSel.value = "";
    renderTableHead();
    applyFiltersAndRender();
  });
  document.getElementById("dashAnalystFilter").addEventListener("change", (e) => {
    applyDashboardAnalystFilter(e.target.value);
  });
  document.getElementById("dashDateFrom").addEventListener("change", applyDashboardDateFilter);
  document.getElementById("dashDateTo").addEventListener("change", applyDashboardDateFilter);
  document.getElementById("dashDateClearBtn").addEventListener("click", () => {
    document.getElementById("dashDateFrom").value = "";
    document.getElementById("dashDateTo").value = "";
    applyDashboardDateFilter();
  });
  document.getElementById("exportCsvBtn").addEventListener("click", exportCSV);
  document.getElementById("sourceWrapBtn").addEventListener("click", toggleSourceWrap);
  document.getElementById("articlesWrapBtn").addEventListener("click", toggleArticlesWrap);
  document.getElementById("sourceFullscreenBtn").addEventListener("click", toggleFullscreen);
  document.getElementById("articlesFullscreenBtn").addEventListener("click", toggleFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && gridFullscreen) toggleFullscreen();
  });
  renderFullscreenButtons();
  document.getElementById("columnsBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    document.getElementById("addMenu").style.display = "none";
    openColumnsMenu();
  });
  document.getElementById("addMenuBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    document.getElementById("columnsMenu").style.display = "none";
    const menu = document.getElementById("addMenu");
    menu.style.display = menu.style.display === "block" ? "none" : "block";
  });
  document.getElementById("logoutBtn").addEventListener("click", doLogout);
  document.getElementById("bulkClearBtn").addEventListener("click", () => {
    selectedIds.clear();
    applyFiltersAndRender();
    updateBulkBar();
  });

  document.getElementById("quickAddForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("quickAddUrl");
    const url = input.value.trim();
    if (!url) return;
    try {
      const result = await submitBulkRows([{ article_link: url }]);
      if (result.inserted) {
        input.value = "";
      } else if (result.skipped_duplicate) {
        alert("That URL is already on this list.");
      } else {
        alert("Could not add that URL.");
      }
    } catch (err) {
      alert(`Could not add URL: ${err.message}`);
    }
  });

  document.getElementById("uploadCsvBtn").addEventListener("click", () => {
    document.getElementById("csvFileInput").click();
  });
  document.getElementById("csvFileInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const rows = parseCsvToRows(text);
      if (!rows.length) {
        alert("No usable rows found — make sure the CSV has an article_link column.");
        return;
      }
      const result = await submitBulkRows(rows);
      let msg = `Added ${result.inserted} row(s).`;
      if (result.skipped_duplicate) msg += ` ${result.skipped_duplicate} already existed.`;
      if (result.skipped_invalid) msg += ` ${result.skipped_invalid} skipped (missing article link).`;
      alert(msg);
    } catch (err) {
      alert(`CSV upload failed: ${err.message}`);
    } finally {
      e.target.value = "";
    }
  });
  document.getElementById("crawlForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const raw = document.getElementById("crawlUrls").value;
    const urls = raw.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) return;
    submitCrawl(urls);
  });

  document.getElementById("loginForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const identifier = document.getElementById("loginIdentifier").value.trim();
    const password = document.getElementById("loginPassword").value;
    doLogin(identifier, password);
  });

  const stored = loadStoredUser();
  if (stored) {
    currentUser = stored;
    await bootAfterLogin();
  }
}

init();
