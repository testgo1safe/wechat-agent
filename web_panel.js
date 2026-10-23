let curKind = "friend", wl = [], cfg = {}, providers = {}, personas = {}, personaUser = "";
let contextUser = "", contextDisplayName = "";
let listLoaded = false;
let kbDocs = [], kbCfg = {};
const $ = s => document.querySelector(s);

function toast(m) {
  const t = $("#toast");
  t.textContent = m;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1600);
}

async function api(path, opt = {}, timeoutMs = 20000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(path, { ...opt, signal: ctrl.signal });
    const t = await r.text();
    if (!r.ok) {
      try {
        const j = JSON.parse(t);
        throw new Error(j.error || t);
      } catch (e) {
        if (e instanceof Error && e.message && e.message !== t) throw e;
        throw new Error(t || ("HTTP " + r.status));
      }
    }
    return t ? JSON.parse(t) : {};
  } catch (e) {
    if (e.name === "AbortError") throw new Error("请求超时，微信 Hook 可能未就绪");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function fillProviderOptions() {
  const sel = $("#provider");
  sel.innerHTML = "";
  Object.entries(providers).forEach(([k, v]) => {
    const o = document.createElement("option");
    o.value = k;
    o.textContent = v.label;
    sel.appendChild(o);
  });
}

function fillModelSuggestions(pid) {
  const list = $("#modelList");
  list.innerHTML = "";
  (providers[pid]?.models || []).forEach(m => {
    const o = document.createElement("option");
    o.value = m;
    list.appendChild(o);
  });
}

function onProviderChange() {
  const pid = $("#provider").value;
  const p = providers[pid] || {};
  fillModelSuggestions(pid);
  $("#apiKeyBox").style.display = p.needs_key ? "block" : "none";
  $("#baseUrlBox").style.display = (pid === "custom") ? "block" : "none";
  if (p.base_url && pid !== "custom") $("#base_url").value = p.base_url;
  if ((providers[pid]?.models || []).length && !$("#model").value) {
    $("#model").value = providers[pid].models[0];
  }
}

async function loadState() {
  const s = await api("/api/state");
  cfg = s.config;
  wl = s.whitelist;
  providers = s.providers || {};
  personas = s.personas || {};
  $("#status").textContent =
    "Hook " + (s.hook_ok ? "已连接" : "未连接") +
    " · 自动回复" + (cfg.enabled === false ? "已禁用" : "已启用") +
    " · 白名单 " + wl.length +
    " · 人格 " + Object.keys(personas).length +
    " · " + (cfg.llm?.provider || "ollama") + "/" + (cfg.llm?.model || cfg.model || "");
  fillProviderOptions();
  const llm = cfg.llm || {};
  $("#provider").value = llm.provider || "ollama";
  $("#model").value = llm.model || cfg.model || "qwen2.5:7b";
  $("#base_url").value = llm.base_url || "";
  $("#api_key").value = "";
  $("#apiKeyHint").textContent = llm.api_key_set
    ? ("已设置 " + (llm.api_key_mask || "****")) : "未设置";
  onProviderChange();
  $("#interval").value = cfg.interval || 3;
  $("#reply_all").checked = !!cfg.reply_all;
  $("#include_system").checked = !!cfg.include_system;
  kbCfg = cfg.knowledge_base || {};
  $("#kbEnabled").checked = !!kbCfg.enabled;
  $("#kbAllowCloud").checked = !!kbCfg.allow_cloud;
  const cloud = (llm.provider || "ollama") !== "ollama";
  $("#kbState").textContent = cloud && !kbCfg.allow_cloud
    ? "当前是云模型：知识库片段默认不会发往云端，检索预览仍可用。"
    : "文档仅供单独授权的白名单会话使用；未命中时按原回复逻辑处理。";
  renderWL();
  renderKBTargetOptions();
}

function renderWL() {
  const box = $("#whitelist");
  if (!wl.length) {
    box.innerHTML = '<div class="empty">空 — 去左边添加</div>';
    return;
  }
  box.innerHTML = "";
  wl.forEach(u => {
    const p = document.createElement("span");
    p.className = "pill";
    const nm = (nameCache[u] || u);
    const tag = personas[u] ? " *" : "";
    p.innerHTML = "<span>" + esc(nm + tag) + "</span>";
    const ctx = document.createElement("button");
    ctx.textContent = "ctx";
    ctx.title = "查看 AI 上下文";
    ctx.style.cssText = "background:transparent;color:var(--acc);padding:0 4px;font-size:11px";
    ctx.onclick = () => openContext(u, nm);
    const b = document.createElement("button");
    b.textContent = "x";
    b.onclick = () => toggle(u, "remove");
    p.appendChild(ctx);
    p.appendChild(b);
    box.appendChild(p);
  });
}

let nameCache = {};

function esc(s) {
  return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function hideHookBanner() {
  const b = $("#hookBanner");
  if (b) b.style.display = "none";
}

function showHookBanner(msg) {
  let b = $("#hookBanner");
  if (!b) {
    b = document.createElement("div");
    b.id = "hookBanner";
    b.style.cssText = "background:#3a2a12;border:1px solid #f59e0b;color:#fbbf24;padding:8px 12px;margin-bottom:10px;border-radius:8px;font-size:12px;cursor:pointer";
    b.title = "点击重试";
    b.onclick = () => loadList(true);
    const card = $("#list").parentNode;
    card.insertBefore(b, $("#list"));
  }
  b.textContent = msg + "（保留上次列表，点击此处重试）";
  b.style.display = "block";
}

function renderContacts(data) {
  const box = $("#list");
  box.innerHTML = "";
  data.forEach(c => {
    nameCache[c.username] = c.name;
    const row = document.createElement("div");
    row.className = "row";
    const inWl = wl.includes(c.username);
    const hasP = !!personas[c.username];
    row.innerHTML =
      '<div class="who"><b>' + esc(c.name) +
      '<span class="tag ' + c.kind + '">' + (c.kind === "friend" ? "好友" : "群") + '</span></b>' +
      "<small>" + esc(c.alias || c.username) + "</small></div>";
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const pbtn = document.createElement("button");
    pbtn.className = hasP ? "btnPersona on" : "btnPersona";
    pbtn.textContent = hasP ? "人格OK" : "人格";
    pbtn.onclick = () => openPersona(c.username, c.name);
    const cbtn = document.createElement("button");
    cbtn.className = "btnCtx";
    cbtn.textContent = "上下文";
    cbtn.onclick = () => openContext(c.username, c.name);
    const btn = document.createElement("button");
    btn.className = inWl ? "rm" : "add";
    btn.textContent = inWl ? "移出" : "加入";
    btn.onclick = () => toggle(c.username, inWl ? "remove" : "add");
    actions.appendChild(pbtn);
    actions.appendChild(cbtn);
    actions.appendChild(btn);
    row.appendChild(actions);
    box.appendChild(row);
  });
  renderWL();
  renderKBTargetOptions();
}

async function loadList(forceLoading) {
  const q = $("#search").value.trim();
  const box = $("#list");
  const hadRows = listLoaded && box.querySelectorAll(".row").length > 0;
  if (forceLoading || !hadRows) {
    box.innerHTML = '<div class="empty">加载中…</div>';
  }
  const data = await api(
    "/api/contacts?kind=" + curKind + "&q=" + encodeURIComponent(q), {}, 25000);
  if (!data.length) {
    if (hadRows && q) {
      toast("无搜索结果");
      return;
    }
    listLoaded = true;
    hideHookBanner();
    box.innerHTML = '<div class="empty">无结果</div>';
    return;
  }
  listLoaded = true;
  hideHookBanner();
  renderContacts(data);
}

function loadListSafe(forceLoading) {
  loadList(forceLoading).catch(e => {
    if (listLoaded && $("#list").querySelectorAll(".row").length > 0) {
      showHookBanner("微信 Hook 繁忙: " + e.message);
      return;
    }
    $("#list").innerHTML = '<div class="empty">加载失败: ' + esc(e.message) + '</div>';
  });
}

function openPersona(username, name) {
  personaUser = username;
  $("#personaTarget").textContent = name || username;
  $("#personaText").value = personas[username] || "";
  $("#personaModal").classList.remove("hidden");
}

function closePersona() {
  $("#personaModal").classList.add("hidden");
  personaUser = "";
}

function roleLabel(role, sender) {
  if (role === "assistant") return "我 / 机器人";
  return sender ? ("对方 · " + sender) : "对方";
}

function renderContext(data) {
  const errBox = $("#contextErr");
  const meta = $("#contextMeta");
  const recentBox = $("#contextRecent");
  const summaryBox = $("#contextSummary");
  if (data.error) {
    meta.textContent = "";
    errBox.textContent = "加载失败: " + data.error;
    errBox.classList.remove("hidden");
    summaryBox.value = "";
    recentBox.innerHTML = '<div class="empty" style="padding:8px">—</div>';
    return;
  }
  errBox.classList.add("hidden");
  if (data.history_error) {
    errBox.textContent = "读消息库失败: " + data.history_error;
    errBox.classList.remove("hidden");
  }
  let metaText = "消息库: " + (data.msg_db || "未找到")
    + " · 库内共 " + (data.history_total || 0) + " 条"
    + " · 送入模型最近 " + (data.recent_for_prompt?.length || 0)
    + "/" + (data.keep_recent || 0) + " 条"
    + " · 长期摘要" + (data.auto_summary_enabled
      ? ("自动更新(>" + (data.auto_summary_threshold || 0) + "条)")
      : "仅手动");
  if (data.has_persona) metaText += " · 已设人格";
  meta.textContent = metaText;
  summaryBox.value = data.summary || "";
  contextDisplayName = data.display_name || data.username;
  const recent = data.recent_for_prompt || [];
  if (!recent.length) {
    recentBox.innerHTML = '<div class="empty" style="padding:8px">无可用原文（修复后消息库可能只有新消息）</div>';
    return;
  }
  recentBox.innerHTML = "";
  recent.forEach(m => {
    const b = document.createElement("div");
    b.className = "ctx-bubble " + m.role;
    b.innerHTML = "<small>" + esc(roleLabel(m.role, m.sender)) + "</small>" + esc(m.text);
    recentBox.appendChild(b);
  });
}

function openContext(username, name) {
  contextUser = username;
  contextDisplayName = name || username;
  $("#contextTarget").textContent = contextDisplayName;
  $("#contextMeta").textContent = "加载中…";
  $("#contextErr").classList.add("hidden");
  $("#contextSummary").value = "";
  $("#contextRecent").innerHTML = '<div class="empty" style="padding:8px">加载中…</div>';
  $("#contextModal").classList.remove("hidden");
  loadContext();
}

function closeContext() {
  $("#contextModal").classList.add("hidden");
  contextUser = "";
  contextDisplayName = "";
}

async function loadContext() {
  if (!contextUser) return;
  try {
    const data = await api("/api/context?username=" + encodeURIComponent(contextUser), {}, 30000);
    renderContext(data);
  } catch (e) {
    renderContext({ error: e.message });
  }
}

async function saveContextSummary() {
  if (!contextUser) return;
  try {
    const data = await api("/api/context", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: contextUser,
        action: "save",
        summary: $("#contextSummary").value
      })
    }, 30000);
    renderContext(data);
    toast("长期记忆已保存");
  } catch (e) {
    toast("保存失败: " + e.message);
  }
}

