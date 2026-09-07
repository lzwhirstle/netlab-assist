"use strict";

const titles = {
  guide: "连接工作台",
  throughput: "吞吐测试",
  acl: "ACL验证",
  multicast: "组播 IPTV",
  dns: "DNS NAT",
  monitor: "连续监控",
  records: "实验记录",
};

const jobs = { throughput: null, multicast: null, monitor: null };
const historyKey = "netlab-assist-history-v1";
const peerSessionKey = "netlab-assist-peer-v2";
let localStatus = null;
let peerConfig = null;

function el(id) { return document.getElementById(id); }

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error("本机服务连接已中断。请确认 NetLab Assist 程序窗口仍在运行，然后刷新本页。");
  }
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function peerFailureMessage(classification) {
  const messages = {
    invalid_code: "实验码不正确。请使用对端页面右上角显示的本轮实验码。",
    refused: "对端拒绝连接。请确认另一台电脑已启动 NetLab Assist，控制端口保持 18080。",
    timeout: "连接对端超时。优先检查是否填了错误的网卡 IP，以及 Windows 防火墙是否允许专用网络。",
    network_error: "当前网络无法到达对端。请检查两端 IP、VLAN、网关和有线网卡状态。",
    incompatible: "两台电脑的软件版本不兼容，请两端都替换为同一个最新版。",
    service_blocked: "对端程序已找到，但数据端口被阻断。请允许 NetLab Assist 通过系统防火墙后重试。",
    peer_error: "对端返回异常，请确认两台电脑都使用相同的最新版。",
  };
  return messages[classification] || "未能连接对端，请检查 IP、实验码和防火墙。";
}

function currentPeerPayload() {
  if (!peerConfig) {
    showPage("guide");
    throw new Error("请先连接实验对端。另一台电脑只需保持程序运行，不需要点击开始。 ");
  }
  return {
    target: peerConfig.target,
    peer_token: peerConfig.peer_token,
    peer_ui_port: peerConfig.peer_ui_port,
    peer_throughput_port: peerConfig.throughput_port,
  };
}

function toast(message, error = false) {
  const box = el("toast");
  box.textContent = message;
  box.className = error ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.className = ""; }, 3200);
}

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function setPairState(kind, text) {
  const state = el("pair-state");
  state.className = `connection-state ${kind}`;
  state.querySelector("span").textContent = text;
  const rail = el("link-label").parentElement;
  rail.classList.toggle("connected", kind === "ready");
  el("link-label").textContent = kind === "ready" ? "链路就绪" : kind === "testing" ? "检查中" : "等待连接";
}

function renderDiagnostics(result) {
  const box = el("pair-diagnostics");
  box.hidden = false;
  box.textContent = "";
  box.className = `diagnostic-strip ${result.ready ? "success" : "error"}`;
  const summary = document.createElement("strong");
  summary.textContent = result.ready
    ? `连接检查通过：本机通过 ${result.route_ip || "当前实验网卡"} 到达对端，现在只需在本机点击测试。`
    : peerFailureMessage(result.classification);
  box.appendChild(summary);
  const checks = Object.values(result.checks || {});
  if (checks.length) {
    const items = document.createElement("div");
    items.className = "diagnostic-items";
    const names = { control: "控制 18080", throughput: "吞吐", echo: "回显" };
    for (const check of checks) {
      const item = document.createElement("span");
      item.textContent = `${check.ok ? "✓" : "×"} ${names[check.name] || check.name} · ${check.ok ? "正常" : check.classification}`;
      items.appendChild(item);
    }
    box.appendChild(items);
  }
}

function applyPeerState() {
  const reuseIds = ["throughput-peer", "multicast-peer", "nat-peer", "monitor-peer"];
  for (const id of reuseIds) {
    const box = el(id);
    if (!box) continue;
    box.classList.toggle("ready", Boolean(peerConfig));
    box.querySelector("span").textContent = peerConfig
      ? `已连接 ${peerConfig.hostname || peerConfig.target} · ${peerConfig.target}:${peerConfig.peer_ui_port}`
      : id === "monitor-peer" ? "连接对端后会自动填入目标，也可手动修改" : "请先在“连接工作台”连接对端";
  }
  if (peerConfig) {
    const monitorForm = el("monitor-form");
    monitorForm.elements.target.value = peerConfig.target;
    monitorForm.elements.port.value = peerConfig.echo_port;
  }
}

