function uuid() {
  return Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}
const $ = (s) => document.querySelector(s),
  esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const rs = (v) =>
  "₹" +
  Number(v || 0).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
let config,
  role,
  page = "billing",
  pending = 0,
  cart = [],
  catalog = [],
  categories = [],
  customer = null,
  editing = null,
  quote = null,
  busy = false,
  requestKey = uuid(),
  searchSequence = 0;
let draft = {
  tax_mode: "exclusive",
  interstate: false,
  bill_date: today(),
  payment_mode: "Cash",
  paid_now: null,
  customer: {},
  metadata: {},
};
function toast(message, bad = false) {
  $("#toast").textContent = message;
  $("#toast").className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("#toast").classList.add("hidden"), 6000);
}
const pendingMutationKeys = new Map();
async function api(
  path,
  { method = "GET", body, key, raw = false, signal } = {},
) {
  const identity = method + path + (raw ? "upload" : JSON.stringify(body));
  if (method !== "GET" && !key) {
    key = pendingMutationKeys.get(identity) || uuid();
    pendingMutationKeys.set(identity, key);
  }
  pending++;
  $("#loading").classList.remove("hidden");
  $("#content").setAttribute("aria-busy", "true");
  try {
    const headers = { "X-Billing-Request": "1" };
    if (body && !raw) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    const r = await fetch("/api" + path, {
      method,
      headers,
      body: body ? (raw ? body : JSON.stringify(body)) : undefined,
      signal: signal || AbortSignal.timeout(45000),
    });
    if (r.status === 401) {
      showLogin();
      throw Error(
        "Your session expired. Sign in again; your draft is preserved.",
      );
    }
    if (!r.ok) {
      let j;
      try {
        j = await r.json();
      } catch {}
      throw Error(
        r.status === 404 && j?.detail === "Not Found"
          ? "This backend does not provide the requested screen. Restart the updated host; an older server may still be running."
          : typeof j?.detail === "string"
            ? j.detail
            : `Request failed (${r.status}). Check the input and try again.`,
      );
    }
    $("#connection").textContent = "● Connected";
    const result = await r.json();
    pendingMutationKeys.delete(identity);
    return result;
  } catch (e) {
    if (e.name === "TimeoutError")
      throw Error(
        "The server response timed out. Retry this same action; do not create a second bill.",
      );
    if (e instanceof TypeError) {
      $("#connection").textContent = "● Offline";
      throw Error(
        "Cannot reach the shop server. Your draft is preserved. Reconnect and retry.",
      );
    }
    throw e;
  } finally {
    if (--pending === 0) {
      $("#loading").classList.add("hidden");
      $("#content").setAttribute("aria-busy", "false");
    }
  }
}
const activeActions = new WeakSet();
function action(fn) {
  return async (e) => {
    const btn = e?.currentTarget;
    if (btn?.disabled || (btn && activeActions.has(btn))) return;
    if (btn) activeActions.add(btn);
    if (btn?.tagName === "BUTTON") btn.disabled = true;
    try {
      await fn(e);
    } catch (err) {
      toast(err.message, true);
    } finally {
      if (btn) activeActions.delete(btn);
      if (btn?.tagName === "BUTTON") btn.disabled = false;
    }
  };
}
function on(selector, event, fn) {
  $(selector)?.addEventListener(event, action(fn));
}
function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(
      () => Promise.resolve(fn(...args)).catch((e) => toast(e.message, true)),
      ms,
    );
  };
}
function persist() {
  try {
    sessionStorage.setItem(
      "billing-draft",
      JSON.stringify({ draft, cart, customer, editing, requestKey }),
    );
  } catch {}
}
function restore() {
  try {
    const data = JSON.parse(sessionStorage.getItem("billing-draft"));
    if (data) {
      ({ draft, cart, customer, editing, requestKey } = data);
    }
  } catch {}
}
function formField(label, name, value = "", type = "text", extra = "") {
  return `<label>${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
}
function values(form) {
  return Object.fromEntries(new FormData(form));
}
function modal(title, html) {
  $("#dialog-body").onclick = null;
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = html;
  if (!$("#dialog").open) $("#dialog").showModal();
}
$("#dialog-close").onclick = () => $("#dialog").close();
$("#role").onchange = () =>
  $("#password-label").classList.toggle("hidden", $("#role").value !== "admin");
function showLogin(target = "employee") {
  persist();
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#role").value = target;
  $("#role").onchange();
  $("#password").value = "";
  $("#login-error").textContent = "";
}
$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  btn.disabled = true;
  try {
    const result = await api("/login", {
      method: "POST",
      body: { role: $("#role").value, password: $("#password").value },
    });
    role = result.role;
    $("#password").value = "";
    if (serverNeedsRestart) {
      await requestRestart();
      return;
    }
    await start();
  } catch (err) {
    $("#login-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
on("#logout", "click", async () => {
  await api("/logout", { method: "POST" });
  showLogin();
});
on("#switch-role", "click", async () => {
  if (role === "admin") {
    await api("/login", { method: "POST", body: { role: "employee" } });
    role = "employee";
    editing = null;
    cart = [];
    quote = null;
    requestKey = uuid();
    draft.tax_mode = config.tax_mode;
    draft.interstate = false;
    draft.bill_date = today();
    if (!editing) draft.paid_now = null;
    persist();
    await start();
  } else showLogin("admin");
});
$("#nav").onclick = action(async (e) => {
  const target = e.target.closest("[data-page]");
  if (target) await navigate(target.dataset.page);
});
async function start() {
  $("#shell").classList.remove("hidden");
  $("#login").classList.add("hidden");
  $(".brand span").textContent = config.company.name;
  $("#role-badge").textContent = role.toUpperCase() + " WORKSPACE";
  $("#switch-role").textContent =
    role === "admin" ? "Switch to employee" : "Switch to admin";
  document
    .querySelectorAll(".admin")
    .forEach((e) => e.classList.toggle("hidden", role !== "admin"));
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  if (
    role !== "admin" &&
    (editing ||
      cart.some((x) => x.discount || x.rate !== x.original_rate) ||
      draft.tax_mode !== config.tax_mode ||
      draft.interstate)
  ) {
    cart = [];
    editing = null;
    draft.tax_mode = config.tax_mode;
    draft.interstate = false;
    if (!editing) draft.paid_now = null;
    requestKey = uuid();
    persist();
  }
  categories = await api("/categories");
  if (!$("#connect-device")) {
    const button = document.createElement("button");
    button.id = "connect-device";
    button.textContent = "Local server / connect device";
    $(".header-actions").append(button);
    on("#connect-device", "click", localConnection);
  }
  await navigate("billing");
}
const reportStates = {};
function metricCards(values) {
  return (
    '<div class="metrics">' +
    values
      .map(
        ([label, value]) =>
          `<div class="card"><p class="eyebrow">${esc(label)}</p><div class="metric">${esc(value)}</div></div>`,
      )
      .join("") +
    "</div>"
  );
}
function periodControls(id, initial = "30days") {
  const s = (reportStates[id] ||= {
    period: initial,
    month: today().slice(0, 7),
    year: today().slice(0, 4),
  });
  return `<div class="toolbar period-controls"><label>Period<select id="${id}-period">${[
    ["all", "All time / till date"],
    ["today", "Today"],
    ["7days", "Last 7 days"],
    ["30days", "Last 30 days"],
    ["month", "Choose month"],
    ["lastmonth", "Last month"],
    ["3months", "Last 3 full months"],
    ["year", "Choose year"],
    ["custom", "Custom dates"],
  ]
    .map(
      ([v, l]) =>
        `<option value="${v}" ${s.period === v ? "selected" : ""}>${l}</option>`,
    )
    .join(
      "",
    )}</select></label><label>Month<input id="${id}-month" type="month" value="${esc(s.month || today().slice(0, 7))}"></label><label>Year<input id="${id}-year" type="number" min="1900" max="9998" value="${esc(s.year || today().slice(0, 4))}"></label><label>From<input id="${id}-from" type="date" value="${esc(s.date_from || "")}"></label><label>To<input id="${id}-to" type="date" value="${esc(s.date_to || "")}"></label><button id="${id}-apply">Apply</button></div>`;
}
function periodParams(id) {
  const s = {
    period: $("#" + id + "-period").value,
    month: $("#" + id + "-month").value,
    year: $("#" + id + "-year").value,
    date_from: $("#" + id + "-from").value,
    date_to: $("#" + id + "-to").value,
  };
  reportStates[id] = { ...reportStates[id], ...s };
  return new URLSearchParams(s);
}
function bindPeriod(id, refresh) {
  const changed = () =>
    Promise.resolve(refresh()).catch((e) => toast(e.message, true));
  on("#" + id + "-apply", "click", refresh);
  $("#" + id + "-period").onchange = changed;
  for (const [field, choice] of [
    ["month", "month"],
    ["year", "year"],
    ["from", "custom"],
    ["to", "custom"],
  ]) {
    $("#" + id + "-" + field).onchange = () => {
      $("#" + id + "-period").value = choice;
      return changed();
    };
  }
}
function showBounds(id, data) {
  $("#" + id + "-from").value = data.date_from || "";
  $("#" + id + "-to").value = data.date_to || "";
}
function pager(id) {
  return `<div class="actions"><button id="${id}-prev">Previous</button><span id="${id}-count"></span><button id="${id}-next">Next</button></div>`;
}
function pageInfo(id, data) {
  $("#" + id + "-prev").disabled = data.offset === 0;
  $("#" + id + "-next").disabled =
    data.offset + data.rows.length >= data.summary.count;
  $("#" + id + "-count").textContent =
    `${data.summary.count ? data.offset + 1 : 0}–${data.offset + data.rows.length} of ${data.summary.count}`;
}
async function customerProfile(id) {
  const data = await api("/customers/" + id + "/profile"),
    c = data.customer,
    s = data.summary;
  modal(
    c.name + " · customer profile",
    `<p>${esc(c.phone || "No phone")} · ${esc(c.address || "No address")}</p>${metricCards(
      [
        ["Purchase visits", s.visits],
        ["Total billed", rs(s.total)],
        ["Received", rs(s.paid)],
        ["Outstanding", rs(s.outstanding)],
      ],
    )}<p>Last purchase: ${esc(s.last_visit || "No purchases yet")}</p><div class="actions"><button id="profile-sales">Sales & payments</button><button id="profile-wishes">Manage wishlist</button></div><h3>Repeated open requests</h3><p class="small muted">Grouped by matching text, ignoring case and surrounding spaces.</p>${table(
      ["Request", "Times requested"],
      data.demand.map((r) => [esc(r.request), r.requests]),
    )}`,
  );
  on("#profile-wishes", "click", () => wishes(c));
  on("#profile-sales", "click", async () => {
    reportStates.sales = { period: "all", customer_id: id, search: "" };
    $("#dialog").close();
    await navigate("sales");
  });
}
function verticalChart(series, granularity) {
  const max = Math.max(1, ...series.map((r) => r.revenue));
  return `<p class="small muted">Select a bar to open its sales. Amounts include GST. Empty dates show zero.</p><div class="chart-scroll"><div class="daily-chart">${series.map((r) => `<button class="chart-column" data-period-label="${esc(r.label)}" title="${esc(r.label)}: ${rs(r.revenue)} · ${r.bills} bills" aria-label="${esc(r.label)}: ${rs(r.revenue)}, ${r.bills} bills"><span class="chart-value">${rs(r.revenue)}</span><span class="chart-track"><span style="height:${(r.revenue / max) * 100}%"></span></span><span>${esc(granularity === "day" ? r.label.slice(5) : r.label)}</span></button>`).join("")}</div></div><details><summary>Show exact sales values</summary>${table(
    [granularity === "day" ? "Date" : "Month", "Bills", "Sales"],
    series.map((r) => [esc(r.label), r.bills, rs(r.revenue)]),
  )}</details>`;
}
function horizontalChart(rows, field) {
  const max = Math.max(1, ...rows.map((r) => r[field]));
  return rows.length
    ? rows
        .map(
          (r) =>
            `<div class="bar-row"><span>${esc(r.name)}</span><div class="bar"><span style="width:${(r[field] / max) * 100}%"></span></div><b>${field === "revenue" ? rs(r[field]) : r[field]}</b></div>`,
        )
        .join("")
    : '<p class="muted">No sales in this period.</p>';
}
async function localConnection() {
  const s = await api("/connection");
  modal(
    "Local shop server",
    `<p><b>Server running</b></p><p>Connect the other device to the same Wi-Fi, then scan or open this address:</p><p><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a></p><img src="/api/connection/qr" class="connection-qr" alt="QR to open the shop on another device"><div class="actions"><button id="copy-shop-url">Copy address</button>${role === "admin" ? '<button id="restart-shop-server">Restart backend</button>' : ""}</div><p class="small muted">This QR opens the shop; invoice QR codes receive UPI payments. The host must stay awake. Closing this desktop leaves the server running.</p>${s.local_only ? '<p class="notice">This host is bound to localhost. Set BILLING_HOST=0.0.0.0 and restart to allow Wi-Fi clients.</p>' : ""}<p class="small">Start the host with startserver.bat or python -m billing --run-server. If another device cannot connect, allow the configured port on the private-network firewall. You can set BILLING_PUBLIC_URL for your host address.</p>`,
  );
  on("#restart-shop-server", "click", requestRestart);
  on("#copy-shop-url", "click", async () => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(s.url);
      toast("Address copied");
    } else {
      prompt("Copy this shop address:", s.url);
    }
  });
}

let serverNeedsRestart = false;
function incompatibleServer(message) {
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-form").classList.add("hidden");
  let panel = $("#startup-problem");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "startup-problem";
    $("#login .login-card").append(panel);
  }
  panel.innerHTML = `<h2>Backend update required</h2><p class="notice">${esc(message)}</p><p>On the host, stop the old Python/Billing server (Ctrl+C in its terminal, or its --run-server process in Task Manager), then start this updated app. Use the desktop's Start / reconnect server control.</p><button id="retry-startup">Check again</button><p>Your records have not been reset.</p>`;
  $("#retry-startup").onclick = () => location.reload();
}
async function reconnectBackend(result) {
  persist();
  modal(
    "Restarting backend",
    '<p>Finishing active work and reconnecting. Your committed records are preserved.</p><p id="restart-status">Waiting for the updated host…</p>',
  );
  const destination = new URL(result.public_url || location.href);
  if (!result.public_url) destination.port = String(result.port);
  // Do not poll another origin with session credentials after a port/host change.
  if (destination.origin !== location.origin) {
    $("#dialog-body").innerHTML =
      `<p>Settings saved. The host is restarting at its new address.</p><a href="${esc(destination.href)}">Open the configured shop address</a><p>On the desktop, Start / reconnect reads the new local configuration.</p>`;
    return;
  }
  for (let n = 0; n < 65; n++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const r = await fetch("/api/health", {
        cache: "no-store",
        signal: AbortSignal.timeout(1500),
      });
      const h = await r.json();
      if (
        h.api_version === 3 &&
        !h.restarting &&
        !h.stale_files &&
        h.instance_id !== result.previous_instance
      ) {
        location.reload();
        return;
      }
    } catch {}
  }
  $("#restart-status").textContent =
    "The host has not returned yet. Use the desktop Start / reconnect control, or start python -m billing --run-server on the host. Open logs for the failure details.";
}
async function requestRestart() {
  if (
    !confirm(
      "Restart the backend for all connected devices? Active requests will finish first.",
    )
  )
    return;
  const result = await api("/server/restart", { method: "POST", body: {} });
  await reconnectBackend(result);
}
async function configuration() {
  const data = await api("/admin/configuration");
  const groups = [
    [
      "Company and invoice",
      [
        ["COMPANY_NAME", "Company name"],
        ["COMPANY_ADDRESS", "Address"],
        ["COMPANY_PHONE", "Contact phone"],
        ["COMPANY_EMAIL", "Email"],
        ["COMPANY_GSTIN", "GSTIN"],
        ["INVOICE_TERMS", "Invoice terms"],
      ],
    ],
    [
      "Bank and payment QR",
      [
        ["BANK_NAME", "Bank name"],
        ["BANK_ACCOUNT", "Bank account"],
        ["BANK_IFSC", "IFSC"],
        ["BANK_BRANCH", "Bank branch"],
        ["UPI_ID", "Receiving UPI ID"],
        ["UPI_PHONE", "UPI-linked phone (reference only)"],
      ],
    ],
    [
      "Billing defaults",
      [
        ["DEFAULT_TAX_MODE", "GST pricing"],
        ["DEFAULT_GST_RATE", "Default GST rate"],
      ],
    ],
    [
      "Optional Meta WhatsApp",
      [
        ["WHATSAPP_PROVIDER", "Provider"],
        ["WHATSAPP_PHONE_NUMBER_ID", "Phone-number ID"],
        ["WHATSAPP_API_VERSION", "Graph API version"],
        ["WHATSAPP_BILL_TEMPLATE", "Approved bill template"],
        ["WHATSAPP_LANGUAGE", "Template language"],
      ],
    ],
    [
      "Local server and backups",
      [
        ["BILLING_HOST", "Network access"],
        ["BILLING_PORT", "Server port"],
        ["BILLING_PUBLIC_URL", "Advertised URL (optional)"],
        ["BACKUP_KEEP", "Automatic backups to retain"],
        ["BACKUP_INTERVAL_SECONDS", "Backup interval (seconds)"],
      ],
    ],
  ];
  const choices = {
    DEFAULT_TAX_MODE: [
      ["exclusive", "GST added"],
      ["inclusive", "GST included"],
      ["none", "Without GST"],
    ],
    WHATSAPP_PROVIDER: [
      ["disabled", "Disabled"],
      ["meta", "Meta Cloud API"],
    ],
    BILLING_HOST: [
      ["0.0.0.0", "Other devices on Wi-Fi"],
      ["127.0.0.1", "This computer only"],
    ],
  };
  const field = (key, label) => {
    const locked = data.locked.includes(key),
      extra = locked ? "disabled" : "";
    if (choices[key])
      return `<label>${esc(label)}<select name="${key}" ${extra}>${choices[key].map(([v, l]) => `<option value="${v}" ${data.values[key] === v ? "selected" : ""}>${l}</option>`).join("")}</select>${locked ? "<small>Controlled by environment</small>" : ""}</label>`;
    return formField(
      label + (locked ? " (environment override)" : ""),
      key,
      data.values[key],
      "text",
      extra,
    );
  };
  $("#content").innerHTML =
    `<form id="settings-form" class="stack"><section class="card"><h3>Configuration</h3><p>Edit setup details here. Saving restarts the backend for connected devices. Existing invoice snapshots and database records remain intact; updated company/payment details apply to new bills.</p><p class="small">Database: <code>${esc(data.database)}</code><br>Config file: <code>${esc(data.config_file)}</code><br>Host build: ${esc(data.build)}</p></section>${groups.map(([name, fields]) => `<section class="card"><h3>${name}</h3><div class="grid2">${fields.map(([k, l]) => field(k, l)).join("")}</div>${name === "Optional Meta WhatsApp" ? `<label>New API token (blank keeps existing)<input type="password" name="whatsapp_token" autocomplete="off" ${data.locked.includes("WHATSAPP_TOKEN") ? "disabled" : ""}></label><p class="small">Stored token: ${data.whatsapp_token_set ? "configured" : "not configured"}. The saved token is never displayed.</p><label class="check"><input type="checkbox" name="clear_whatsapp_token" ${data.locked.includes("WHATSAPP_TOKEN") ? "disabled" : ""}>Remove stored token</label>` : ""}</section>`).join("")}<section class="card"><h3>Change admin password (optional)</h3><div class="grid2">${formField("Current password", "current_password", "", "password", 'autocomplete="current-password"')}${formField("New password", "new_password", "", "password", 'autocomplete="new-password" minlength="8"')}${formField("Confirm new password", "confirm_password", "", "password", 'autocomplete="new-password"')}</div><p class="small">Changing the password signs existing sessions out after restart.</p><button class="primary" ${data.restart_supported ? "" : "disabled"}>Save & restart backend</button>${data.restart_supported ? "" : '<p class="notice">Restart controls need the managed launcher: python -m billing --run-server.</p>'}<p id="settings-error" class="error"></p></section></form>`;
  $("#settings-form").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target,
      button = e.submitter;
    button.disabled = true;
    try {
      const fields = values(form),
        settings = {};
      for (const key of Object.keys(data.values))
        if (!data.locked.includes(key)) settings[key] = fields[key];
      const result = await api("/admin/configuration", {
        method: "POST",
        body: {
          revision: data.revision,
          values: settings,
          whatsapp_token: fields.whatsapp_token || "",
          clear_whatsapp_token: form.elements.clear_whatsapp_token.checked,
          current_password: fields.current_password,
          new_password: fields.new_password,
          confirm_password: fields.confirm_password,
        },
      });
      form
        .querySelectorAll("input[type=password]")
        .forEach((e) => (e.value = ""));
      await reconnectBackend(result);
    } catch (err) {
      $("#settings-error").textContent = err.message;
      button.disabled = false;
    }
  };
}