async function resetContextMemory() {
  if (!contextUser) return;
  if (!confirm("确定清空「" + (contextDisplayName || contextUser) + "」的长期记忆？\n短期原文仍来自微信库，不受影响。")) return;
  try {
    const data = await api("/api/context", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: contextUser, action: "reset" })
    }, 30000);
    renderContext(data);
    toast("长期记忆已清空");
  } catch (e) {
    toast("清空失败: " + e.message);
  }
}

function editContextPersona() {
  if (!contextUser) return;
  const u = contextUser;
  const n = contextDisplayName;
  closeContext();
  openPersona(u, n);
}

async function savePersonaText(text) {
  if (!personaUser) return;
  const r = await api("/api/persona", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: personaUser, text: text })
  });
  personas = r.personas || {};
  toast(text ? "人格已保存" : "已清除人格");
  closePersona();
  renderWL();
  loadListSafe();
  $("#status").textContent = $("#status").textContent.replace(
    /人格 \d+/, "人格 " + Object.keys(personas).length);
}

async function toggle(username, action) {
  const r = await api("/api/whitelist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, username })
  });
  wl = r.whitelist;
  toast(action === "add" ? "已加入白名单" : "已移出");
  renderWL();
  renderKBTargetOptions();
  loadListSafe();
  $("#status").textContent = $("#status").textContent.replace(
    /白名单 \d+/, "白名单 " + wl.length);
}