async function checkPeer(data, quiet = false) {
  const form = el("pair-form");
  const button = el("pair-button");
  button.disabled = true;
  button.textContent = "正在检查三项服务";
  setPairState("testing", "正在检查");
  try {
    const result = await api("/api/peer/check", data);
    renderDiagnostics(result);
    if (!result.ready) {
      peerConfig = null;
      sessionStorage.removeItem(peerSessionKey);
      applyPeerState();
      setPairState("failed", "连接失败");
      if (!quiet) toast(peerFailureMessage(result.classification), true);
      return false;
    }
    peerConfig = {
      target: result.target,
      peer_token: data.peer_token,
      peer_ui_port: number(data.peer_ui_port, 18080),
      throughput_port: result.peer.throughput_port,
      echo_port: result.peer.echo_port,
      hostname: result.peer.hostname,
      version: result.peer.version,
    };
    sessionStorage.setItem(peerSessionKey, JSON.stringify(peerConfig));
    form.elements.target.value = peerConfig.target;
    form.elements.peer_token.value = peerConfig.peer_token;
    form.elements.peer_ui_port.value = peerConfig.peer_ui_port;
    applyPeerState();
    setPairState("ready", "对端已连接");
    if (!quiet) toast("连接成功。后续测试只在本机点击一次。 ");
    return true;
  } catch (error) {
    peerConfig = null;
    sessionStorage.removeItem(peerSessionKey);
    applyPeerState();
    setPairState("failed", "连接失败");
    renderDiagnostics({ ready: false, classification: "network_error", checks: {} });
    if (!quiet) toast(error.message, true);
    return false;
  } finally {
    button.disabled = false;
    button.textContent = "连接并检查";
  }
}

function pairingUI() {
  const form = el("pair-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = formObject(form);
    data.peer_ui_port = number(data.peer_ui_port, 18080);
    await checkPeer(data);
  });
}

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatMbps(value) {
  return value === undefined || value === null ? "—" : `${number(value).toFixed(2)} Mbps`;
}

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(historyKey) || "[]"); }
  catch { return []; }
}

