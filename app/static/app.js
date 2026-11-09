"use strict";

const $ = (id) => document.getElementById(id);
const state = { clients: [], clientId: "", documents: [], mode: "search_only", busy: false, result: null, source: null, page: null };
const sampleFiles = ["northstar-insurance.txt", "ledgercloud-agreement.txt", "ledgercloud-invoice.txt"];
let toastTimer;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const message = typeof body.detail === "string" ? body.detail : "The request could not be completed. Check your input and retry.";
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function jsonPost(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function clientPath(suffix = "") {
  return `/api/clients/${encodeURIComponent(state.clientId)}${suffix}`;
}

function notify(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 10000 : 5000);
}

function controls() {
  document.querySelectorAll("[data-lock]").forEach((element) => { element.disabled = state.busy; });
  $("upload-button").disabled = state.busy || !state.clientId;
  $("ask-button").disabled = state.busy || !state.documents.length;
  $("question").disabled = state.busy || !state.documents.length;
  $("copy-button").disabled = state.busy;
  $("previous-page").disabled = state.busy || !state.page || state.page.page <= 1;
  $("next-page").disabled = state.busy || !state.page || state.page.page >= state.page.page_count;
}

async function work(message, action) {
  if (state.busy) return;
  state.busy = true;
  $("activity-text").textContent = message;
  $("activity").hidden = false;
  $("question-form").setAttribute("aria-busy", "true");
  controls();
  try { await action(); } catch (error) { notify(error.message || "Something went wrong. Please retry.", true); }
  finally {
    state.busy = false;
    $("activity").hidden = true;
    $("question-form").setAttribute("aria-busy", "false");
    controls();
  }
}

function clearResult() {
  state.result = null;
  state.source = null;
  state.page = null;
  $("result-layout").classList.remove("has-result");
  $("results").hidden = true;
  $("source-viewer").hidden = true;
  $("empty-state").hidden = false;
}

function renderClients() {
  $("client-select").replaceChildren(new Option("Choose a client", ""));
  state.clients.forEach((client) => $("client-select").add(new Option(client.name, client.id)));
  $("client-select").value = state.clientId;
  const client = state.clients.find((item) => item.id === state.clientId);
  $("workspace-label").textContent = client ? client.name.toUpperCase() : "YOUR WORKSPACE";
  $("delete-client").hidden = !client;
  $("example-questions").hidden = client?.name !== "Oak & Co (sample)";
}

function renderDocuments() {
  $("document-count").textContent = state.documents.length;
  $("document-list").replaceChildren();
  $("welcome").hidden = state.documents.length > 0;
  if (!state.documents.length) {
    $("document-list").append(node("p", "sidebar-empty", state.clientId ? "No documents yet. Add your first file." : "Choose a client to add documents."));
  }
  state.documents.forEach((document) => {
    const row = node("div", "document-row");
    const open = node("button", "document-open");
    open.dataset.lock = "";
    open.setAttribute("aria-label", `View ${document.name}`);
    open.title = document.name;
    open.append(node("span", "file-icon", document.media_type === "application/pdf" ? "PDF" : "TXT"));
    const details = node("span", "document-info");
    details.append(node("span", "document-title", document.name), node("span", "document-meta", `${document.page_count} ${document.page_count === 1 ? "page" : "pages"} · Ready`));
    open.append(details);
    open.addEventListener("click", () => work("Opening source…", () => viewSource({ document_id: document.id, page: 1 })));
    const remove = node("button", "icon-button document-delete", "×");
    remove.dataset.lock = "";
    remove.setAttribute("aria-label", `Delete ${document.name}`);
    remove.addEventListener("click", () => {
      if (!window.confirm(`Remove ${document.name} and its index from this client?`)) return;
      work("Removing document…", async () => {
        await api(clientPath(`/documents/${document.id}`), { method: "DELETE" });
        clearResult();
        await refreshDocuments();
        notify("Document and its index removed.");
      });
    });
    row.append(open, remove);
    $("document-list").append(row);
  });
  $("scope-label").textContent = state.documents.length ? `Searching only this client's ${state.documents.length} ${state.documents.length === 1 ? "document" : "documents"}.` : state.clientId ? "Add a document to start asking questions." : "Select a client to get started.";
  controls();
}

async function refreshDocuments() {
  state.documents = state.clientId ? await api(clientPath("/documents")) : [];
  renderDocuments();
}

async function selectClient(id) {
  clearResult();
  $("question").value = "";
  state.clientId = id;
  state.documents = [];
  renderClients();
  renderDocuments();
  await refreshDocuments();
}