function renderKBTargetOptions() {
  const selected = new Set(Array.from($("#kbTargets").selectedOptions).map(o => o.value));
  const preview = $("#kbPreviewTarget").value;
  for (const selector of ["#kbTargets", "#kbPreviewTarget"]) {
    const box = $(selector);
    box.innerHTML = "";
    for (const username of wl) {
      const option = document.createElement("option");
      option.value = username;
      option.textContent = nameCache[username] || username;
      if (selector === "#kbTargets") option.selected = selected.has(username);
      if (selector === "#kbPreviewTarget") option.selected = username === preview;
      box.appendChild(option);
    }
  }
}

function selectedKBTargets() {
  return Array.from($("#kbTargets").selectedOptions).map(o => o.value);
}

function kbPost(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KB-Admin": "1" },
    body: JSON.stringify(body)
  }, 30000);
}

function renderKBDocs() {
  const box = $("#kbDocs");
  box.innerHTML = "";
  if (!kbDocs.length) {
    box.innerHTML = '<div class="empty">还没有文档。先选择授权会话，再录入文字或导入文件。</div>';
    return;
  }
  for (const doc of kbDocs) {
    const row = document.createElement("div");
    row.className = "kb-doc";
    const title = document.createElement("strong");
    title.textContent = doc.title;
    const info = document.createElement("small");
    const labels = (doc.targets || []).map(u => nameCache[u] || u);
    info.textContent = (doc.source_name || "手动录入") + " · " +
      doc.chunk_count + " 段 · 授权：" + labels.join("、");
    const select = document.createElement("select");
    select.multiple = true;
    select.size = Math.min(4, Math.max(2, wl.length));
    for (const username of wl) {
      const option = document.createElement("option");
      option.value = username;
      option.textContent = nameCache[username] || username;
      option.selected = (doc.targets || []).includes(username);
      select.appendChild(option);
    }
    const actions = document.createElement("div");
    actions.className = "kb-actions";
    const save = document.createElement("button");
    save.className = "btn2";
    save.textContent = "保存授权";
    save.onclick = async () => {
      try {
        const targets = Array.from(select.selectedOptions).map(o => o.value);
        if (!targets.length) throw new Error("至少选择一个白名单会话");
        const result = await kbPost("/api/kb/documents",
          { action: "set_targets", id: doc.id, targets });
        kbDocs = result.documents || [];
        renderKBDocs();
        toast("文档授权已更新");
      } catch (e) { toast("更新失败: " + e.message); }
    };
    const remove = document.createElement("button");
    remove.className = "rm";
    remove.textContent = "删除";
    remove.onclick = async () => {
      if (!confirm("确定删除知识库文档「" + doc.title + "」？")) return;
      try {
        const result = await kbPost("/api/kb/documents",
          { action: "delete", id: doc.id });
        kbDocs = result.documents || [];
        renderKBDocs();
        toast("文档已删除");
      } catch (e) { toast("删除失败: " + e.message); }
    };
    actions.append(save, remove);
    row.append(title, info, select, actions);
    box.appendChild(row);
  }
}