function saveRecord(kind, title, data) {
  const records = loadHistory();
  records.unshift({ id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`, kind, title, saved_at: new Date().toISOString(), data });
  localStorage.setItem(historyKey, JSON.stringify(records.slice(0, 100)));
  renderRecords();
}

function resultSummary(record) {
  const data = record.data || {};
  if (record.kind === "throughput") return `${data.mode || "测试"} · ${formatMbps(data.aggregate_average_mbps)}`;
  if (record.kind === "acl") return `${data.passed || 0}/${data.total || 0} 符合预期，${data.uncertain || 0} 项待确认`;
  if (record.kind === "multicast") return `${data.mode === "reverse" ? "对端→本机" : "本机→对端"} · ${formatMbps(data.average_mbps)} · 丢包 ${data.loss_percent ?? "—"}%`;
  if (record.kind === "dns") return `${data.name || "DNS"} · ${data.query_transaction_id || ""} · ${data.elapsed_ms || 0} ms`;
  if (record.kind === "nat") return `${data.client_local || ""} → ${data.client_destination || ""}`;
  if (record.kind === "monitor") return `可用率 ${data.availability_percent ?? "—"}% · 最长中断 ${data.longest_outage_s ?? "—"} s`;
  return "已保存实验结果";
}

function renderRecords() {
  const records = loadHistory();
  el("record-count").textContent = `${records.length}条记录`;
  const list = el("record-list");
  list.textContent = "";
  if (!records.length) {
    const empty = document.createElement("div");
    empty.className = "empty-card";
    empty.textContent = "暂无实验记录";
    list.appendChild(empty);
    return;
  }
  for (const record of records) {
    const card = document.createElement("div");
    card.className = "record-card";
    const type = document.createElement("span"); type.className = "type"; type.textContent = record.title;
    const summary = document.createElement("span"); summary.className = "summary"; summary.textContent = resultSummary(record);
    const time = document.createElement("time"); time.textContent = new Date(record.saved_at).toLocaleString();
    card.append(type, summary, time);
    list.appendChild(card);
  }
}

function drawChart(canvas, samples, mode = "rate") {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth);
  const height = Math.max(180, canvas.clientHeight);
  canvas.width = width * dpr; canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  const pad = { l: 42, r: 15, t: 17, b: 29 };
  const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
  ctx.strokeStyle = "#e8efec"; ctx.fillStyle = "#81908a"; ctx.font = "10px system-ui"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + plotH * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(width - pad.r, y); ctx.stroke();
  }
  if (!samples || !samples.length) {
    ctx.fillText("等待采样", pad.l + 8, pad.t + plotH / 2);
    return;
  }
  const xMax = Math.max(1, ...samples.map(item => number(item.second)));
  const values = samples.map(item => mode === "state" ? (item.up ? 1 : 0) : number(item.mbps));
  const yMax = mode === "state" ? 1 : Math.max(1, ...values) * 1.12;
  const groups = mode === "state" ? { state: samples } : Object.groupBy ? Object.groupBy(samples, item => item.direction || "rate") : samples.reduce((acc, item) => { const key = item.direction || "rate"; (acc[key] ||= []).push(item); return acc; }, {});
  const colors = { forward: "#1677ff", reverse: "#20a7c9", rate: "#1677ff", state: "#18a875" };
  for (const [key, group] of Object.entries(groups)) {
    ctx.strokeStyle = colors[key] || "#8c6bd6"; ctx.lineWidth = 2.2; ctx.beginPath();
    group.forEach((item, index) => {
      const x = pad.l + number(item.second) / xMax * plotW;
      const raw = mode === "state" ? (item.up ? 1 : 0) : number(item.mbps);
      const y = pad.t + plotH - raw / yMax * plotH;
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.fillText(mode === "state" ? "UP" : `${yMax.toFixed(1)} Mbps`, 3, pad.t + 4);
  ctx.fillText(mode === "state" ? "DOWN" : "0", 3, pad.t + plotH);
  ctx.fillText(`${xMax.toFixed(1)} s`, width - pad.r - 33, height - 9);
}

async function pollJob(feature, jobId, onUpdate, onDone) {
  jobs[feature] = jobId;
  while (jobs[feature] === jobId) {
    let job;
    try { job = await api(`/api/jobs/${jobId}`); }
    catch (error) { toast(error.message, true); return; }
    onUpdate(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      jobs[feature] = null;
      onDone(job);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}

async function stopActiveJobs() {
  for (const [feature, jobId] of Object.entries(jobs)) {
    if (!jobId) continue;
    try { await api(`/api/jobs/${jobId}/cancel`, {}); toast(`${titles[feature] || feature}正在停止`); }
    catch (error) { toast(error.message, true); }
  }
}

function throughputUI() {
  const form = el("throughput-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    let data;
    try { data = { ...formObject(form), ...currentPeerPayload() }; }
    catch (error) { toast(error.message, true); return; }
    data.duration = number(data.duration, 10); data.streams = number(data.streams, 4); data.peer_ui_port = number(data.peer_ui_port, 18080);
    el("throughput-state").textContent = "正在建立连接";
    try {
      const started = await api("/api/throughput", data);
      pollJob("throughput", started.job_id, job => {
        el("throughput-state").textContent = job.status === "running" ? `测试中 ${Math.round(job.progress * 100)}%` : job.status;
        const samples = job.samples || [];
        const last = samples.at(-1);
        el("throughput-live").firstChild.textContent = `${last ? number(last.mbps).toFixed(1) : "0.0"} `;
        drawChart(el("throughput-chart"), samples);
      }, job => {
        if (job.status === "completed") {
          const results = job.result.results || {};
          el("metric-forward").textContent = formatMbps(results.forward?.average_mbps);
          el("metric-reverse").textContent = formatMbps(results.reverse?.average_mbps);
          const connected = Object.values(results).reduce((sum, item) => sum + number(item.streams_connected), 0);
          el("metric-streams").textContent = `${connected}`;
          el("throughput-state").textContent = job.result.failures?.length ? "完成，部分方向失败" : "测试完成";
          saveRecord("throughput", "吞吐测试", job.result);
        } else {
          el("throughput-state").textContent = job.error || job.status;
          toast(job.error || "测试已停止", job.status === "failed");
        }
      });
    } catch (error) { el("throughput-state").textContent = "启动失败"; toast(error.message, true); }
  });
}

function parseMatrix(text) {
  return text.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith("#")).map((line, index) => {
    const parts = line.split(",").map(item => item.trim());
    if (parts.length < 5) throw new Error(`第${index + 1}行应包含5列`);
    const [name, target, kind, port, expected] = parts;
    if (!["tcp", "ping"].includes(kind.toLowerCase())) throw new Error(`第${index + 1}行方式必须是tcp或ping`);
    if (!["allow", "deny"].includes(expected.toLowerCase())) throw new Error(`第${index + 1}行预期必须是allow或deny`);
    return { name, target, kind: kind.toLowerCase(), port: port ? number(port) : undefined, expected: expected.toLowerCase() };
  });
}

function verdictFor(row, observed) {
  if (row.expected === "allow") {
    if (observed.ok) return { code: "ok", text: "符合" };
    if (observed.classification === "refused") return { code: "warn", text: "待确认" };
    return { code: "bad", text: "不符合" };
  }
  if (observed.ok) return { code: "bad", text: "不符合" };
  if (["timeout", "no_reply"].includes(observed.classification)) return { code: "ok", text: "符合" };
  return { code: "warn", text: "待确认" };
}

function renderACL(results) {
  const body = el("acl-results"); body.textContent = "";
  for (const item of results) {
    const tr = document.createElement("tr");
    const values = [item.name, item.target, item.kind === "tcp" ? `TCP ${item.port}` : "ICMP", item.expected.toUpperCase(), item.observed.classification, item.verdict.text, `${item.observed.elapsed_ms} ms`];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 5) { const tag = document.createElement("span"); tag.className = `verdict ${item.verdict.code}`; tag.textContent = value; td.appendChild(tag); }
      else td.textContent = value;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}

function aclUI() {
  el("acl-preset").addEventListener("click", () => {
    el("acl-matrix").value = [
      "PC1访问PC2远程桌面,192.168.4.100,tcp,3389,allow",
      "PC2反向访问PC1远程桌面,192.168.3.100,tcp,3389,deny",
      "PC2访问Gateway HTTPS,192.168.1.1,tcp,443,deny",
      "PC2访问Switch1 SSH,192.168.1.2,tcp,22,deny",
      "PC2 Ping本地网关,192.168.4.2,ping,,allow",
      "PC2 Ping管理设备,192.168.1.2,ping,,deny",
      "Guest访问内网Controller,192.168.1.11,tcp,443,deny",
      "Guest访问Internet DNS,1.1.1.1,tcp,443,allow",
    ].join("\n");
  });
  el("clear-acl").addEventListener("click", () => { el("acl-results").innerHTML = '<tr><td colspan="7" class="empty">暂无结果</td></tr>'; el("acl-summary").textContent = "尚未执行"; el("acl-score").querySelector("strong").textContent = "—"; });
  el("run-acl").addEventListener("click", async () => {
    let rows;
    try { rows = parseMatrix(el("acl-matrix").value); if (!rows.length) throw new Error("请先输入验证矩阵"); }
    catch (error) { toast(error.message, true); return; }
    const button = el("run-acl"); button.disabled = true; button.textContent = "执行中";
    const results = [];
    try {
      for (let index = 0; index < rows.length; index++) {
        el("acl-summary").textContent = `正在执行 ${index + 1}/${rows.length}`;
        const row = rows[index];
        let observed;
        try { observed = await api("/api/probe", { target: row.target, kind: row.kind, port: row.port, timeout: 2, count: 3 }); }
        catch (error) { observed = { ok: false, classification: "request_error", elapsed_ms: 0, error: error.message }; }
        results.push({ ...row, observed, verdict: verdictFor(row, observed) });
        renderACL(results);
      }
      const passed = results.filter(item => item.verdict.code === "ok").length;
      const uncertain = results.filter(item => item.verdict.code === "warn").length;
      const failed = results.filter(item => item.verdict.code === "bad").length;
      el("acl-summary").textContent = `${passed}项符合，${uncertain}项待确认，${failed}项不符合`;
      el("acl-score").querySelector("strong").textContent = `${Math.round(passed * 100 / results.length)}%`;
      saveRecord("acl", "ACL矩阵", { total: results.length, passed, uncertain, failed, results, measured_at: new Date().toISOString(), evidence_note: "Refused and missing baselines are not treated as ACL proof." });
    } finally { button.disabled = false; button.textContent = "执行矩阵"; }
  });
}

function multicastUI() {
  const form = el("multicast-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    let data;
    try { data = { ...formObject(form), ...currentPeerPayload() }; }
    catch (error) { toast(error.message, true); return; }
    data.port = number(data.port); data.duration = number(data.duration); data.rate_mbps = number(data.rate_mbps);
    try {
      const started = await api("/api/multicast/pair", data);
      pollJob("multicast", started.job_id, job => {
        el("multicast-state").textContent = `两端自动协同中 ${Math.round(job.progress * 100)}%`;
        const samples = job.samples || []; const last = samples.at(-1);
        el("multicast-live").firstChild.textContent = `${last ? number(last.mbps).toFixed(1) : "0.0"} `;
        drawChart(el("multicast-chart"), samples);
        if (last) { el("mc-packets").textContent = last.packets ?? "—"; el("mc-loss").textContent = last.lost === undefined ? "—" : `${last.lost}包`; }
      }, job => {
        if (job.status === "completed") {
          el("multicast-state").textContent = "两端任务完成"; el("mc-packets").textContent = job.result.packets ?? "—"; el("mc-loss").textContent = job.result.loss_percent === undefined ? "—" : `${job.result.loss_percent}%`; el("mc-ooo").textContent = job.result.out_of_order ?? "—";
          el("multicast-live").firstChild.textContent = `${number(job.result.average_mbps).toFixed(1)} `;
          saveRecord("multicast", "组播IPTV", job.result);
        } else { el("multicast-state").textContent = job.error || job.status; toast(job.error || "任务已停止", job.status === "failed"); }
      });
    } catch (error) { toast(error.message, true); }
  });
}

function dnsNatUI() {
  el("dns-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = formObject(event.currentTarget);
    data.port = number(data.port, 53);
    if (data.txid) data.txid = data.txid.toLowerCase().startsWith("0x") ? parseInt(data.txid, 16) : number(data.txid); else delete data.txid;
    el("dns-result").textContent = "正在查询";
    try { const result = await api("/api/dns", data); el("dns-result").textContent = JSON.stringify(result, null, 2); saveRecord("dns", "DNS查询", result); }
    catch (error) { el("dns-result").textContent = error.message; toast(error.message, true); }
  });
  el("nat-form").addEventListener("submit", async event => {
    event.preventDefault();
    let data;
    try {
      data = { ...currentPeerPayload(), port: peerConfig.echo_port };
    } catch (error) { toast(error.message, true); return; }
    el("nat-result").textContent = "正在连接";
    try { const result = await api("/api/nat", data); el("nat-result").textContent = JSON.stringify(result, null, 2); saveRecord("nat", "NAT元组", result); }
    catch (error) { el("nat-result").textContent = error.message; toast(error.message, true); }
  });
}

function monitorUI() {
  const form = el("monitor-form");
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = formObject(form); data.port = number(data.port); data.interval = number(data.interval); data.duration = number(data.duration);
    try {
      const started = await api("/api/monitor/start", data);
      pollJob("monitor", started.job_id, job => {
        const samples = job.samples || []; const last = samples.at(-1); drawChart(el("monitor-chart"), samples, "state");
        el("monitor-state").textContent = `监控中 ${Math.round(job.progress * 100)}%`; el("monitor-live").textContent = last ? (last.up ? "当前可达" : "当前中断") : "检测中";
      }, job => {
        if (job.status === "completed") {
          el("monitor-state").textContent = "监控完成"; el("mon-availability").textContent = `${job.result.availability_percent}%`; el("mon-outage").textContent = `${job.result.longest_outage_s}s`; el("mon-transitions").textContent = job.result.transitions.length;
          saveRecord("monitor", "连续监控", job.result);
        } else { el("monitor-state").textContent = job.error || job.status; toast(job.error || "监控已停止", job.status === "failed"); }
      });
    } catch (error) { toast(error.message, true); }
  });
}

function recordsUI() {
  function download(content, filename, type) {
    const blob = new Blob([content], { type });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob); link.download = filename; link.click(); URL.revokeObjectURL(link.href);
  }
  el("export-records").addEventListener("click", () => {
    const records = loadHistory(); const stamp = new Date().toISOString().replaceAll(":", "-");
    download(JSON.stringify({ app: "NetLab Assist", exported_at: new Date().toISOString(), records }, null, 2), `netlab-assist-${stamp}.json`, "application/json");
  });
  el("export-report").addEventListener("click", () => {
    const records = loadHistory();
    if (!records.length) { toast("暂无可导出的实验记录", true); return; }
    const lines = ["# NetLab Assist 实验记录", "", `导出时间：${new Date().toLocaleString()}`, "", "> 请与拓扑、设备配置、端口计数器、截图及必要的 pcap 一起归档。", ""];
    records.forEach((record, index) => lines.push(`## ${index + 1}. ${record.title}`, "", `时间：${new Date(record.saved_at).toLocaleString()}`, "", `摘要：${resultSummary(record)}`, "", "```json", JSON.stringify(record.data, null, 2), "```", ""));
    download(lines.join("\n"), `netlab-assist-report-${new Date().toISOString().replaceAll(":", "-")}.md`, "text/markdown;charset=utf-8");
  });
  el("clear-records").addEventListener("click", () => { if (confirm("只清空当前浏览器中的实验记录，已导出的文件不会删除。确认继续吗？")) { localStorage.removeItem(historyKey); renderRecords(); } });
}