async function uploadFiles(files) {
  if (!files.length || !state.clientId || state.busy) return;
  await work("Adding documents…", async () => {
    let added = 0;
    const errors = [];
    for (const file of files) {
      $("activity-text").textContent = `Reading ${file.name}…`;
      if (file.size > 10 * 1024 * 1024) { errors.push(`${file.name}: exceeds 10 MB.`); continue; }
      const form = new FormData();
      form.append("file", file);
      try { await api(clientPath("/documents"), { method: "POST", body: form }); added++; }
      catch (error) { errors.push(`${file.name}: ${error.message}`); }
    }
    clearResult();
    await refreshDocuments();
    notify(errors.length ? `${added} added. ${errors.join(" ")}` : `${added} ${added === 1 ? "document" : "documents"} ready.`, errors.length > 0);
    $("file-input").value = "";
  });
}

async function viewSource(source) {
  const page = await api(clientPath(`/documents/${source.document_id}/pages/${source.page}`));
  state.source = source;
  state.page = page;
  $("source-filename").textContent = page.filename;
  $("page-label").textContent = `Page ${page.page} of ${page.page_count}`;
  $("source-text").textContent = page.text;
  $("selected-quote").textContent = source.quote || "";
  $("selected-quote").hidden = !source.quote;
  $("original-link").href = clientPath(`/documents/${source.document_id}/file`) + `#page=${source.page}`;
  $("source-viewer").hidden = false;
  $("empty-state").hidden = true;
  document.querySelectorAll(".source-card").forEach((element) => {
    const selected = Number(element.dataset.number) === source.number;
    element.classList.toggle("selected", selected);
    element.setAttribute("aria-pressed", String(selected));
  });
  controls();
}

function renderResult(question, result) {
  state.result = { ...result, question };
  $("result-layout").classList.toggle("has-result", result.sources.length > 0);
  $("results").hidden = false;
  $("empty-state").hidden = true;
  $("asked-question").textContent = question;
  $("answer-heading").textContent = result.status === "search_only" ? "Matching passages" : "Finding";
  const statuses = { answered: ["Source citations checked", ""], search_only: ["Source search · no AI answer", "neutral"], insufficient_evidence: ["Insufficient evidence", "neutral"], conflicting_evidence: ["Conflicting evidence", "warning"] };
  const [label, className] = statuses[result.status];
  $("answer-status").textContent = label;
  $("answer-status").className = `answer-status ${className}`;
  $("answer-message").textContent = result.message;
  $("answer-message").hidden = !result.message;
  $("claims").replaceChildren();
  result.claims.forEach((claim) => {
    const paragraph = node("p", "claim", claim.text);
    claim.citations.forEach((number) => {
      const citation = node("button", "citation", `[${number}]`);
      citation.dataset.lock = "";
      citation.setAttribute("aria-label", `View source ${number}`);
      citation.addEventListener("click", () => work("Opening source…", () => viewSource(result.sources.find((source) => source.number === number))));
      paragraph.append(citation);
    });
    $("claims").append(paragraph);
  });
  $("source-list").replaceChildren();
  result.sources.forEach((source) => {
    const card = node("button", "source-card");
    card.dataset.number = source.number;
    card.dataset.lock = "";
    card.setAttribute("aria-label", `View source ${source.number}: ${source.filename}, page ${source.page}`);
    card.setAttribute("aria-pressed", "false");
    const top = node("div", "source-card-top");
    top.append(node("span", "source-number", source.number), node("span", "", source.filename), node("span", "source-page", `Page ${source.page} ↗`));
    card.append(top, node("p", "source-excerpt", source.quote || source.context));
    card.addEventListener("click", () => work("Opening source…", () => viewSource(source)));
    $("source-list").append(card);
  });
  $("copy-button").hidden = !result.sources.length;
  $("review-note").hidden = !result.sources.length;
  controls();
}

async function submitQuestion() {
  const question = $("question").value.trim();
  if (question.length < 3) { notify("Enter a question with at least three characters.", true); return; }
  if (!state.documents.length) return;
  await work(state.mode === "rag" ? "Finding evidence and checking citations… First-time indexing can take a moment." : "Searching your documents…", async () => {
    clearResult();
    const result = await api(clientPath("/ask"), jsonPost({ question }));
    renderResult(question, result);
    if (result.sources.length) await viewSource(result.sources[0]);
  });
}