async function loadKB() {
  const result = await api("/api/kb/documents");
  kbDocs = result.documents || [];
  renderKBDocs();
}

async function previewKB() {
  const username = $("#kbPreviewTarget").value;
  const query = $("#kbQuery").value.trim();
  if (!username || !query) {
    toast("先选会话并输入问题");
    return;
  }
  try {
    const params = new URLSearchParams({ username, q: query });
    const result = await api("/api/kb/search?" + params, {}, 30000);
    const box = $("#kbResults");
    box.innerHTML = "";
    if (!(result.hits || []).length) {
      box.innerHTML = '<div class="empty">没有可引用的授权片段</div>';
      return;
    }
    for (const hit of result.hits) {
      const item = document.createElement("div");
      item.className = "kb-result";
      item.textContent = "《" + hit.title + "》\n" + hit.snippet;
      box.appendChild(item);
    }
  } catch (e) { toast("检索失败: " + e.message); }
}

$("#saveSettings").onclick = async () => {
  const body = {
    interval: Number($("#interval").value) || 3,
    reply_all: $("#reply_all").checked,
    include_system: $("#include_system").checked,
    llm: {
      provider: $("#provider").value,
      model: $("#model").value.trim(),
      base_url: $("#base_url").value.trim(),
      api_key: $("#api_key").value.trim()
    }
  };
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  toast("设置已保存（守护进程将热加载）");
  loadState();
};