function showPage(page) {
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === page));
    document.querySelectorAll(".page").forEach(item => item.classList.toggle("active", item.id === `page-${page}`));
    el("page-title").textContent = titles[page];
    if (page === "records") renderRecords();
    requestAnimationFrame(() => { document.querySelectorAll("canvas").forEach(canvas => drawChart(canvas, [])); });
}

function navigationUI() {
  el("navigation").addEventListener("click", event => {
    const button = event.target.closest("[data-page]"); if (!button) return;
    showPage(button.dataset.page);
  });
}

async function initialize() {
  navigationUI(); pairingUI(); throughputUI(); aclUI(); multicastUI(); dnsNatUI(); monitorUI(); recordsUI();
  document.querySelectorAll("[data-stop-job]").forEach(button => button.addEventListener("click", stopActiveJobs));
  drawChart(el("throughput-chart"), []); drawChart(el("multicast-chart"), []); drawChart(el("monitor-chart"), [], "state"); renderRecords(); applyPeerState();
  try {
    const status = await api("/api/status");
    localStatus = status;
    const primaryIp = status.local_ips.find(ip => !ip.startsWith("127.")) || status.local_ips[0];
    el("local-ip").textContent = `${primaryIp}:${status.ui_port}`;
    el("local-node-name").textContent = status.hostname;
    el("app-version").textContent = `v${status.version}`;
    const ipList = el("local-ip-list");
    ipList.textContent = "";
    status.local_ips.filter(ip => !ip.startsWith("127.")).forEach(ip => {
      const tag = document.createElement("span");
      tag.textContent = `${ip}:${status.ui_port}`;
      ipList.appendChild(tag);
    });
    if (!ipList.children.length) {
      const tag = document.createElement("span"); tag.textContent = `127.0.0.1:${status.ui_port}`; ipList.appendChild(tag);
    }
    el("copy-code").textContent = status.access_code;
    el("copy-code").addEventListener("click", async () => { await navigator.clipboard.writeText(status.access_code); toast("实验码已复制"); });
    try {
      const restored = JSON.parse(sessionStorage.getItem(peerSessionKey) || "null");
      if (restored?.target && restored?.peer_token) {
        const form = el("pair-form");
        form.elements.target.value = restored.target;
        form.elements.peer_token.value = restored.peer_token;
        form.elements.peer_ui_port.value = restored.peer_ui_port || 18080;
        await checkPeer({ target: restored.target, peer_token: restored.peer_token, peer_ui_port: restored.peer_ui_port || 18080 }, true);
      }
    } catch { sessionStorage.removeItem(peerSessionKey); }
  } catch (error) { toast(`无法读取本机状态：${error.message}`, true); }

  setInterval(async () => {
    const state = el("service-state");
    try {
      await api("/api/status");
      state.classList.remove("offline");
      state.querySelector("span").textContent = "服务正常";
    } catch {
      state.classList.add("offline");
      state.querySelector("span").textContent = "服务断开";
    }
  }, 5000);
}

initialize();