$("client-select").addEventListener("change", (event) => work("Opening workspace…", () => selectClient(event.target.value)));
$("add-client").addEventListener("click", () => { $("client-name").value = ""; $("client-dialog").showModal(); $("client-name").focus(); });
$("cancel-client").addEventListener("click", () => $("client-dialog").close());
$("client-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("client-name").value.trim();
  if (!name) return;
  $("client-dialog").close();
  work("Creating workspace…", async () => {
    const client = await api("/api/clients", jsonPost({ name }));
    state.clients.push(client);
    await selectClient(client.id);
  });
});
$("delete-client").addEventListener("click", () => {
  const client = state.clients.find((item) => item.id === state.clientId);
  if (!window.confirm(`Remove ${client.name} and all its documents?`)) return;
  work("Removing workspace…", async () => {
    await api(clientPath(), { method: "DELETE" });
    state.clients = state.clients.filter((item) => item.id !== state.clientId);
    await selectClient(state.clients[0]?.id || "");
    notify("Client workspace removed.");
  });
});
$("upload-button").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
$("upload-button").addEventListener("dragover", (event) => { event.preventDefault(); if (!state.busy && state.clientId) $("upload-button").classList.add("dragging"); });
$("upload-button").addEventListener("dragleave", () => $("upload-button").classList.remove("dragging"));
$("upload-button").addEventListener("drop", (event) => { event.preventDefault(); $("upload-button").classList.remove("dragging"); uploadFiles([...event.dataTransfer.files]); });
$("question-form").addEventListener("submit", (event) => { event.preventDefault(); submitQuestion(); });
$("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submitQuestion(); } });
document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => { $("question").value = button.dataset.question; submitQuestion(); }));
$("previous-page").addEventListener("click", () => work("Opening page…", () => viewSource({ document_id: state.source.document_id, page: state.page.page - 1 })));
$("next-page").addEventListener("click", () => work("Opening page…", () => viewSource({ document_id: state.source.document_id, page: state.page.page + 1 })));
$("copy-button").addEventListener("click", async () => {
  if (!state.result) return;
  const result = state.result;
  const lines = [result.question, "", result.status === "search_only" ? "Source search results (not an AI answer):" : result.message];
  result.claims.forEach((claim) => lines.push(`${claim.text} ${claim.citations.map((number) => `[${number}]`).join(" ")}`));
  result.sources.forEach((source) => lines.push(`\n[${source.number}] ${source.filename}, page ${source.page}\n${source.quote || source.context}`));
  try { await navigator.clipboard.writeText(lines.join("\n")); notify("Copied with source references."); }
  catch { notify("Clipboard access is unavailable. Select and copy the finding manually.", true); }
});
$("sample-button").addEventListener("click", () => work("Loading sample documents…", async () => {
  let client = state.clients.find((item) => item.name === "Oak & Co (sample)");
  if (!client) { client = await api("/api/clients", jsonPost({ name: "Oak & Co (sample)" })); state.clients.push(client); }
  await selectClient(client.id);
  for (const filename of sampleFiles) {
    const response = await fetch(`/static/examples/${filename}`);
    if (!response.ok) throw new Error("A sample document could not be loaded.");
    const form = new FormData();
    form.append("file", await response.blob(), filename);
    try { await api(clientPath("/documents"), { method: "POST", body: form }); }
    catch (error) { if (error.status !== 409) throw error; }
  }
  await refreshDocuments();
  notify("Three fictional sample documents are ready.");
}));
$("mode-button").addEventListener("click", () => $("mode-dialog").showModal());
$("close-mode").addEventListener("click", () => $("mode-dialog").close());

work("Opening your workspace…", async () => {
  const [status, clients] = await Promise.all([api("/api/status"), api("/api/clients")]);
  state.mode = status.mode;
  state.clients = clients;
  const isAI = state.mode === "rag";
  $("mode-button").textContent = isAI ? "AI evidence search" : "Source search";
  $("mode-title").textContent = isAI ? "AI evidence search" : "Source search";
  $("ask-label").textContent = isAI ? "Ask question" : "Find evidence";
  $("mode-description").replaceChildren(
    node("p", "", isAI ? "Questions and document text are sent to OpenAI to find relevant passages and generate cited answers. Files and cached embeddings are stored in your local workspace." : "Keyword search and document viewing work locally without an API key. This mode returns matching passages, not generated answers."),
    node("p", "", isAI ? "Citations are checked against retrieved text. Review the source before relying on a finding; this does not guarantee the interpretation is correct." : "To enable semantic search and AI answers, add OPENAI_API_KEY to your .env file and restart the server. The README explains the setup and data flow.")
  );
  $("privacy-note").textContent = isAI ? "AI mode sends document text and questions to OpenAI." : "Documents stay on this machine in source-search mode.";
  await selectClient(clients[0]?.id || "");
});
