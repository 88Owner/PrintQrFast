const state = {
  master: null,
  loaded: null,
  printing: false,
  previewToken: 0,
  settingsTimer: 0,
  printerCache: new Map(),
  suggestIndex: -1,
  suggestItems: [],
};

const $ = (id) => document.getElementById(id);
let previewUrl = "";
let toastTimer = 0;
const SUGGEST_LIMIT = 80;

function fold(value) {
  return String(value).normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

function fmt(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  if (Math.abs(number - Math.round(number)) < 1e-9) return String(Math.round(number));
  return String(Math.round(number * 100) / 100);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || "Có lỗi khi gọi máy chủ");
  return data;
}

function toast(message, kind = "ok") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind === "ok" ? "" : kind}`.trim();
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 3600);
}

function beep(ok = true) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  const ctx = new AudioCtx();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.frequency.value = ok ? 880 : 220;
  gain.gain.value = 0.04;
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + 0.08);
  osc.onended = () => ctx.close();
}

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = `status ${kind || ""}`.trim();
}

function currentBody() {
  const sku = $("sku").value.trim();
  const qr = $("qr").value.trim() || sku;
  return { sku, name: $("name").value.trim(), qr };
}

function isComplete() {
  const body = currentBody();
  return Boolean(body.sku && body.name && body.qr);
}

function labelSize() {
  return { w: 35, h: 22 };
}

function updateSaveButton() {
  const button = $("save-btn");
  if (!isComplete()) {
    button.hidden = true;
    return;
  }
  if (!state.loaded) {
    button.hidden = false;
    button.textContent = "Lưu vào master";
    return;
  }
  const body = currentBody();
  const dirty = body.name !== state.loaded.name || body.sku !== state.loaded.sku || body.qr !== state.loaded.qr;
  button.hidden = !dirty;
  button.textContent = "Cập nhật master";
}

function updatePrintState() {
  const size = labelSize();
  const ready = isComplete() && $("printer").value && size.w >= 15 && size.h >= 15 && !state.printing;
  $("print-btn").disabled = !ready;
}

function chooseProduct(product) {
  state.loaded = product;
  $("name").value = product.name;
  $("sku").value = product.sku;
  $("qr").value = product.qr || product.sku;
  $("product-search").value = product.name;
  hideSuggestions();
  setStatus("Đã có trong master data", "ok");
  refreshPreview();
  updateSaveButton();
  updatePrintState();
  renderTable();
}

function queryWords(query) {
  return fold(query).split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

function wordSpans(text) {
  return [...fold(text).matchAll(/[\p{L}\p{N}]+/gu)].map((match) => ({
    word: match[0],
    start: match.index,
    end: match.index + match[0].length,
  }));
}

function matchingProducts(query) {
  const products = state.master?.products || [];
  const words = queryWords(query);
  const list = words.length
    ? products.filter((product) => {
        const spans = wordSpans(product.name);
        return words.every((word) => spans.some((span) => span.word.startsWith(word)));
      })
    : products;
  return [...list].sort((a, b) => {
    if (words.length) {
      const rank = (product) => (wordSpans(product.name)[0]?.word.startsWith(words[0]) ? 0 : 1);
      const diff = rank(a) - rank(b);
      if (diff) return diff;
    }
    return a.name.localeCompare(b.name, "vi");
  });
}

function highlight(text, query) {
  const words = queryWords(query);
  if (!words.length) return [document.createTextNode(text)];
  const ranges = [];
  for (const span of wordSpans(text)) {
    const hit = words.filter((word) => span.word.startsWith(word)).sort((a, b) => b.length - a.length)[0];
    if (hit) ranges.push([span.start, span.start + hit.length]);
  }
  if (!ranges.length) return [document.createTextNode(text)];
  const nodes = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) nodes.push(document.createTextNode(text.slice(cursor, start)));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(start, end);
    nodes.push(mark);
    cursor = end;
  }
  if (cursor < text.length) nodes.push(document.createTextNode(text.slice(cursor)));
  return nodes;
}

function renderSuggestions() {
  const query = $("product-search").value;
  const matches = matchingProducts(query);
  const shown = matches.slice(0, SUGGEST_LIMIT);
  state.suggestItems = shown;
  if (state.suggestIndex >= shown.length) state.suggestIndex = shown.length - 1;
  const list = $("suggestions");
  list.replaceChildren();
  $("product-search").setAttribute("aria-expanded", "true");
  if (!shown.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = state.master?.products.length ? "Không có tên phù hợp" : "Chưa có master data. Hãy nhập file ở tab Master data.";
    list.append(item);
    list.hidden = false;
    return;
  }
  shown.forEach((product, index) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "option");
    if (index === state.suggestIndex) button.classList.add("active");
    const name = document.createElement("strong");
    name.append(...highlight(product.name, query));
    const sku = document.createElement("span");
    sku.textContent = product.sku;
    button.append(name, sku);
    button.addEventListener("mousedown", (event) => {
      event.preventDefault();
      chooseProduct(product);
    });
    item.append(button);
    list.append(item);
  });
  if (matches.length > shown.length) {
    const more = document.createElement("li");
    more.className = "more";
    more.textContent = `Còn ${matches.length - shown.length} sản phẩm. Gõ thêm để lọc.`;
    list.append(more);
  }
  list.hidden = false;
  const active = list.querySelector(".active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function hideSuggestions() {
  $("suggestions").hidden = true;
  $("product-search").setAttribute("aria-expanded", "false");
  state.suggestIndex = -1;
}

async function submitScan(raw) {
  const code = raw.trim();
  if (!code) {
    toast("Nhập hoặc quét SKU", "err");
    return;
  }
  try {
    const data = await api(`/api/lookup?sku=${encodeURIComponent(code)}`);
    if (!data.found) {
      beep(false);
      toast("Không có mã này trong master data", "err");
      $("scan").select();
      return;
    }
    chooseProduct(data.product);
    $("scan").value = "";
    beep(true);
    toast(`Đã nạp ${data.product.name}`);
    $("scan").focus();
  } catch (error) {
    toast(error.message, "err");
  }
}

let previewTimer = 0;
function refreshPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 140);
}

async function runPreview() {
  const token = ++state.previewToken;
  const meta = $("preview-meta");
  if (!isComplete()) {
    $("label").hidden = true;
    $("placeholder").hidden = false;
    $("placeholder").textContent = "Chọn sản phẩm hoặc quét mã để hiện tem";
    meta.textContent = "";
    return;
  }
  const size = labelSize();
  const body = currentBody();
  try {
    const response = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, label_width_mm: size.w, label_height_mm: size.h }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.message || "Không tạo được tem");
    }
    const blob = await response.blob();
    if (token !== state.previewToken) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("label").src = previewUrl;
    $("label").hidden = false;
    $("placeholder").hidden = true;
    const qrNote = body.qr === body.sku ? `QR chứa SKU ${body.sku}` : `QR chứa ${body.qr}`;
    meta.textContent = `Tem đôi 35 × 22 mm · 2 tem/hàng · ${qrNote}`;
    meta.className = "field-hint";
  } catch (error) {
    if (token !== state.previewToken) return;
    $("label").hidden = true;
    $("placeholder").hidden = false;
    $("placeholder").textContent = error.message;
  }
}

function stickerCount(value) {
  let count = Math.round(Number(value));
  if (!Number.isFinite(count) || count < 2) count = 2;
  if (count % 2) count += 1;
  if (count > 100) count = 100;
  if (count % 2) count -= 1;
  return count;
}

function collectSettings() {
  return {
    printer: $("printer").value,
    preset: "35x22",
    label_width_mm: 35,
    label_height_mm: 22,
    copies: stickerCount($("copies").value),
  };
}

function scheduleSettingsSave() {
  clearTimeout(state.settingsTimer);
  state.settingsTimer = setTimeout(() => {
    api("/api/settings", { method: "PUT", body: collectSettings() }).catch(() => {});
  }, 250);
}

function onPrintSettingsChange() {
  refreshPreview();
  updatePrintState();
  refreshDriverHint();
  scheduleSettingsSave();
}

function printerIsVirtual(name) {
  const text = name.toLowerCase();
  return ["pdf", "xps", "onenote", "fax"].some((key) => text.includes(key));
}

async function refreshDriverHint() {
  const hint = $("print-hint");
  const name = $("printer").value;
  const selected = $("printer").selectedOptions[0];
  const host = selected?.dataset.host || "";
  if (!name) {
    hint.textContent = state.printerMessage || "Chưa có máy in.";
    hint.className = "field-hint bad";
    return;
  }
  if (name.startsWith("lan:")) {
    hint.textContent = `In thẳng tới ${host || "máy in LAN"} cổng 9100. Số tem là số tem in ra, mỗi hàng 2 tem.`;
    hint.className = "field-hint";
    return;
  }
  if (printerIsVirtual(name)) {
    const extra = state.printerMessage ? ` ${state.printerMessage}` : "";
    hint.textContent = `Máy này sẽ hỏi nơi lưu file. Hãy chọn máy in tem.${extra}`;
    hint.className = "field-hint bad";
    return;
  }
  try {
    hint.textContent = `${host ? `IP ${host}. ` : ""}Số tem là đúng số tem in ra. Tem đôi, mỗi hàng 2 tem.`;
    hint.className = "field-hint";
    if (!state.printerCache.has(name)) {
      state.printerCache.set(name, await api(`/api/printer-info?name=${encodeURIComponent(name)}`));
    }
  } catch (error) {
    hint.textContent = error.message;
    hint.className = "field-hint warn";
  }
}

function fillPrinters(data) {
  const select = $("printer");
  const previous = select.value;
  select.replaceChildren();
  const items = data.printers || [];
  if (!items.length) select.append(new Option("Không có máy in", ""));
  for (const item of items) {
    const option = new Option(item.label || item.name || item.id, item.id);
    option.dataset.kind = item.kind || "windows";
    option.dataset.host = item.host || "";
    option.dataset.online = item.online === false ? "0" : "1";
    select.append(option);
  }
  const valid = (id) => Boolean(id) && [...select.options].some((option) => option.value === id);
  const lan = [...select.options].filter((option) => option.dataset.kind === "lan");
  const liveLan = lan.filter((option) => option.dataset.online !== "0");
  const savedIsLan = valid(state.savedPrinter) && lan.some((option) => option.value === state.savedPrinter);
  const previousIsLan = valid(previous) && [...select.options].some((option) => option.value === previous && option.dataset.kind === "lan");
  let preferred = "";
  if (previousIsLan) preferred = previous;
  else if (liveLan.length === 1) preferred = liveLan[0].value;
  else if (savedIsLan) preferred = state.savedPrinter;
  else if (lan.length === 1) preferred = lan[0].value;
  else if (valid(previous)) preferred = previous;
  else if (valid(data.default)) preferred = data.default;
  if (preferred) select.value = preferred;
  state.printerMessage = data.message || "";
}

async function reloadPrinters(quiet = false) {
  const data = await api(quiet ? "/api/printers" : "/api/printers?refresh=1");
  state.printerCache.clear();
  fillPrinters(data);
  refreshDriverHint();
  updatePrintState();
  if (!quiet) toast("Đã tải danh sách máy in");
}

async function saveCurrent() {
  try {
    const saved = await api("/api/products", { method: "POST", body: currentBody() });
    toast(`Đã lưu ${saved.product.sku}`);
    await reloadMaster();
    chooseProduct(saved.product);
  } catch (error) {
    toast(error.message, "err");
  }
}

async function printNow() {
  updatePrintState();
  if ($("print-btn").disabled) return;
  state.printing = true;
  updatePrintState();
  $("print-btn").textContent = "Đang gửi…";
  const size = labelSize();
  try {
    const copies = stickerCount($("copies").value);
    $("copies").value = String(copies);
    const result = await api("/api/print", {
      method: "POST",
      body: {
        ...currentBody(),
        printer: $("printer").value,
        copies,
        label_width_mm: size.w,
        label_height_mm: size.h,
      },
    });
    toast(result.warning ? `${result.message} ${result.warning}` : result.message, result.warning ? "warn" : "ok");
    $("product-search").focus();
  } catch (error) {
    toast(error.message, "err");
  } finally {
    state.printing = false;
    $("print-btn").textContent = "In ngay";
    updatePrintState();
  }
}

function showTab(name) {
  const print = name === "print";
  $("view-print").hidden = !print;
  $("view-master").hidden = print;
  $("tab-print").setAttribute("aria-selected", String(print));
  $("tab-master").setAttribute("aria-selected", String(!print));
  window.scrollTo(0, 0);
  if (print) $("product-search").focus();
}

function renderTable() {
  if (!state.master) return;
  const words = queryWords($("search").value);
  const currentSku = $("sku").value.trim();
  const rows = state.master.products.filter((product) => {
    if (!words.length) return true;
    const spans = wordSpans(`${product.name} ${product.sku} ${product.qr || ""}`);
    return words.every((word) => spans.some((span) => span.word.startsWith(word)));
  });
  $("product-count").textContent = `${rows.length} sản phẩm`;
  const body = $("product-rows");
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = state.master.products.length ? "Không có sản phẩm phù hợp" : "Chưa có dữ liệu. Hãy nhập file master.";
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const product of rows) {
    const tr = document.createElement("tr");
    if (product.sku === currentSku) tr.className = "current";
    const cells = [product.sku, product.name, product.qr];
    cells.forEach((text, index) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (index === 0) td.className = "sku";
      if (index === 2) {
        td.className = "qr";
        td.title = text;
      }
      tr.append(td);
    });
    const actions = document.createElement("td");
    const use = document.createElement("button");
    use.type = "button";
    use.className = "linkish";
    use.textContent = "In";
    use.addEventListener("click", () => {
      chooseProduct(product);
      showTab("print");
    });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "linkish";
    edit.textContent = "Sửa";
    edit.addEventListener("click", () => {
      $("m-sku").value = product.sku;
      $("m-name").value = product.name;
      $("m-qr").value = product.qr;
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "linkish danger";
    remove.textContent = "Xóa";
    remove.addEventListener("click", () => deleteProduct(product));
    actions.append(use, edit, remove);
    tr.append(actions);
    body.append(tr);
  }
}

async function deleteProduct(product) {
  if (!window.confirm(`Xóa sản phẩm ${product.sku}?`)) return;
  try {
    await api("/api/products", { method: "DELETE", body: { sku: product.sku } });
    if (state.loaded && state.loaded.sku === product.sku) state.loaded = null;
    await reloadMaster();
    updateSaveButton();
    toast(`Đã xóa ${product.sku}`);
  } catch (error) {
    toast(error.message, "err");
  }
}

async function reloadMaster() {
  state.master = await api("/api/master");
  renderTable();
}

async function saveMasterProduct(event) {
  event.preventDefault();
  const body = {
    sku: $("m-sku").value.trim(),
    name: $("m-name").value.trim(),
    qr: $("m-qr").value.trim(),
  };
  if (!body.sku || !body.name) {
    toast("Nhập SKU và tên sản phẩm", "err");
    return;
  }
  try {
    const saved = await api("/api/products", { method: "POST", body });
    toast(`Đã lưu ${saved.product.sku}`);
    await reloadMaster();
    $("m-sku").value = saved.product.sku;
    $("m-name").value = saved.product.name;
    $("m-qr").value = saved.product.qr;
    if (state.loaded && state.loaded.sku === saved.product.sku) chooseProduct(saved.product);
  } catch (error) {
    toast(error.message, "err");
  }
}

async function importFile() {
  const file = $("import-file").files[0];
  if (!file) {
    toast("Chọn file CSV hoặc Excel", "err");
    return;
  }
  const current = state.master?.products.length || 0;
  const note = current ? `Thay ${current} sản phẩm hiện tại bằng nội dung file ${file.name}?` : `Nhập master data từ ${file.name}?`;
  if (!window.confirm(note)) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/api/import", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "Không nhập được file");
    await reloadMaster();
    $("import-file").value = "";
    toast(`Đã nhập ${data.imported} sản phẩm`);
  } catch (error) {
    toast(error.message, "err");
  }
}

function onSearchKey(event) {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    if ($("suggestions").hidden) renderSuggestions();
    state.suggestIndex = Math.min(state.suggestItems.length - 1, state.suggestIndex + 1);
    renderSuggestions();
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    state.suggestIndex = Math.max(0, state.suggestIndex - 1);
    renderSuggestions();
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    if (state.suggestIndex >= 0 && state.suggestItems[state.suggestIndex]) chooseProduct(state.suggestItems[state.suggestIndex]);
    else if (state.suggestItems.length === 1) chooseProduct(state.suggestItems[0]);
    return;
  }
  if (event.key === "Escape") hideSuggestions();
}

function bind() {
  $("scan-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitScan($("scan").value);
  });
  $("product-search").addEventListener("input", () => {
    state.suggestIndex = -1;
    renderSuggestions();
  });
  $("product-search").addEventListener("focus", renderSuggestions);
  $("product-search").addEventListener("keydown", onSearchKey);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".combo")) hideSuggestions();
  });
  for (const id of ["name", "sku", "qr"]) {
    $(id).addEventListener("input", () => {
      refreshPreview();
      updateSaveButton();
      updatePrintState();
    });
  }
  $("save-btn").addEventListener("click", saveCurrent);
  $("print-btn").addEventListener("click", printNow);
  $("copies").addEventListener("input", onPrintSettingsChange);
  $("copies").addEventListener("wheel", (event) => {
    if (document.activeElement === $("copies")) event.preventDefault();
  }, { passive: false });
  $("copies").addEventListener("change", () => {
    $("copies").value = String(stickerCount($("copies").value));
    onPrintSettingsChange();
  });
  $("copies-dec").addEventListener("click", () => {
    $("copies").value = String(stickerCount((Number($("copies").value) || 2) - 2));
    onPrintSettingsChange();
  });
  $("copies-inc").addEventListener("click", () => {
    $("copies").value = String(stickerCount((Number($("copies").value) || 2) + 2));
    onPrintSettingsChange();
  });
  $("printer").addEventListener("change", onPrintSettingsChange);
  $("reload-printers").addEventListener("click", () => reloadPrinters(false));
  $("tab-print").addEventListener("click", () => showTab("print"));
  $("tab-master").addEventListener("click", () => showTab("master"));
  $("import-btn").addEventListener("click", importFile);
  $("master-form").addEventListener("submit", saveMasterProduct);
  $("master-clear").addEventListener("click", () => {
    $("m-sku").value = "";
    $("m-name").value = "";
    $("m-qr").value = "";
  });
  $("search").addEventListener("input", renderTable);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      printNow();
    }
  });
}

async function init() {
  bind();
  try {
    const [master, settings, printers] = await Promise.all([
      api("/api/master"),
      api("/api/settings"),
      api("/api/printers"),
    ]);
    state.master = master;
    state.savedPrinter = settings.printer || "";
    renderTable();
    if (!master.products.length) setStatus("Chưa có master data. Nhập file ở tab Master data.", "warn");
    $("copies").value = String(stickerCount(settings.copies || 2));
    fillPrinters(printers);
    refreshDriverHint();
    updatePrintState();
  } catch (error) {
    toast(error.message, "err");
  } finally {
    document.body.classList.remove("loading");
    $("product-search").focus();
  }
}

init();