$("#kbSaveSettings").onclick = async () => {
  const allowCloud = $("#kbAllowCloud").checked;
  if (allowCloud && !kbCfg.allow_cloud &&
      !confirm("允许把命中的知识库原文片段发送给云模型？请先确认文档和数据合规要求。")) {
    $("#kbAllowCloud").checked = false;
    return;
  }
  try {
    const result = await kbPost("/api/kb/settings", {
      enabled: $("#kbEnabled").checked,
      allow_cloud: allowCloud
    });
    kbCfg = result.knowledge_base || {};
    toast("知识库开关已保存，守护进程将热加载");
    loadState();
  } catch (e) { toast("保存失败: " + e.message); }
};

$("#kbAddText").onclick = async () => {
  try {
    const targets = selectedKBTargets();
    const title = $("#kbTitle").value.trim();
    const content = $("#kbText").value.trim();
    if (!targets.length || !title || !content) {
      throw new Error("请填写标题、正文并选择授权会话");
    }
    if (content.length > 100000) throw new Error("正文超过 10 万字");
    const result = await kbPost("/api/kb/documents", {
      action: "add", title, text: content, targets
    });
    kbDocs = result.documents || [];
    renderKBDocs();
    $("#kbText").value = "";
    toast("文档已入库");
  } catch (e) { toast("入库失败: " + e.message); }
};

$("#kbUpload").onclick = async () => {
  try {
    const file = $("#kbFile").files[0];
    const targets = selectedKBTargets();
    if (!file || !targets.length) throw new Error("请选择文件和授权会话");
    if (file.size > 5 * 1024 * 1024) throw new Error("文件超过 5 MB");
    const params = new URLSearchParams({
      filename: file.name,
      title: $("#kbTitle").value.trim() || file.name,
      targets: JSON.stringify(targets)
    });
    const result = await api("/api/kb/upload?" + params, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-KB-Admin": "1" },
      body: file
    }, 60000);
    kbDocs = result.documents || [];
    renderKBDocs();
    $("#kbFile").value = "";
    toast("文件已入库");
  } catch (e) { toast("导入失败: " + e.message); }
};

$("#kbSearch").onclick = previewKB;
$("#provider").onchange = onProviderChange;
$("#personaSave").onclick = () => savePersonaText($("#personaText").value.trim());
$("#personaClear").onclick = () => savePersonaText("");
$("#personaCancel").onclick = closePersona;
$("#personaModal").onclick = e => { if (e.target.id === "personaModal") closePersona(); };
$("#contextClose").onclick = closeContext;
$("#contextRefresh").onclick = loadContext;
$("#contextSave").onclick = saveContextSummary;
$("#contextReset").onclick = resetContextMemory;
$("#contextEditPersona").onclick = editContextPersona;
$("#contextModal").onclick = e => { if (e.target.id === "contextModal") closeContext(); };

document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  curKind = t.dataset.kind;
  loadListSafe();
});

let tmr;
$("#search").oninput = () => {
  clearTimeout(tmr);
  tmr = setTimeout(loadListSafe, 250);
};

loadState()
  .then(() => {
    loadListSafe(true);
    return loadKB();
  })
  .catch(e => {
    $("#kbDocs").innerHTML = '<div class="empty">知识库加载失败: ' + esc(e.message) + '</div>';
    if (!cfg || !Object.keys(cfg).length) {
      $("#status").textContent = "错误: " + e.message;
      $("#list").innerHTML = '<div class="empty">加载失败，请 Ctrl+F5 刷新或运行 web_panel_service.ps1 restart</div>';
    }
  });