const titles = {
  billing: "New bill",
  inventory: "Inventory",
  customers: "Customers",
  sales: "Sales & payments",
  balances: "Customer balances",
  expenses: "Expenses",
  stats: "Shop overview",
  messages: "WhatsApp updates",
  status: "Backup & activity",
  configuration: "Configuration",
};
async function navigate(name) {
  if (role !== "admin" && name !== "billing") return;
  page = name;
  $("#content").onclick = null;
  $("#page-title").textContent = titles[name];
  $("#switch-role").classList.toggle("hidden", name !== "billing");
  document
    .querySelectorAll("[data-page]")
    .forEach((e) => e.classList.toggle("active", e.dataset.page === name));
  await {
    billing,
    inventory,
    customers,
    sales,
    balances,
    expenses,
    stats,
    messages,
    status,
    configuration,
  }[name]();
}
function billPayload() {
  return {
    ...draft,
    customer_id: customer?.id,
    items: cart.map(
      ({ item_id, version, quantity, rate, discount, gst_rate }) => ({
        item_id,
        version,
        quantity,
        rate,
        discount,
        gst_rate,
      }),
    ),
    ...(editing ? { version: editing.version } : {}),
  };
}
async function updateQuote() {
  persist();
  if (!cart.length) {
    quote = null;
    renderCart();
    return;
  }
  const snapshot = JSON.stringify(billPayload());
  const result = await api("/bills/quote", {
    method: "POST",
    body: billPayload(),
  });
  if (snapshot !== JSON.stringify(billPayload())) return;
  quote = result;
  if (page === "billing") renderCart();
}
async function billing() {
  $("#content").innerHTML =
    `${editing ? `<div class="notice">Editing ${esc(editing.bill_no)}. Existing payments are preserved. A history entry will record this change.</div>` : ""}<div class="bill-grid"><div class="stack"><section class="card"><div class="row between"><h3>Customer <span class="muted small">optional</span></h3><button id="clear-customer" class="quiet small">Clear</button></div><label>Find customer<input id="customer-search" placeholder="Name or phone"></label><div id="customer-results"></div><div class="grid2">${formField("Name (optional)", "new-name", draft.customer.name || "")}${formField("Phone (optional)", "new-phone", draft.customer.phone || "")}</div>${formField("Address (optional)", "new-address", draft.customer.address || "")}<p id="customer-selected" class="small muted"></p></section><section class="card"><div class="row between"><h3>Add items</h3>${role === "admin" ? '<button id="quick-item" class="quiet">+ New item</button>' : ""}</div><label>Search or scan barcode<input id="catalog-search" autofocus placeholder="Scan barcode, or search item name"></label><p class="muted small">Employee prices come from the catalog. Select an item to add it.</p><div id="catalog" class="catalog"></div></section></div><section class="card"><div class="row between"><h3>Bill items</h3><span class="pill">${role === "admin" ? "Admin controls" : "Catalog prices"}</span></div><div id="cart"></div><div id="totals" class="summary"></div><div class="grid2"><label>GST pricing<select id="tax-mode" ${role !== "admin" ? "disabled" : ""}><option value="exclusive">GST added to price</option><option value="inclusive">GST included in price</option><option value="none">Without GST</option></select></label><label>Payment mode<select id="payment-mode"><option>Cash</option><option>UPI</option><option>Card</option><option>Other</option></select></label></div>${role === "admin" ? '<label class="check"><input id="interstate" type="checkbox">Interstate sale (IGST)</label>' : ""}<div class="grid2">${formField("Bill date", "bill-date", draft.bill_date, "date", role === "admin" ? "" : "disabled")}${formField(editing ? "Paid already (unchanged)" : "Paid now", "paid-now", draft.paid_now ?? "", "number", `min="0" step="0.01" ${editing ? "disabled" : ""}`)}</div><p class="small muted">If payment has not arrived, enter 0 in Paid now. The unpaid invoice will show the configured UPI payment QR.</p><details><summary>Additional invoice details (optional)</summary><div class="grid2">${[
      ["book_no", "Book number"],
      ["party_gstin", "Party GSTIN"],
      ["pin_code", "Pin code"],
      ["eway_no", "E-Way bill no."],
      ["eway_date", "E-Way date"],
      ["po_no", "P.O. number"],
      ["po_date", "P.O. date"],
      ["transport", "Transport"],
      ["lr_no", "L/R number"],
    ]
      .map(([key, label]) =>
        formField(label, "meta-" + key, draft.metadata[key] || ""),
      )
      .join(
        "",
      )}</div></details><div class="checkout"><button id="complete" class="primary wide">${editing ? "Save bill changes" : "Complete bill"}</button><button id="clear-bill" class="quiet wide small">${editing ? "Cancel edit" : "Clear this bill"}</button></div></section></div>`;
  $("#tax-mode").value = draft.tax_mode;
  $("#payment-mode").value = draft.payment_mode;
  if ($("#interstate")) $("#interstate").checked = draft.interstate;
  const updateCustomer = () => {
    draft.customer = {
      name: $("[name=new-name]").value,
      phone: $("[name=new-phone]").value,
      address: $("[name=new-address]").value,
    };
    persist();
  };
  ["new-name", "new-phone", "new-address"].forEach((n) =>
    on(`[name=${n}]`, "input", updateCustomer),
  );
  function selected() {
    const has = !!customer;
    $("#customer-selected").textContent = has
      ? `Using ${customer.name} · ${customer.visits ?? 0} purchase visits · Last: ${(customer.last_visit || "Never").slice(0, 10)}. Saved details will not be overwritten.`
      : "Leave details blank for a walk-in sale.";
    for (const [n, field] of [
      ["new-name", "name"],
      ["new-phone", "phone"],
      ["new-address", "address"],
    ]) {
      const input = $(`[name=${n}]`);
      input.disabled = has;
      if (has) input.value = customer[field] || "";
    }
  }
  selected();
  on("#clear-customer", "click", () => {
    customer = null;
    draft.customer = {};
    ["new-name", "new-phone", "new-address"].forEach(
      (n) => ($(`[name=${n}]`).value = ""),
    );
    selected();
    persist();
  });
  $("#customer-search").oninput = debounce(async () => {
    const query = $("#customer-search").value;
    const list = await api("/customers?search=" + encodeURIComponent(query));
    if (page !== "billing" || $("#customer-search").value !== query) return;
    $("#customer-results").innerHTML = list
      .slice(0, 8)
      .map(
        (c) =>
          `<button class="quiet small" data-cid="${c.id}">${esc(c.name)} · ${esc(c.phone || "No phone")}</button>`,
      )
      .join("");
    $("#customer-results").onclick = (e) => {
      const c = list.find((x) => x.id === Number(e.target.dataset.cid));
      if (c) {
        customer = c;
        selected();
        $("#customer-results").innerHTML = "";
        persist();
      }
    };
  });
  $("#catalog-search").oninput = debounce(() =>
    loadCatalog($("#catalog-search").value),
  );
  on("#catalog-search", "keydown", async (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      await loadCatalog($("#catalog-search").value);
      const exact = catalog.find(
        (x) => x.barcode === $("#catalog-search").value.trim(),
      );
      if (exact) {
        await addCart(exact);
        $("#catalog-search").value = "";
        await loadCatalog("");
      }
    }
  });
  on("#quick-item", "click", () => itemForm(null, async () => loadCatalog("")));
  on("#tax-mode", "change", async () => {
    draft.tax_mode = $("#tax-mode").value;
    if (!editing) draft.paid_now = null;
    await updateQuote();
  });
  on("#interstate", "change", async () => {
    draft.interstate = $("#interstate").checked;
    await updateQuote();
  });
  on("#payment-mode", "change", () => {
    draft.payment_mode = $("#payment-mode").value;
    persist();
  });
  on("[name=bill-date]", "change", () => {
    draft.bill_date = $("[name=bill-date]").value;
    persist();
  });
  on("[name=paid-now]", "change", () => {
    draft.paid_now = Number($("[name=paid-now]").value);
    persist();
    renderTotals();
  });
  document.querySelectorAll("[name^=meta-]").forEach(
    (input) =>
      (input.onchange = () => {
        draft.metadata[input.name.slice(5)] = input.value;
        persist();
      }),
  );
  on("#complete", "click", complete);
  on("#clear-bill", "click", async () => {
    if (confirm("Clear this draft? Saved bills will stay unchanged.")) {
      resetBill();
      await billing();
    }
  });
  await loadCatalog("");
  await updateQuote();
}
async function loadCatalog(search) {
  const seq = ++searchSequence;
  const rows = await api("/items?search=" + encodeURIComponent(search));
  if (page !== "billing" || seq !== searchSequence) return;
  catalog = rows;
  $("#catalog").innerHTML = rows.length
    ? rows
        .map(
          (i) =>
            `<div class="catalog-item"><div><b>${esc(i.name)}</b><p>${esc([i.category_name, i.size, i.color].filter(Boolean).join(" · "))} · Stock ${i.stock_qty}</p></div><button data-add="${i.id}">${rs(i.rate)} +</button></div>`,
        )
        .join("")
    : '<p class="empty">No items. An admin can add catalog items.</p>';
  $("#catalog").onclick = action(async (e) => {
    const item = rows.find((i) => i.id === Number(e.target.dataset.add));
    if (item) await addCart(item);
  });
}
async function addCart(item) {
  const existing = cart.find((x) => x.item_id === item.id);
  if (existing) {
    if (existing.version !== item.version)
      throw Error(
        "This item changed. Remove it from the draft and add it again.",
      );
    existing.quantity++;
  } else
    cart.push({
      item_id: item.id,
      name: item.name,
      version: item.version,
      quantity: 1,
      rate: item.rate,
      original_rate: item.rate,
      gst_rate: item.gst_rate,
      discount: 0,
    });
  if (!editing) draft.paid_now = null;
  await updateQuote();
}
function renderCart() {
  if (page !== "billing") return;
  $("#cart").innerHTML = cart.length
    ? `<div class="table-scroll"><table class="cart"><thead><tr><th>Item</th><th>Qty</th><th>Rate</th>${role === "admin" ? "<th>Discount</th>" : ""}<th></th></tr></thead><tbody>${cart.map((r, i) => `<tr><td class="item-name">${esc(r.name)}</td><td><input aria-label="Quantity for ${esc(r.name)}" data-i="${i}" data-field="quantity" type="number" min="1" step="1" value="${r.quantity}"></td><td>${role === "admin" ? `<input aria-label="Rate for ${esc(r.name)}" data-i="${i}" data-field="rate" type="number" min="0" step=".01" value="${r.rate}">` : rs(r.rate)}</td>${role === "admin" ? `<td><input aria-label="Discount for ${esc(r.name)}" data-i="${i}" data-field="discount" type="number" min="0" step=".01" value="${r.discount}"></td>` : ""}<td><button data-remove="${i}" class="quiet" aria-label="Remove ${esc(r.name)}">×</button></td></tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">Your bill starts here.<br><span class="small">Scan or select an item to get started.</span></div>';
  $("#cart").onchange = action(async (e) => {
    const { i, field } = e.target.dataset;
    if (field) {
      cart[Number(i)][field] = Number(e.target.value);
      if (!editing) draft.paid_now = null;
      await updateQuote();
    }
  });
  $("#cart").onclick = action(async (e) => {
    const i = e.target.dataset.remove;
    if (i !== undefined) {
      cart.splice(Number(i), 1);
      if (!editing) draft.paid_now = null;
      await updateQuote();
    }
  });
  renderTotals();
}
function renderTotals() {
  if (page !== "billing") return;
  const q = quote || {};
  $("#totals").innerHTML =
    [
      ["Subtotal", q.subtotal],
      ["Discount", q.discount_amount],
      ["Taxable amount", q.taxable_amount],
      ["GST", q.gst_amount],
    ]
      .map(
        ([label, v]) =>
          `<div class="row"><span class="muted">${label}</span><b>${rs(v)}</b></div>`,
      )
      .join("") +
    `<div class="row grand"><span>Total</span><span>${rs(q.total)}</span></div><div class="row"><span>Outstanding</span><b>${rs(Math.max(0, (q.total || 0) - (draft.paid_now ?? q.total ?? 0)))}</b></div>`;
  if (draft.paid_now === null)
    $("[name=paid-now]").value = (q.total || 0).toFixed(2);
  $("#complete").disabled = !cart.length || busy;
}
function resetBill() {
  cart = [];
  customer = null;
  quote = null;
  editing = null;
  requestKey = uuid();
  draft = {
    tax_mode: config.tax_mode,
    interstate: false,
    bill_date: today(),
    payment_mode: "Cash",
    paid_now: null,
    customer: {},
    metadata: {},
  };
  persist();
}
async function complete() {
  if (busy || !cart.length) return;
  busy = true;
  $("#shell").inert = true;
  renderTotals();
  try {
    // The last display quote may still be in flight when checkout is clicked.
    if (draft.paid_now === null) {
      const finalQuote = await api("/bills/quote", {
        method: "POST",
        body: billPayload(),
      });
      draft.paid_now = finalQuote.total;
    }
    persist();
    const result = await api(editing ? "/bills/" + editing.id : "/bills", {
      method: editing ? "PUT" : "POST",
      body: billPayload(),
      key: requestKey,
    });
    resetBill();
    toast("Bill " + result.bill_no + " saved");
    try {
      await billing();
      await receipt(result.bill_id);
    } catch {
      modal(
        "Bill saved",
        `<p>Bill ${esc(result.bill_no)} was saved successfully. The receipt could not load. Reconnect and open it below.</p><button id="retry-receipt" class="primary">Open saved receipt</button>`,
      );
      on("#retry-receipt", "click", () => receipt(result.bill_id));
    }
  } finally {
    busy = false;
    $("#shell").inert = false;
    if (page === "billing" && $("#totals")) renderTotals();
  }
}
async function receipt(id) {
  const { bill } = await api("/bills/" + id);
  modal(
    bill.bill_no,
    `<p>Paid ${rs(bill.paid_amount)} · Outstanding <b>${rs(bill.balance)}</b></p><iframe title="Invoice preview" class="invoice-frame" src="/api/bills/${id}/invoice"></iframe><div class="actions"><a target="_blank" rel="noopener" href="/api/bills/${id}/invoice">Open / print</a><a href="/api/bills/${id}/pdf" download>Download PDF</a><button id="share-pdf">Share PDF</button>${config.whatsapp ? '<button id="send-whatsapp" class="primary">Send WhatsApp</button>' : ""}</div><p class="small muted">Sharing needs the customer’s phone and permission. The PDF contains the saved bill, not your current draft.</p>`,
  );
  on("#share-pdf", "click", async () => {
    const res = await fetch("/api/bills/" + id + "/pdf");
    if (!res.ok) throw Error("Could not download invoice");
    const file = new File([await res.blob()], bill.bill_no + ".pdf", {
      type: "application/pdf",
    });
    if (navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title: bill.bill_no });
    } else {
      toast("Download the PDF, then attach it in WhatsApp.");
      const phone = (bill.customer_phone || "").replace(/\D/g, "");
      window.open(
        "https://wa.me/" +
          phone +
          "?text=" +
          encodeURIComponent(
            "Your invoice " + bill.bill_no + " from " + config.company.name,
          ),
        "_blank",
        "noopener",
      );
    }
  });
  const sendKey = uuid();
  on("#send-whatsapp", "click", async () => {
    await api("/bills/" + id + "/whatsapp", {
      method: "POST",
      body: {},
      key: sendKey,
    });
    toast("Invoice queued. Check WhatsApp updates for provider status.");
    $("#send-whatsapp").disabled = true;
  });
}
async function itemForm(item, done) {
  modal(
    item ? "Edit item" : "New catalog item",
    `<form id="item-form"><div class="grid2">${formField("Item name", "name", item?.name || "", "text", "required")}<label>Category<select name="category_id">${categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></label><label>Brand / style (optional)<select name="subtype_id"><option value="">None</option></select></label>${formField("Barcode (optional)", "barcode", item?.barcode || "")}${formField("Size (optional)", "size", item?.size || "")}${formField("Colour (optional)", "color", item?.color || "")}${formField("Price", "rate", item?.rate ?? 0, "number", 'min="0" step=".01" required')}${formField("Stock recorded", "stock_qty", item?.stock_qty ?? 0, "number", 'min="0" step="1"')}${formField("GST rate %", "gst_rate", item?.gst_rate ?? config.gst_rate, "number", 'min="0" max="100" step=".01"')}${formField("HSN (optional)", "hsn", item?.hsn || "")}</div><p class="muted small">Unknown stock can stay at zero. A sale is not blocked by the recorded stock count.</p><div class="actions"><button type="submit" class="primary">Save item</button></div></form>`,
  );
  const f = $("#item-form");
  if (item) f.elements.category_id.value = item.category_id;
  async function brands() {
    const subs = await api(
      "/subtypes?category_id=" + f.elements.category_id.value,
    );
    f.elements.subtype_id.innerHTML =
      '<option value="">None</option>' +
      subs
        .map((s) => `<option value="${s.id}">${esc(s.name)}</option>`)
        .join("");
    if (item) f.elements.subtype_id.value = item.subtype_id || "";
  }
  await brands();
  f.elements.category_id.onchange = action(brands);
  f.onsubmit = action(async (e) => {
    e.preventDefault();
    const data = values(f);
    ["category_id", "rate", "stock_qty", "gst_rate"].forEach(
      (k) => (data[k] = Number(data[k])),
    );
    data.subtype_id = data.subtype_id ? Number(data.subtype_id) : null;
    if (item) {
      data.version = item.version;
      data.expected_stock = item.stock_qty;
    }
    await api("/items" + (item ? "/" + item.id : ""), {
      method: item ? "PUT" : "POST",
      body: data,
    });
    $("#dialog").close();
    await done();
    toast("Item saved");
  });
}
async function inventory() {
  $("#content").innerHTML =
    '<section class="card"><div class="toolbar"><label>Search inventory<input id="inv-search" placeholder="Item name or barcode"></label><button id="add-category">Categories / brands</button><button id="add-item" class="primary">+ Add item</button></div><div id="inv-list"></div><div class="actions"><button id="inv-prev">Previous</button><button id="inv-next">Next</button></div></section>';
  let offset = 0;
  async function refresh() {
    const query = $("#inv-search").value;
    const list = await api(
      "/items?search=" + encodeURIComponent(query) + "&offset=" + offset,
    );
    if (page !== "inventory" || query !== $("#inv-search").value) return;
    $("#inv-list").innerHTML = table(
      ["Item", "Category", "Price", "GST", "Stock", "Actions"],
      list.map((i) => [
        esc(i.name),
        esc(i.category_name),
        rs(i.rate),
        i.gst_rate + "%",
        i.stock_qty,
        `<button data-edit="${i.id}">Edit</button> <button class="danger" data-delete="${i.id}">Archive</button>`,
      ]),
    );
    $("#inv-prev").disabled = offset === 0;
    $("#inv-next").disabled = list.length < 100;
    $("#inv-list").onclick = action(async (e) => {
      const i = list.find(
        (r) =>
          r.id === Number(e.target.dataset.edit || e.target.dataset.delete),
      );
      if (!i) return;
      if (e.target.dataset.edit) await itemForm(i, refresh);
      else if (
        confirm("Archive this catalog item? Existing invoices will be kept.")
      ) {
        await api("/items/" + i.id, {
          method: "DELETE",
          body: { version: i.version },
        });
        await refresh();
      }
    });
  }
  $("#inv-search").oninput = debounce(() => {
    offset = 0;
    return refresh();
  });
  on("#inv-prev", "click", () => {
    offset = Math.max(0, offset - 100);
    return refresh();
  });
  on("#inv-next", "click", () => {
    offset += 100;
    return refresh();
  });
  on("#add-item", "click", () => itemForm(null, refresh));
  on("#add-category", "click", categoryForm);
  await refresh();
}
async function categoryForm() {
  modal(
    "Categories and brands",
    `<form id="category-form">${formField("New category name", "name", "", "text", "required")}<button class="primary">Add category</button></form><hr><form id="brand-form"><label>Category<select name="category_id">${categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></label>${formField("New brand / style", "name", "", "text", "required")}<button class="primary">Add brand</button></form><hr><div id="category-manage">${categories.map((c) => `<p>${esc(c.name)} <button data-rename="${c.id}">Rename</button> <button class="danger" data-remove="${c.id}">Delete</button></p>`).join("")}</div>`,
  );
  $("#category-form").onsubmit = action(async (e) => {
    e.preventDefault();
    await api("/categories", { method: "POST", body: values(e.target) });
    categories = await api("/categories");
    await categoryForm();
  });
  $("#brand-form").onsubmit = action(async (e) => {
    e.preventDefault();
    const body = values(e.target);
    body.category_id = Number(body.category_id);
    await api("/subtypes", { method: "POST", body });
    toast("Brand added");
    e.target.elements.name.value = "";
  });
  $("#category-manage").onclick = action(async (e) => {
    const id = Number(e.target.dataset.rename || e.target.dataset.remove);
    if (!id) return;
    const cat = categories.find((c) => c.id === id);
    if (e.target.dataset.rename) {
      const name = prompt("New category name", cat.name);
      if (!name) return;
      await api("/categories/" + id, { method: "PUT", body: { name } });
    } else {
      if (!confirm("Delete this category? It must have no inventory items."))
        return;
      await api("/categories/" + id, { method: "DELETE", body: {} });
    }
    categories = await api("/categories");
    await categoryForm();
  });
}
function table(headers, rows) {
  return rows.length
    ? `<div class="table-scroll"><table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell ?? ""}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">No records to show.</div>';
}
async function customerForm(c, done) {
  modal(
    c ? "Edit customer" : "New customer",
    `<form id="customer-form"><div class="grid2">${formField("Name", "name", c?.name || "", "text", "required")}${formField("Phone (optional, include country code)", "phone", c?.phone || "")}${formField("Address (optional)", "address", c?.address || "")}${formField("Notes (optional)", "notes", c?.notes || "")}${formField("GSTIN (optional)", "gstin", c?.gstin || "")}${formField("Pin code (optional)", "pin_code", c?.pin_code || "")}</div><label class="check"><input type="checkbox" name="whatsapp_opt_in" ${c?.whatsapp_opt_in ? "checked" : ""}>Customer has agreed to receive WhatsApp bills and shop updates</label><p class="muted small">Leave unchecked if permission has not been given. Uncheck to stop future sends.</p><div class="actions"><button class="primary">Save customer</button></div></form>`,
  );
  $("#customer-form").onsubmit = action(async (e) => {
    e.preventDefault();
    const body = values(e.target);
    body.whatsapp_opt_in = e.target.elements.whatsapp_opt_in.checked ? 1 : 0;
    if (c) body.version = c.version;
    await api("/customers" + (c ? "/" + c.id : ""), {
      method: c ? "PUT" : "POST",
      body,
    });
    $("#dialog").close();
    await done();
    toast("Customer saved");
  });
}
async function customers() {
  $("#content").innerHTML =
    '<section class="card"><div class="toolbar"><label>Search customers<input id="cust-search" placeholder="Name or phone"></label><button id="new-customer" class="primary">+ New customer</button></div><div id="customers-list"></div><div class="actions"><button id="cust-prev">Previous</button><button id="cust-next">Next</button></div></section>';
  let offset = 0;
  async function refresh() {
    const query = $("#cust-search").value;
    const list = await api(
      "/customers?search=" + encodeURIComponent(query) + "&offset=" + offset,
    );
    if (page !== "customers" || query !== $("#cust-search").value) return;
    $("#customers-list").innerHTML = table(
      ["Name", "Phone", "Purchase visits", "WhatsApp", "Actions"],
      list.map((c) => [
        esc(c.name),
        esc(c.phone || "—"),
        c.visits,
        c.whatsapp_opt_in ? "Opted in" : "Not subscribed",
        `<button data-profile="${c.id}">Profile / balance</button> <button data-edit="${c.id}">Edit</button> <button data-history="${c.id}">History</button> <button data-wish="${c.id}">Wishlist</button> <button class="danger" data-delete="${c.id}">Delete</button>`,
      ]),
    );
    $("#cust-prev").disabled = offset === 0;
    $("#cust-next").disabled = list.length < 100;
    $("#customers-list").onclick = action(async (e) => {
      const cid = Number(
          e.target.dataset.profile ||
            e.target.dataset.edit ||
            e.target.dataset.history ||
            e.target.dataset.delete ||
            e.target.dataset.wish,
        ),
        c = list.find((x) => x.id === cid);
      if (!c) return;
      if (e.target.dataset.profile) return customerProfile(cid);
      if (e.target.dataset.edit) return customerForm(c, refresh);
      if (e.target.dataset.history) {
        reportStates.sales = { period: "all", customer_id: cid, search: "" };
        await navigate("sales");
      } else if (e.target.dataset.wish) await wishes(c);
      else if (
        confirm(
          "Delete customer profile? Saved invoice snapshots and audit records will be retained.",
        )
      ) {
        await api("/customers/" + cid, {
          method: "DELETE",
          body: { version: c.version },
        });
        await refresh();
      }
    });
  }
  $("#cust-search").oninput = debounce(() => {
    offset = 0;
    return refresh();
  });
  on("#new-customer", "click", () => customerForm(null, refresh));
  on("#cust-prev", "click", () => {
    offset = Math.max(0, offset - 100);
    return refresh();
  });
  on("#cust-next", "click", () => {
    offset += 100;
    return refresh();
  });
  await refresh();
}
async function wishes(c) {
  const list = await api("/customers/" + c.id + "/wishlist");
  modal(
    c.name + " · wishlist",
    table(
      ["Request", "Status", ""],
      list.map((w) => [
        esc(w.item_description),
        w.fulfilled ? "Done" : "Open",
        `<button data-done="${w.id}">${w.fulfilled ? "Reopen" : "Complete"}</button>`,
      ]),
    ) +
      '<form id="wish-form">' +
      formField("New request", "description", "", "text", "required") +
      '<button class="primary">Add request</button></form>',
  );
  $("#wish-form").onsubmit = action(async (e) => {
    e.preventDefault();
    await api("/customers/" + c.id + "/wishlist", {
      method: "POST",
      body: values(e.target),
    });
    await wishes(c);
  });
  $("#dialog-body").onclick = action(async (e) => {
    const w = list.find((w) => w.id === Number(e.target.dataset.done));
    if (w) {
      await api("/wishlist/" + w.id, {
        method: "PUT",
        body: { fulfilled: !w.fulfilled },
      });
      await wishes(c);
    }
  });
}
async function sales() {
  const state = (reportStates.sales ||= { period: "30days" });
  $("#content").innerHTML =
    '<div id="sales-totals"></div><section class="card">' +
    periodControls("sales") +
    `<div class="toolbar"><label>Find invoice or customer<input id="sales-search" placeholder="Name, phone or invoice" value="${esc(state.search || "")}"></label><a id="sales-csv" download>Export all matching sales (CSV)</a>${state.customer_id ? '<button id="clear-sales-customer">Show all customers</button>' : ""}</div><div id="sales-list"></div>${pager("sales")}</section>`;
  let offset = 0,
    list = [],
    sequence = 0;
  async function refresh() {
    const seq = ++sequence,
      params = periodParams("sales");
    params.set("search", $("#sales-search").value);
    params.set("offset", offset);
    if (state.customer_id) params.set("customer_id", state.customer_id);
    reportStates.sales.search = $("#sales-search").value;
    const data = await api("/reports/sales?" + params);
    if (page !== "sales" || seq !== sequence) return;
    list = data.rows;
    showBounds("sales", data);
    pageInfo("sales", data);
    $("#sales-csv").href = "/api/reports/sales/csv?" + params;
    $("#sales-totals").innerHTML = metricCards([
      ["Matching bills", data.summary.count],
      ["Pieces sold", data.summary.pieces],
      ["Total billed", rs(data.summary.total)],
      ["Current outstanding", rs(data.summary.outstanding)],
    ]);
    $("#sales-list").innerHTML = table(
      ["Invoice", "Date", "Customer", "Total", "Balance", "Actions"],
      list.map((b) => [
        esc(b.bill_no),
        esc(b.bill_date.slice(0, 10)),
        esc(b.customer_name),
        rs(b.total),
        rs(Math.max(0, b.total - b.paid_amount)),
        `<button data-view="${b.id}">View</button> <button data-pay="${b.id}">Payment</button> <button data-edit="${b.id}">Edit</button> <button data-void="${b.id}" class="danger">Void</button>`,
      ]),
    );

    $("#sales-list").onclick = action(async (e) => {
      const id = Number(
        e.target.dataset.view ||
          e.target.dataset.pay ||
          e.target.dataset.edit ||
          e.target.dataset.void,
      );
      if (!id) return;
      const row = list.find((r) => r.id === id);
      if (e.target.dataset.view) return receipt(id);
      if (e.target.dataset.pay) return paymentForm(id, refresh);
      if (e.target.dataset.edit) return editBill(id);
      const reason = prompt(
        "Reason for voiding this invoice (history will be kept):",
      );
      if (reason) {
        await api("/bills/" + id, {
          method: "DELETE",
          body: { version: row.version, reason },
        });
        await refresh();
        toast("Invoice voided");
      }
    });
  }
  bindPeriod("sales", () => {
    offset = 0;
    return refresh();
  });
  $("#sales-search").oninput = debounce(() => {
    offset = 0;
    return refresh();
  });
  on("#sales-prev", "click", () => {
    offset = Math.max(0, offset - 50);
    return refresh();
  });
  on("#sales-next", "click", () => {
    offset += 50;
    return refresh();
  });
  on("#clear-sales-customer", "click", () => {
    delete state.customer_id;
    delete reportStates.sales.customer_id;
    return sales();
  });
  await refresh();
}
async function paymentForm(id, done) {
  const [{ bill }, payments] = await Promise.all([
    api("/bills/" + id),
    api("/bills/" + id + "/payments"),
  ]);
  modal(
    "Payments · " + bill.bill_no,
    `<p>Outstanding: <b>${rs(bill.balance)}</b></p>${table(
      ["Date", "Amount", "Method"],
      payments.map((p) => [
        esc(p.payment_date),
        rs(p.amount),
        esc(p.payment_mode),
      ]),
    )}<form id="pay-form"><div class="grid2">${formField("Receive amount", "amount", bill.balance, "number", `min=".01" max="${bill.balance}" step=".01" required`)}${formField("Payment date", "payment_date", today(), "date", "required")}</div><label>Method<select name="payment_mode"><option>Cash</option><option>UPI</option><option>Card</option><option>Other</option></select></label><button class="primary" ${bill.balance <= 0 ? "disabled" : ""}>Record payment</button></form>`,
  );
  const payKey = uuid();
  $("#pay-form").onsubmit = action(async (e) => {
    e.preventDefault();
    await api("/bills/" + id + "/payments", {
      method: "POST",
      body: values(e.target),
      key: payKey,
    });
    $("#dialog").close();
    await done();
    toast("Payment recorded");
  });
}
async function editBill(id) {
  if (
    cart.length &&
    !confirm("Replace your current draft with this saved bill?")
  )
    return;
  const { bill, items } = await api("/bills/" + id);
  if (bill.tax_mode === "legacy")
    throw Error(
      "This historical bill needs reconciliation before editing. Its saved values have been preserved.",
    );
  const current = await Promise.all(
    items.map((i) => api("/items/" + i.item_id)),
  );
  cart = items.map((r, i) => ({
    item_id: r.item_id,
    name: r.item_name_snapshot,
    version: current[i].version,
    quantity: r.quantity,
    rate: r.rate,
    original_rate: current[i].rate,
    gst_rate: r.gst_rate,
    discount: r.discount,
  }));
  editing = bill;
  customer = bill.customer_id
    ? { id: bill.customer_id, name: bill.customer_name }
    : null;
  requestKey = uuid();
  draft = {
    tax_mode: bill.tax_mode,
    interstate: !!bill.interstate,
    bill_date: bill.bill_date,
    payment_mode: bill.payment_mode,
    paid_now: bill.paid_amount,
    customer: {},
    metadata: bill.metadata,
  };
  persist();
  await navigate("billing");
}
async function balances() {
  $("#content").innerHTML =
    '<div id="balances-totals"></div><section class="card"><div class="toolbar"><label>Find customer<input id="balance-search" placeholder="Name or phone"></label><label class="check"><input type="checkbox" id="only-outstanding">Only outstanding</label><a id="balance-csv" download>Export matching balances (CSV)</a></div><p id="balance-match" class="muted"></p><div id="balance-list"></div>' +
    pager("balance") +
    '<p class="small muted">Totals cover all named customers. Walk-in bills are available in Sales & payments. Visits count saved invoices.</p></section>';
  let offset = 0,
    sequence = 0;
  async function refresh() {
    const seq = ++sequence,
      params = new URLSearchParams({
        search: $("#balance-search").value,
        outstanding: $("#only-outstanding").checked ? "1" : "0",
        offset,
      });
    const data = await api("/reports/balances?" + params);
    if (page !== "balances" || seq !== sequence) return;
    const t = data.overall;
    $("#balances-totals").innerHTML = metricCards([
      ["Total billed", rs(t.total)],
      ["Payments received", rs(t.paid)],
      ["Total outstanding", rs(t.outstanding)],
    ]);
    $("#balance-match").textContent =
      `${data.summary.count} matching customers · Matching outstanding ${rs(data.summary.outstanding)}`;
    $("#balance-csv").href = "/api/reports/balances/csv?" + params;
    $("#balance-list").innerHTML = table(
      ["Customer", "Phone", "Visits", "Billed", "Collected", "Balance", ""],
      data.rows.map((r) => [
        esc(r.name),
        esc(r.phone || "—"),
        r.visits,
        rs(r.total_billed),
        rs(r.total_paid),
        rs(r.balance),
        `<button data-profile="${r.id}">Ledger / wishlist</button>`,
      ]),
    );
    pageInfo("balance", data);
  }
  $("#balance-search").oninput = debounce(() => {
    offset = 0;
    return refresh();
  });
  on("#only-outstanding", "change", () => {
    offset = 0;
    return refresh();
  });
  on("#balance-prev", "click", () => {
    offset = Math.max(0, offset - 50);
    return refresh();
  });
  on("#balance-next", "click", () => {
    offset += 50;
    return refresh();
  });
  $("#balance-list").onclick = action((e) =>
    e.target.dataset.profile
      ? customerProfile(Number(e.target.dataset.profile))
      : null,
  );
  await refresh();
}
async function expenses() {
  $("#content").innerHTML =
    '<div id="expense-totals"></div><section class="card"><div class="row between"><h3>Expense history</h3><button id="expense-add" class="primary">+ Add expense</button></div>' +
    periodControls("expenses") +
    '<div class="toolbar"><label>Search expenses<input id="expense-search" placeholder="Description or category"></label><label>Category<select id="expense-category"><option value="">All categories</option></select></label><a id="expense-csv" download>Export all matching expenses (CSV)</a></div><div id="expense-list"></div>' +
    pager("expense") +
    "</section>";
  let offset = 0,
    sequence = 0;
  async function refresh() {
    const seq = ++sequence,
      params = periodParams("expenses");
    params.set("offset", offset);
    params.set("search", $("#expense-search").value);
    params.set("category", $("#expense-category").value);
    const data = await api("/reports/expenses?" + params);
    if (page !== "expenses" || seq !== sequence) return;
    showBounds("expenses", data);
    pageInfo("expense", data);
    $("#expense-totals").innerHTML = metricCards([
      ["Matching expenses", data.summary.count],
      ["Total expenses", rs(data.summary.total)],
    ]);
    const category = params.get("category");
    $("#expense-category").innerHTML =
      '<option value="">All categories</option>' +
      data.categories
        .map((c) => `<option value="${esc(c)}">${esc(c)}</option>`)
        .join("");
    $("#expense-category").value = category;
    $("#expense-csv").href = "/api/reports/expenses/csv?" + params;
    $("#expense-list").innerHTML = table(
      ["Date", "Category", "Amount", "Method", "Description", ""],
      data.rows.map((r) => [
        esc(r.expense_date),
        esc(r.category),
        rs(r.amount),
        esc(r.payment_mode),
        esc(r.description),
        `<button data-expense-delete="${r.id}" class="danger">Delete</button>`,
      ]),
    );
  }
  bindPeriod("expenses", () => {
    offset = 0;
    return refresh();
  });
  $("#expense-search").oninput = debounce(() => {
    offset = 0;
    return refresh();
  });
  on("#expense-category", "change", () => {
    offset = 0;
    return refresh();
  });
  on("#expense-prev", "click", () => {
    offset = Math.max(0, offset - 50);
    return refresh();
  });
  on("#expense-next", "click", () => {
    offset += 50;
    return refresh();
  });
  on("#expense-add", "click", () => {
    modal(
      "Record expense",
      '<form id="expense-form"><div class="grid2">' +
        formField("Category", "category", "", "text", "required") +
        formField(
          "Amount",
          "amount",
          "",
          "number",
          'min=".01" step=".01" required',
        ) +
        formField("Date", "expense_date", today(), "date", "required") +
        formField("Description (optional)", "description") +
        '</div><label>Method<select name="payment_mode"><option>Cash</option><option>UPI</option><option>Card</option></select></label><button class="primary">Save expense</button></form>',
    );
    const key = uuid();
    $("#expense-form").onsubmit = action(async (e) => {
      e.preventDefault();
      await api("/expenses", { method: "POST", body: values(e.target), key });
      $("#dialog").close();
      await refresh();
    });
  });
  $("#expense-list").onclick = action(async (e) => {
    const id = e.target.dataset.expenseDelete;
    if (
      id &&
      confirm(
        "Delete this expense? The original record will remain in the activity history.",
      )
    ) {
      await api("/expenses/" + id, { method: "DELETE" });
      await refresh();
    }
  });
  await refresh();
}
async function stats() {
  $("#content").innerHTML =
    '<section class="card">' +
    periodControls("overview", "month") +
    '<p class="small muted">Outstanding is the current balance of bills in the selected period. Collections use payment dates. Rankings use recorded taxable sales, excluding GST; historical tax details may be incomplete.</p></section><div id="overview-results"></div>';
  let sequence = 0;
  async function refresh() {
    const seq = ++sequence,
      data = await api("/reports/overview?" + periodParams("overview"));
    if (page !== "stats" || seq !== sequence) return;
    showBounds("overview", data);
    const t = data.totals;
    $("#overview-results").innerHTML =
      metricCards([
        ["Bills", t.bill_count],
        ["Pieces sold", t.pieces],
        ["Sales incl GST", rs(t.revenue)],
        ["Collected in period", rs(t.payments_collected)],
        ["Current outstanding", rs(t.outstanding)],
        ["Expenses", rs(t.expenses)],
      ]) +
      `<div class="stack"><section class="card"><h3>${data.granularity === "day" ? "Daily" : "Monthly"} sales · ${esc(data.date_from)} to ${esc(data.date_to)}</h3>${verticalChart(data.series, data.granularity)}</section><section class="card"><h3>Categories sold · quantity</h3>${horizontalChart(data.categories, "quantity")}${table(
        ["Category", "Quantity", "Net sales (ex GST)"],
        data.categories.map((r) => [esc(r.name), r.quantity, rs(r.revenue)]),
      )}</section><div class="grid2"><section class="card"><h3>Best items by quantity</h3>${horizontalChart(data.top_quantity, "quantity")}</section><section class="card"><h3>Best items by net sales</h3>${horizontalChart(data.top_revenue, "revenue")}</section></div><section class="card"><h3>Best customers · total billed</h3>${table(
        ["Customer", "Visits", "Sales incl GST", ""],
        data.customers.map((c) => [
          esc(c.name),
          c.visits,
          rs(c.revenue),
          `<button data-profile="${c.id}">Profile / wishlist</button>`,
        ]),
      )}</section><section class="card"><h3>Common open wishlist demand · all customers</h3><p class="small muted">Current open requests, independent of the sales period. Exact descriptions grouped ignoring case and surrounding spaces; different wording stays separate.</p>${table(
        ["Request", "Customers", "Requests"],
        data.demand.map((r) => [esc(r.request), r.customers, r.requests]),
      )}</section></div>`;
    $("#overview-results").onclick = action(async (e) => {
      const bar = e.target.closest("[data-period-label]");
      if (bar) {
        const label = bar.dataset.periodLabel;
        reportStates.sales =
          label.length === 10
            ? { period: "custom", date_from: label, date_to: label }
            : { period: "month", month: label };
        await navigate("sales");
      } else if (e.target.dataset.profile)
        await customerProfile(Number(e.target.dataset.profile));
    });
  }
  bindPeriod("overview", refresh);
  await refresh();
}
async function messages() {
  const rows = await api("/messages");
  $("#content").innerHTML =
    `<div class="stack"><section class="card"><h3>Offers & new stock updates</h3><p class="muted">Upload an image and use an approved WhatsApp image template. Sends go only to customers whose permission is recorded.</p>${!config.whatsapp ? '<div class="notice">WhatsApp API is not configured. PDF download and manual sharing are available from each bill.</div>' : ""}<form id="campaign-form"><div class="grid2">${formField("Approved template name", "template", "", "text", "required")}<label>Offer / stock image<input name="image" type="file" accept="image/png,image/jpeg" required></label></div><label>Update text (template body parameter)<textarea name="text" maxlength="1000" required></textarea></label><button class="primary" ${!config.whatsapp ? "disabled" : ""}>Review & send update</button></form></section><section class="card"><div class="row between"><h3>Message queue</h3><button id="message-refresh">Refresh status</button></div><p class="small muted">“Accepted” means the provider accepted the request, not that the customer received it. Do not resend an uncertain message until you check the provider.</p>${table(
      ["Phone", "Type", "Status", "Detail"],
      rows.map((r) => [
        esc(r.phone),
        esc(r.kind),
        esc(r.status),
        esc(r.error || r.provider_id || "—"),
      ]),
    )}</section></div>`;
  on("#message-refresh", "click", messages);
  const key = uuid();
  let upload = null;
  $("#campaign-form").onsubmit = action(async (e) => {
    e.preventDefault();
    const f = e.target,
      file = f.elements.image.files[0];
    if (file.size > 5 * 1024 * 1024) throw Error("Image must be at most 5 MB");
    const count = await api("/campaigns/preview");
    if (
      !confirm(`Queue this update for ${count.recipients} opted-in customers?`)
    )
      return;
    if (!upload)
      upload = await api("/media", { method: "POST", body: file, raw: true });
    const result = await api("/campaigns", {
      method: "POST",
      body: {
        template: f.elements.template.value,
        text: f.elements.text.value,
        media: upload.id,
      },
      key,
    });
    toast(`Queued for ${result.queued} customers`);
    await messages();
  });
}
async function status() {
  const [s, audit] = await Promise.all([api("/status"), api("/audit")]);
  $("#content").innerHTML =
    `<div class="stack"><section class="card"><h3>Data protection</h3><p class="small">Database: ${esc(s.database)}</p><p class="muted">Writes use transactions and durable SQLite journaling. Automatic backups run in the background; keep an additional copy on another device.</p>${s.backup_error ? `<div class="notice">Backup failed: ${esc(s.backup_error)}. Check disk space and permissions.</div>` : ""}<div class="actions"><button id="backup-now" class="primary">Back up now</button><a href="/api/export" download>Export tables (ZIP of CSVs)</a></div><h3>Recent backups</h3><pre>${esc(s.backups.join("\n") || "First backup pending")}</pre>${s.legacy_payments_to_review || s.legacy_stock_lines_to_review ? `<div class="notice">Historical data review: ${s.legacy_payments_to_review} old migration payment records and ${s.legacy_stock_lines_to_review} stock lines need verification. They have not been guessed or overwritten. <button id="review-legacy">Review records</button></div>` : ""}</section><section class="card"><h3>Recent activity</h3>${table(
      ["When", "Role", "Action", "Record", "Details"],
      audit.map((a) => [
        esc(a.at),
        esc(a.actor),
        esc(a.action),
        esc(a.entity),
        `<details><summary>View</summary><pre>${esc(JSON.stringify(JSON.parse(a.snapshot), null, 2))}</pre></details>`,
      ]),
    )}</section></div>`;
  on("#review-legacy", "click", reconciliation);
  on("#backup-now", "click", async () => {
    await api("/backup", { method: "POST" });
    toast("Verified backup saved");
    await status();
  });
}
async function boot() {
  try {
    const health = await api("/health");
    if (
      health.application !== "cloth-shop-billing" ||
      health.api_version !== 3 ||
      !["reports", "customer_profiles", "configuration", "connection"].every(
        (c) => health.capabilities?.includes(c),
      )
    ) {
      incompatibleServer(
        "The running backend does not support this app version. This is a server/version mismatch, not an empty database.",
      );
      return;
    }
    if (health.stale_files) {
      serverNeedsRestart = true;
      showLogin("admin");
      $("#role").disabled = true;
      $("#login-error").textContent =
        "Server files were updated but the old Python process is still running. Sign in as admin to restart the backend.";
      return;
    }
    config = await api("/config");
    $("#login-company").textContent = config.company.name;
    document.title = config.company.name + " · Billing";
    draft.tax_mode = config.tax_mode;
    restore();
    if (!config.configured) {
      $("#login-error").textContent =
        "Run setupapp.bat to configure company and admin password before using the app.";
      return;
    }
    try {
      role = (await api("/session")).role;
      await start();
    } catch {
      showLogin();
    }
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
}
boot();
async function reconciliation() {
  const rows = await api("/reconciliation");
  modal(
    "Review historical records",
    `<p class="notice">Use actual shop records to confirm corrections. Nothing here assumes that an old bill was paid or how much stock was deducted.</p>${table(
      ["Invoice", ""],
      rows.map((r) => [
        esc(r.bill_no),
        `<button data-reconcile="${r.id}">Review</button>`,
      ]),
    )}`,
  );
  $("#dialog-body").onclick = action(async (e) => {
    const id = Number(e.target.dataset.reconcile);
    if (!id) return;
    const [{ bill, items }, payments] = await Promise.all([
      api("/bills/" + id),
      api("/bills/" + id + "/payments"),
    ]);
    const flagged = payments.filter(
      (p) => p.notes === "Migrated from historical bill",
    );
    modal(
      "Reconcile " + bill.bill_no,
      `<form id="reconcile-form"><p>Recorded paid: ${rs(bill.paid_amount)} · Outstanding: ${rs(bill.balance)}</p><h3>Old automatic migration payments</h3><p class="small muted">Check only a payment that your records prove was never received.</p>${flagged.map((p) => `<label class="check"><input type="checkbox" name="payment-${p.id}">Remove incorrect migration payment: ${rs(p.amount)} on ${esc(p.payment_date)}</label>`).join("") || "<p>No flagged payments.</p>"}<h3>Original stock deduction</h3><p class="small muted">Enter the quantity actually deducted when this old bill was created. This records how to reverse it later; it does not change current stock. Leave unknown values blank.</p>${items
        .filter((i) => i.stock_deducted === null)
        .map((i) =>
          formField(
            i.item_name_snapshot + " (sold " + i.quantity + ")",
            "line-" + i.id,
            "",
            "number",
            `min="0" max="${i.quantity}" step="1"`,
          ),
        )
        .join(
          "",
        )}<label>Evidence / reason<textarea name="reason" required></textarea></label><button class="primary">Save verified corrections</button></form>`,
    );
    $("#reconcile-form").onsubmit = action(async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      const body = {
        version: bill.version,
        reason: f.elements.reason.value,
        remove_migration_payments: flagged
          .filter((p) => f.elements["payment-" + p.id].checked)
          .map((p) => p.id),
        stock_deductions: {},
      };
      items
        .filter((i) => i.stock_deducted === null)
        .forEach((i) => {
          const v = f.elements["line-" + i.id].value;
          if (v !== "") body.stock_deductions[i.id] = Number(v);
        });
      if (
        !confirm(
          "Apply these verified corrections? The previous records and your reason will remain in the activity history.",
        )
      )
        return;
      await api("/bills/" + id + "/reconcile", { method: "POST", body });
      $("#dialog").close();
      toast("Corrections recorded");
      await status();
    });
  });
}
