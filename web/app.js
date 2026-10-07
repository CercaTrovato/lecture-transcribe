/* Lecture Transcribe 前端：录音控制 / 实时预览 / 历史库 / 播放高亮 */
const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) { let d = "", action; try { const body = await r.json(); d = body.detail; action = body.action; } catch {} const e = new Error(typeof d === "string" ? d : JSON.stringify(d || r.statusText)); e.status = r.status; e.action = action; throw e; }
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const hms = (s) => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? String(h).padStart(2, "0") + ":" : "") + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0"); };
const STATUS_TEXT = { recording: "录音中", finalizing: "收尾中", importing: "导入中", recorded: "待转录", queued: "排队中", transcribing: "转录中", pausing: "正在暂停", paused: "已暂停", cancelled: "已取消", done: "已完成", error: "失败" };
const ACTIVE_JOBS = ["queued", "running", "pausing", "paused"];
const LANG_NAMES = { auto: "自动检测", zh: "中文", en: "英语" };

const state = {
  current: null,        // 当前展示的录音 id
  recState: "idle",
  recRid: null,
  liveCount: 0,         // 已拉取的实时词数
  liveWords: [],
  livePending: "",
  liveSents: [],        // 实时成句 + 译文：liveSents[k] = {k, ws, we, en, zh}
  liveSentTotal: 0,
  lang: "both",         // both | en | zh
  segments: [],
  activeIdx: -1,
  list: [],
  job: null,
  courses: [],
};

// ---------------------------------------------------------------- 历史列表
async function loadList() {
  state.list = await api("/api/recordings");
  renderList();
}
function renderList() {
  const ul = $("rec-list");
  ul.innerHTML = "";
  for (const m of state.list) {
    const li = document.createElement("li");
    li.className = m.id === state.current ? "active" : "";
    const recording = m.id === state.recRid && state.recState !== "idle";
    const status = m.id === state.stoppingRid ? "finalizing" : m.status;
    li.innerHTML = `<div class="t">${esc(m.name)}</div>
      <div class="s"><span class="dot ${status}"></span>${m.course ? esc(m.course) + " · " : ""}${m.created.replace("T", " ").slice(0, 16)} · ${hms(m.duration)} · ${STATUS_TEXT[status] || status}</div>` +
      (recording ? "" : `<button class="del" title="删除这条录音">${TRASH_SVG}</button>`);   // 悬停显示；正在录的那条不能删
    li.onclick = () => select(m.id);
    const del = li.querySelector(".del");
    if (del) del.onclick = (ev) => { ev.stopPropagation(); deleteRecording(m.id, m.name); };
    ul.appendChild(li);
  }
  if (!state.list.length) ul.innerHTML = `<li class="empty">还没有录音</li>`;
}
const TRASH_SVG = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg>`;
async function deleteRecording(id, name) {
  if (!confirm(`删除「${name}」及其音频、转录、翻译？不可恢复。`)) return;
  try { await api(`/api/recordings/${id}`, { method: "DELETE" }); } catch (e) { alert("删除失败：" + e.message); return; }
  if (state.current === id) { state.current = null; $("tr-name").textContent = "未选择录音"; $("tr-status").textContent = ""; $("tr-actions").hidden = true; $("segments").innerHTML = ""; $("audio").removeAttribute("src"); }
  await loadList();
  if (!state.current && state.list.length) select(state.list[0].id);
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- 展示一条录音
async function select(rid) {
  if (rid !== state.current) { state.segments = []; state.activeIdx = -1; state.liveWords = []; state.liveCount = 0; state.livePending = ""; state.liveSents = []; state.liveSentTotal = 0; state.translation = null; $("segments").innerHTML = ""; }  // 换记录时清掉上一条的内容
  state.current = rid;
  if ($("tr-draft").dataset.rid !== rid) $("tr-draft").dataset.rid = "";
  await refreshCurrent();
  await loadList();
}

async function refreshCurrent() {
  if (!state.current) return;
  let d;
  try {
    d = await api(`/api/recordings/${state.current}`);
  } catch (e) {
    if (e.status !== 404) return;      // 服务暂时不通：保留当前内容，下一轮再试
    const gone = state.current;        // 记录已被删（取消导入 / 另一个标签页删的）：换一条，别留着旧内容
    state.current = null;
    await loadList();
    const next = state.list.find((m) => m.id !== gone);
    if (next) select(next.id);
    else { $("tr-name").textContent = "未选择录音"; $("tr-status").textContent = ""; $("tr-actions").hidden = true; $("segments").innerHTML = ""; $("audio").removeAttribute("src"); }
    return;
  }
  if (d.id !== state.current) return;
  state.job = d.job;
  $("tr-name").textContent = d.name;
  const isRec = state.recRid === d.id && state.recState !== "idle";
  const badge = $("tr-status");
  badge.className = "badge " + (isRec ? (state.recState === "finalizing" ? "busy" : "rec") : d.status === "done" ? "done" : d.status === "error" ? "err" : "busy");
  badge.textContent = isRec ? (state.recState === "finalizing" ? "收尾中 · 归一化" : state.recState === "paused" ? "已暂停" : "录音中 · 实时预览") : (d.provisional && d.status !== "done" ? `${STATUS_TEXT[d.status]} · 显示实时预览` : STATUS_TEXT[d.status] || d.status);
  $("tr-actions").hidden = isRec;
  $("btn-view").hidden = isRec;
  const activeJob = [d.job, d.translate_job].find((x) => x && ACTIVE_JOBS.includes(x.status));
  state.controlJob = activeJob || null;
  $("btn-retranscribe").disabled = !!activeJob || ["importing", "recording", "finalizing"].includes(d.status);
  $("tr-lang").disabled = $("btn-retranscribe").disabled;
  if ($("tr-lang").dataset.rid !== d.id) { $("tr-lang").value = d.lang || "auto"; $("tr-lang").dataset.rid = d.id; }
  $("btn-delete").disabled = !!activeJob;
  $("btn-cancel-import").hidden = d.status !== "importing";
  $("btn-job-pause").hidden = !activeJob || ["paused", "pausing"].includes(activeJob.status);
  $("btn-job-resume").hidden = !activeJob || !["paused", "pausing"].includes(activeJob.status);
  $("btn-job-resume").textContent = activeJob?.status === "pausing" ? "撤销暂停" : "继续任务";
  $("btn-job-cancel").hidden = !activeJob;
  for (const id of ["btn-job-pause", "btn-job-resume", "btn-job-cancel"]) $(id).disabled = !!activeJob?.cancel_requested;
  for (const k of ["md", "srt", "txt"]) { const a = $("dl-" + k); a.href = `/api/recordings/${d.id}/file/${k}`; a.style.display = d.files[k] ? "" : "none"; }
  $("btn-copy-path").dataset.path = d.files.txt || "";
  $("btn-copy-path").style.display = d.files.txt ? "" : "none";
  const course = state.courses.find((c) => c.code === d.course);
  $("btn-export").hidden = !(d.files.txt && course && course.transcripts_dir);
  $("btn-export").textContent = d.exported ? `已导出 ${d.module}（再导出）` : `导出到知识库 ${d.course || ""}`;
  $("btn-export").dataset.module = d.module || (course && course.next_module) || "";
  const j = d.job;
  state.sourceLanguage = d.detected_lang || d.lang;
  $("tr-meta").textContent = [d.created.replace("T", " "), d.course ? `课程 ${d.course}` : "", `时长 ${hms(d.duration)}`, d.translation_status ? `翻译: ${{ queued: "排队", running: "进行中", done: "完成", partial: "部分失败", error: "失败" }[d.translation_status] || d.translation_status}` : "", d.hotwords ? `术语: ${d.hotwords}` : "", d.elapsed ? `转录耗时 ${d.elapsed < 60 ? Math.round(d.elapsed) + "s" : hms(d.elapsed)}` : "", d.error ? `错误: ${d.error}` : ""].filter(Boolean).join(" · ");
  state.translation = d.translation || null;
  const hasDraft = (d.live_segments || []).some(s => s.zh);
  const draft = $("tr-draft");
  draft.hidden = isRec || !hasDraft;
  if (draft.dataset.rid !== d.id) { draft.value = d.translation ? "final" : hasDraft ? "live" : "final"; draft.dataset.rid = d.id; }
  const showingDraft = !isRec && hasDraft && draft.value === "live";
  state.draftLive = showingDraft;
  if (showingDraft) state.translation = null;
  $("draft-note").textContent = showingDraft ? "实时识别与译文，非最终整篇精译；完整原文可通过稿件类型切换。" : d.translation ? "最终转录与精译稿" : "";
  const tj = d.translate_job, tRunning = tj && ACTIVE_JOBS.includes(tj.status);
  $("dl-zh").hidden = !d.files.zh_md; $("dl-zh").href = `/api/recordings/${d.id}/file/zh_md`;
  $("dl-live-zh").hidden = !d.files.live_md; $("dl-live-zh").href = `/api/recordings/${d.id}/file/live_md`;
  $("btn-translate").hidden = !(d.segments.length && d.status === "done") || isRec || state.sourceLanguage === "zh";
  $("btn-translate").disabled = !!tRunning;
  $("btn-translate").textContent = tRunning ? (tj.phase || "翻译中") : (d.translation ? "重新翻译" : "翻译");
  if (tRunning && !(j && ACTIVE_JOBS.includes(j.status))) {
    $("tr-progress").hidden = false; $("tr-progress-fill").style.width = (tj.progress * 100) + "%";
    $("tr-progress-text").textContent = `${tj.phase || "翻译"} ${(tj.progress * 100).toFixed(0)}%` + (tj.eta != null ? ` · 剩余约 ${hms(tj.eta)}` : "");
  }
  state.dropped = d.dropped || [];
  $("btn-dropped").hidden = !state.dropped.length;
  $("btn-dropped").textContent = `${state.showDropped ? "隐藏" : "显示"}已过滤噪声 (${state.dropped.length})`;

  // 进度条
  const running = j && ACTIVE_JOBS.includes(j.status);
  $("tr-progress").hidden = !running && !tRunning;   // 翻译任务的进度在上面已填好，这里别盖掉
  if (running) {
    $("tr-progress-fill").style.width = (j.progress * 100) + "%";
    const eta = j.eta != null ? ` · 剩余约 ${hms(j.eta)}` : "";
    $("tr-progress-text").textContent = j.status === "queued" ? "排队中…" : `${j.phase || "转录"} ${(j.progress * 100).toFixed(0)}%${eta}`;
  }
  if (activeJob) {
    const ctl = activeJob;
    const elapsed = ctl.started ? ` · 处理用时 ${hms(ctl.active_elapsed ?? (Date.now() / 1000 - ctl.started))}` : "";
    const stale = ctl.updated && Date.now() / 1000 - ctl.updated > 15;
    const eta = ctl.eta != null && !stale ? ` · 预计剩余 ${hms(ctl.eta)}` : " · 正在处理，剩余时间待更新";
    $("tr-progress-text").textContent = ctl.cancel_requested ? "正在取消，等待当前批次结束…" :
      ctl.status === "paused" ? `已暂停 · ${ctl.phase} · 继续后接着处理` :
      ctl.status === "pausing" ? "正在暂停，等待当前批次结束…" :
      ctl.status === "queued" ? "排队中…" : `${ctl.phase} ${(ctl.progress * 100).toFixed(0)}%${elapsed}${eta}`;
  }
  $("tr-meta").textContent += ` · 音频语言：${LANG_NAMES[d.lang] || d.lang}` + (d.lang === "auto" && d.detected_lang ? `（${LANG_NAMES[d.detected_lang] || d.detected_lang}）` : "");

  // 音频
  const audio = $("audio");
  const src = location.origin + d.audio;
  if (!isRec && !["recording", "importing"].includes(d.status)) { if (!audio.src.startsWith(src)) { audio.src = src + "?v=" + (d.transcribed || d.created); } audio.style.visibility = ""; }
  else { audio.removeAttribute("src"); audio.load(); }

  if (!isRec) renderSegments(showingDraft ? d.live_segments : d.segments, showingDraft || d.provisional);
  else if (d.live !== false) renderLive();
}
function mergedSegments(segs) {
  // 显示被过滤段时，把它们按时间插回去（带 dropped 标记），播放高亮仍只跟正式段走
  if (!state.showDropped || !state.dropped.length) return segs;
  return [...segs, ...state.dropped.map((s) => ({ ...s, dropped: true }))].sort((a, b) => a.start - b.start);
}

// 视图：段落流式（换气 / 短停顿不换行，只在句末长停顿处分段）或逐句一行。实时预览固定用段落。
const SENT_END = /[.?!。！？…]["'”’)\]]?$/;
const PARA_SOFT = 75, PARA_HARD = 120;   // 段落时长：超过 75s 在下一个句末切；超过 120s 无论如何切（防无标点模式下无限长）
function zhBlock(zh, state) {
  // zh: 字符串 | null(翻译中) | ""(失败)；state: "waiting" | "failed" | ""
  if (zh === undefined) return "";
  if (zh === null) return `<div class="zh waiting">翻译中…</div>`;
  if (zh === "") return `<div class="zh failed">（翻译失败）</div>`;
  return `<div class="zh">${esc(zh)}</div>`;
}
function paraEl(p, provisional) {
  const el = document.createElement("div");
  el.className = "para" + (provisional ? " provisional" : "");
  el.innerHTML = `<span class="ts" title="${hms(p.start)} – ${hms(p.end)}">${hms(p.start)}<br><small>– ${hms(p.end)}</small></span><span class="ptext"><span class="en">` + p.segs.map((s) =>
    `<span class="w${s.dropped ? " dropped" : ""}" data-i="${s.dropped ? -1 : s.i}" title="${hms(s.start)}${s.dropped ? " 已过滤：" + esc(s.reason) : ""}">${esc(s.text)}</span>`).join(" ") + `</span>${zhBlock(p.zh)}</span>`;
  el.querySelectorAll(".w").forEach((w, k) => { const seg = p.segs[k]; w.onclick = () => { const a = $("audio"); if (a.src) { a.currentTime = seg.start; a.play(); } }; });
  return el;
}
function liveForced() { return !!(state.current && state.recRid === state.current && state.recState !== "idle"); }
function viewMode() { return liveForced() || state.draftLive ? "para" : (state.view || "line"); }
function renderSegments(segs, provisional) {
  state.segments = segs;
  state.activeIdx = -1;
  const box = $("segments");
  const all = mergedSegments(segs);
  if (!all.length) { box.innerHTML = `<div class="empty">${provisional === undefined ? "" : "暂无转录内容"}</div>`; return; }
  const frag = document.createDocumentFragment();
  if (viewMode() === "para") {
    const tr = state.translation;
    if (tr && tr.paras && tr.paras.length && !state.showDropped) {
      // 有最终版翻译：用服务端切好的段落（与译文对齐），英文 + 中文
      for (const p of tr.paras) frag.appendChild(paraEl({ start: p.start, end: p.end, segs: p.idx.map((i) => ({ ...segs[i], i })), zh: p.zh || "" }, provisional));
    } else {
      let k = 0;
      const indexed = all.map((s) => ({ ...s, i: s.dropped ? -1 : k++ }));
      for (const p of buildParagraphsIndexed(indexed)) frag.appendChild(paraEl(p, provisional));
    }
  } else {
    let prevEnd = null, k = 0;
    for (const s of all) {
      frag.appendChild(segEl(s, s.dropped ? -1 : k++, provisional, prevEnd !== null && s.start - prevEnd > 1.8));
      prevEnd = Math.max(prevEnd ?? 0, s.end);
    }
  }
  box.innerHTML = "";
  box.appendChild(frag);
  $("search").oninput();
  $("btn-view").textContent = (state.view || "line") === "para" ? "切换为逐句" : "切换为段落";
  $("btn-view").hidden = liveForced() || state.draftLive;
  applyLang();
}
function buildParagraphsIndexed(segs) {
  // 实时预览段（live_segments.json）带有逐句译文 zh：拼成段落译文，结束录音后到最终版翻译出来之前继续显示中文
  return splitParagraphs(segs, (s) => s.start, (s) => s.end, (s) => s.text).map((p) => ({ start: p.start, end: p.end, segs: p.items, zh: p.items.some((s) => s.zh) ? p.items.map((s) => s.zh || "").join("") : undefined }));
}
// 通用分段：句末 + 停顿分段；超过 PARA_SOFT 秒还没句末时，回溯最近 LOOKBACK 秒在"最长停顿（≥ MIN_PAUSE）"处切；
// 连停顿都没有才等到 PARA_HARD 硬切（理论兜底，实际几乎不会触发）
const LOOKBACK = 40, MIN_PAUSE = 0.6;
const GAP_HARD = 3.0, GAP_SOFT = 1.8;   // 停顿 ≥3s 无条件分段；≥1.8s 且上一句已结束则分段
function splitParagraphs(items, getS, getE, getT) {
  const paras = [];
  let cur = null;
  for (const it of items) {
    const s = getS(it), e = getE(it);
    const prev = cur && cur.items[cur.items.length - 1];
    const gap = prev ? s - getE(prev) : 0;
    const sentEnd = prev ? SENT_END.test(getT(prev)) : true;
    let brk = !cur || gap >= GAP_HARD || (gap >= GAP_SOFT && sentEnd) || (sentEnd && s - cur.start >= PARA_SOFT) || s - cur.start >= PARA_HARD;
    if (!brk && cur && s - cur.start >= PARA_SOFT) {
      // 超时且不在句末：在当前段最近 LOOKBACK 秒里找最长停顿，回溯切开
      let best = -1, bestGap = MIN_PAUSE;
      for (let j = 1; j < cur.items.length; j++) {
        if (getS(cur.items[j]) < s - LOOKBACK) continue;
        const g = getS(cur.items[j]) - getE(cur.items[j - 1]);
        if (g >= bestGap) { bestGap = g; best = j; }
      }
      if (best > 0) {
        const tail = cur.items.splice(best);
        cur.end = getE(cur.items[cur.items.length - 1]);
        cur = { start: getS(tail[0]), end: getE(tail[tail.length - 1]), items: tail };
        paras.push(cur);
      }
    }
    if (brk) { cur = { start: s, end: e, items: [] }; paras.push(cur); }
    cur.items.push(it);
    cur.end = Math.max(cur.end, e);
  }
  return paras;
}
function segEl(s, i, provisional, paraBreak) {
  const el = document.createElement("div");
  el.className = "seg" + (provisional ? " provisional" : "") + (paraBreak ? " para-break" : "") + (s.dropped ? " dropped" : "");
  el.dataset.i = i;
  el.title = s.dropped ? `已过滤：${s.reason}` : "";
  el.innerHTML = `<span class="ts">${hms(s.start)}</span><span class="tx">${esc(s.text)}${s.dropped ? `<span class="why">${esc(s.reason)}</span>` : ""}</span>`;
  el.onclick = () => { const a = $("audio"); if (a.src) { a.currentTime = s.start; a.play(); } };
  return el;
}
// 实时（v2）：已定稿的词按"句末 + 长停顿"分段流式排版，未定稿的词以灰字挂在段尾
function liveParagraphs(words) {
  // 每个词带上下标 i（= 服务端 live.words 的下标），liveZh 用它把句子（ws）归到段落
  return splitParagraphs(words.map((w, i) => ({ ...w, i })), (w) => w.s, (w) => w.e, (w) => w.w).map((p) => ({ start: p.start, end: p.end, words: p.items }));
}
function liveZh(p) {
  // 段落里各句的译文按顺序拼接；有句子还在翻译 → null；全部失败 → ""
  const lo = p.words[0].i, hi = p.words[p.words.length - 1].i;
  const sents = state.liveSents.filter((s) => s && s.ws >= lo && s.ws <= hi);
  if (!sents.length) return undefined;
  if (sents.some((s) => s.zh === null)) { const done = sents.filter((s) => s.zh).map((s) => s.zh).join(""); return done ? done + " …" : null; }
  const zh = sents.map((s) => s.zh).filter(Boolean).join("");
  return zh;
}
function liveParaEl(p, pending) {
  const el = document.createElement("div");
  el.className = "para live";
  el.innerHTML = `<span class="ts">${hms(p.start)}<br><small>– ${hms(p.end)}</small></span><span class="ptext"><span class="en">${esc(p.words.map((w) => w.w).join(" "))}${pending ? ` <span class="pending">${esc(pending)}</span>` : ""}</span>${zhBlock(liveZh(p))}</span>`;
  return el;
}
function renderLive() {
  const box = $("segments");
  const paras = liveParagraphs(state.liveWords);
  if (!paras.length) {
    box.innerHTML = `<div class="para live"><span class="ts"></span><span class="ptext"><span class="pending">${esc(state.livePending || "")}</span></span></div>`;
    if (!state.livePending) box.innerHTML = `<div class="empty">正在等待实时识别结果，模型加载或性能受限时可能需要更久（灰字为未定稿；结束后用完整转录替换）<br><small>录音在本机服务端进行，关闭此页面不会中断</small></div>`;
    applyLang();
    return;
  }
  const nodes = box.querySelectorAll(".para.live");
  if (!nodes.length || (nodes.length !== paras.length - 1 && nodes.length !== paras.length)) { box.innerHTML = ""; paras.forEach((p, k) => box.appendChild(liveParaEl(p, k === paras.length - 1 ? state.livePending : ""))); }
  else {
    if (nodes.length === paras.length) nodes[nodes.length - 1].replaceWith(liveParaEl(paras[paras.length - 1], state.livePending));
    else { if (nodes.length) nodes[nodes.length - 1].replaceWith(liveParaEl(paras[paras.length - 2], "")); box.appendChild(liveParaEl(paras[paras.length - 1], state.livePending)); }
    // 译文是异步补上的：已渲染的段里内容有变化的（一般只有最近几段）重绘
    const cur = box.querySelectorAll(".para.live");
    for (let k = 0; k < paras.length - 1; k++) {
      const want = liveParaEl(paras[k], "").innerHTML;
      if (cur[k] && cur[k].innerHTML !== want) cur[k].replaceWith(liveParaEl(paras[k], ""));
    }
  }
  applyLang();
  if ($("follow").checked) box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 响度均衡（Web Audio 压缩器，只作用于播放，不改文件）
const leveler = { ctx: null, src: null, comp: null, gain: null, on: false };
function setLeveler(on) {
  const a = $("audio");
  if (!leveler.ctx) {
    try {
      leveler.ctx = new (window.AudioContext || window.webkitAudioContext)();
      leveler.src = leveler.ctx.createMediaElementSource(a);
      leveler.comp = leveler.ctx.createDynamicsCompressor();
      leveler.comp.threshold.value = -40; leveler.comp.knee.value = 20; leveler.comp.ratio.value = 6;
      leveler.comp.attack.value = 0.005; leveler.comp.release.value = 0.3;
      leveler.gain = leveler.ctx.createGain(); leveler.gain.gain.value = 2.2;   // 压缩后补偿增益
      leveler.comp.connect(leveler.gain);
    } catch (e) { $("leveler").checked = false; return; }
  }
  try { leveler.src.disconnect(); leveler.gain.disconnect(); } catch {}
  if (on) { leveler.src.connect(leveler.comp); leveler.gain.connect(leveler.ctx.destination); }
  else { leveler.src.connect(leveler.ctx.destination); }
  leveler.on = on;
  if (leveler.ctx.state === "suspended") leveler.ctx.resume().catch(() => {});
  try { localStorage.setItem("lt.leveler", on ? "1" : "0"); } catch {}
}
$("leveler").onchange = () => setLeveler($("leveler").checked);
$("audio").addEventListener("play", () => { if ($("leveler").checked && !leveler.on) setLeveler(true); if (leveler.ctx && leveler.ctx.state === "suspended") leveler.ctx.resume().catch(() => {}); });
try { if (localStorage.getItem("lt.leveler") === "1") $("leveler").checked = true; } catch {}

// ---------------------------------------------------------------- 播放高亮
$("audio").addEventListener("timeupdate", () => {
  const t = $("audio").currentTime, segs = state.segments;
  if (!segs.length) return;
  let i = state.activeIdx;
  if (i < 0 || t < segs[i].start || t >= (segs[i + 1]?.start ?? Infinity)) {
    let lo = 0, hi = segs.length - 1; i = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (segs[mid].start <= t) { i = mid; lo = mid + 1; } else hi = mid - 1; }
  }
  if (i !== state.activeIdx) {
    document.querySelector(".seg.active, .w.active")?.classList.remove("active");
    state.activeIdx = i;
    const el = document.querySelector(`.seg[data-i="${i}"], .w[data-i="${i}"]`);
    if (el) { el.classList.add("active"); if ($("follow").checked) el.scrollIntoView({ block: "center" }); }
  }
});

// ---------------------------------------------------------------- 课程（courses.json，由知识库 agent 生成）
async function loadCourses() {
  state.courses = await api("/api/courses");
  const sel = $("rec-course");
  sel.innerHTML = `<option value="">（不选课程）</option>` + state.courses.map((c) => `<option value="${esc(c.code)}">${esc(c.code)} ${esc(c.name)}</option>`).join("");
  let last = ""; try { last = localStorage.getItem("lt.course") || ""; } catch {}
  if (state.courses.some((c) => c.code === last)) { sel.value = last; applyCourse(false); }
}
function applyCourse(overwrite) {
  const c = state.courses.find((x) => x.code === $("rec-course").value);
  try { localStorage.setItem("lt.course", c ? c.code : ""); } catch {}
  if (!c) return;
  // 只取前 12 条：实测少量术语提升明显（错 8→3），太多反而变慢、诱发幻觉；转录侧还有 64 token 硬上限
  if (overwrite || !$("rec-hotwords").value.trim()) $("rec-hotwords").value = c.hotwords.split(",").map((x) => x.trim()).filter(Boolean).slice(0, 12).join(", ");
  if (overwrite || !$("rec-name").value.trim()) $("rec-name").value = c.next_module ? `${c.code}_${c.next_module}` : c.code;
  if (c.notes) $("rec-hint").textContent = c.notes;
}
$("rec-course").onchange = () => applyCourse(true);

// ---------------------------------------------------------------- 录音控制
async function loadDevices() {
  const devs = await api("/api/devices");
  const sel = $("rec-device");
  sel.innerHTML = devs.map((d) => `<option value="${d.index}" ${d.default && d.api.includes("WASAPI") ? "selected" : ""}>${esc(d.name)} (${d.api})</option>`).join("");
}
function setRecUI(st) {
  state.recState = st;
  $("btn-start").hidden = st !== "idle";
  $("btn-pause").hidden = st !== "recording";
  $("btn-resume").hidden = st !== "paused";
  $("btn-stop").hidden = !["recording", "paused"].includes(st);
  for (const id of ["rec-course", "rec-name", "rec-hotwords", "rec-device", "rec-live", "rec-live-mt", "rec-mt", "rec-asr"]) $(id).disabled = st !== "idle";
  if (st === "idle") { $("rec-level").style.width = "0"; $("rec-hint").textContent = ""; $("rec-timer").textContent = "00:00:00"; $("rec-timer").classList.remove("paused"); }
  $("rec-lang").disabled = st !== "idle";
  applyAudioLanguage();
}
$("btn-start").onclick = async () => {
  // 导入 / 转录在跑时录音会互相拖慢（共用 GPU 和 CPU），先提醒一句
  const busy = state.list.filter((m) => m.status === "importing").length;
  if (busy || state.jobsRunning) {
    const what = [busy ? "导入" : "", state.jobsRunning ? "转录/翻译任务" : ""].filter(Boolean).join(" 和 ");
    if (!confirm(`当前有${what}在跑，实时转录会变慢（共用 GPU/CPU）。\n转录任务会自动等录音结束再继续，导入不会。\n仍要开始录音吗？`)) return;
  }
  try {
    const wanted = [];
    if ($("rec-live").checked) wanted.push($("rec-asr").value || "asr-turbo");
    if ($("rec-live").checked && $("rec-live-mt").checked && $("rec-lang").value !== "zh") wanted.push("mt-live");
    if ($("rec-mt").checked && $("rec-lang").value !== "zh") wanted.push("mt-final");
    if (!(await requireModels(wanted))) return;
    $("btn-start").disabled = true;
    const m = await post("/api/recorder/start", { name: $("rec-name").value, device: $("rec-device").value ? +$("rec-device").value : null, hotwords: $("rec-hotwords").value, lang: $("rec-lang").value, live: $("rec-live").checked, course: $("rec-course").value, live_translate: $("rec-live-mt").checked, translate: $("rec-mt").checked, model_id: $("rec-asr").value || null });
    state.recRid = m.id; state.liveCount = 0; state.liveWords = []; state.livePending = ""; state.liveSents = []; state.liveSentTotal = 0; setRecUI("recording");
    await select(m.id);
    if ($("rec-live").checked) renderLive();
    else $("segments").innerHTML = `<div class="empty">未开启实时转录，结束后统一转录<br><small>录音在本机服务端进行，关闭此页面不会中断</small></div>`;
  } catch (e) { alert("开始录音失败：" + e.message); }
  finally { $("btn-start").disabled = false; }
};
$("btn-pause").onclick = async () => { await post("/api/recorder/pause"); setRecUI("paused"); refreshCurrent(); };
$("btn-resume").onclick = async () => { await post("/api/recorder/resume"); setRecUI("recording"); refreshCurrent(); };
function markFinalizing() {
  // 结束录音的请求返回前（转完剩余实时块 + 翻译收尾 + 归一化，长录音要几十秒），标题栏别再显示"录音中"
  setRecUI("finalizing");
  renderList();
  if (state.current && state.current === state.recRid) { const b = $("tr-status"); b.className = "badge busy"; b.textContent = "收尾中 · 归一化"; }
  document.title = "收尾中 · Lecture Transcribe";
}
$("btn-stop").onclick = async () => {
  state.stoppingRid = state.recRid;
  $("btn-stop").disabled = true; $("rec-hint").textContent = "正在收尾（转完剩余实时块 + 归一化）…";
  markFinalizing();
  try { const r = await post("/api/recorder/stop"); state.stoppingRid = null; state.recRid = null; setRecUI("idle"); await select(r.recording.id); }
  catch (e) { state.stoppingRid = null; alert("结束失败：" + e.message); }
  finally { $("btn-stop").disabled = false; }
};

// ---------------------------------------------------------------- 轮询
async function poll() {
  try {
    const s = await api("/api/status");
    if (!state.booted) await boot();
    const pill = $("model-pill");
    if (s.model.error) { pill.className = "pill err"; pill.textContent = "模型加载失败: " + s.model.error; }
    else if (s.model.ready) { pill.className = "pill ok"; pill.textContent = `模型就绪 · ${s.model.name}` + (s.jobs.length ? ` · ${s.jobs.length} 个任务` : ""); }
    else { pill.className = "pill"; pill.textContent = s.model.installed ? "模型已安装 · 使用时加载" : "仅录音模式 · 转录模型未安装"; }
    if (!state.modelTick || ++state.modelTick % 3 === 0 || $("model-manager").open) { state.modelTick = state.modelTick || 1; await loadModelStatus(); }

    const r = state.stoppingRid ? { ...s.recorder, state: "finalizing", rid: state.stoppingRid } : s.recorder;
    if (r.state !== "idle" && (state.recState !== r.state || state.recRid !== r.rid)) {
      if (state.recRid !== r.rid) { state.liveCount = 0; state.liveWords = []; state.livePending = ""; state.liveSents = []; state.liveSentTotal = 0; }
      state.recRid = r.rid; setRecUI(r.state); if (state.current !== r.rid) await select(r.rid);
      await loadList();
    }  // 页面刷新 / 另一窗口操作后恢复
    if (r.state === "idle" && state.recState !== "idle") { state.recRid = null; setRecUI("idle"); await refreshCurrent(); await loadList(); }
    document.title = r.state === "finalizing" ? "收尾中 · Lecture Transcribe" : r.state === "recording" ? `● ${hms(r.elapsed)} 录音中 · Lecture Transcribe` : r.state === "paused" ? `⏸ 已暂停 · Lecture Transcribe` : s.jobs.length ? `转录中 · Lecture Transcribe` : "Lecture Transcribe";
    if (r.state === "idle" && s.jobs.length && s.jobs.every((j) => j.status === "paused")) document.title = "任务已暂停 · Lecture Transcribe";
    $("rec-timer").classList.toggle("paused", r.state === "paused");
    if (r.state !== "idle") {
      $("rec-timer").textContent = hms(r.elapsed);
      $("rec-level").style.width = Math.min(100, r.peak * 100 / 0.6) + "%";
      $("rec-hint").textContent = r.state === "finalizing" ? "正在收尾（转完剩余实时块 + 归一化）…" : r.state === "paused" ? "已暂停" : r.peak < 0.02 ? "声音偏小：靠近麦克风或调高输入音量" : s.live.busy ? "实时转录中…" : "";
      if (s.live.active && r.state === "recording" && r.peak >= 0.02 && s.live.latency) $("rec-hint").textContent = `实时转录中（${s.live.model}）· 定稿延迟 ${s.live.latency.toFixed(1)}s`;
    }
    // 当前录音有任务在跑 → 刷新进度；任务刚结束 → 重新加载
    const j = state.job;
    if (state.current && r.rid !== state.current) {
      const running = s.jobs.some((x) => x.rid === state.current);
      if (running || (j && ACTIVE_JOBS.includes(j.status)) || state.tRunning) { await refreshCurrent(); if (!running) loadList(); }
      state.tRunning = s.jobs.some((x) => x.rid === state.current && x.kind === "translate");
    }
    state.jobsRunning = s.jobs.length > 0;
    if (s.mt && s.mt.error) $("rec-hint").textContent = "翻译模型: " + s.mt.error;
    // 有任务 / 录音 / 导入进行中时，每 5 秒刷一次左栏状态
    if ((s.jobs.length || r.state !== "idle" || state.list.some((m) => m.status === "importing")) && (state.tick = (state.tick || 0) + 1) % 5 === 0) loadList();
  } catch (e) {
    if (state.quit) return;   // 自己点的退出：不再轮询，也不报错
    $("model-pill").className = "pill err"; $("model-pill").textContent = "服务未响应";
  }
  setTimeout(poll, 1000);
}
async function pollLive() {
  if (state.livePolling) return;
  state.livePolling = true;
  const rid = state.recRid, since = state.liveCount;
  try {
    // 句子从"最早还没拿到译文的那句"起重新拉（译文是异步补上的，只按 sent_total 增量拉会漏掉后来译好的旧句）
    let sentSince = 0;
    while (sentSince < state.liveSentTotal && state.liveSents[sentSince] && state.liveSents[sentSince].zh !== null) sentSince++;
    const d = await api(`/api/recorder/live?since=${since}&sent_since=${sentSince}`);
    if (rid !== state.recRid || state.current !== rid || since !== state.liveCount || (d.recorder && d.recorder.rid !== rid)) return;
    if (d.total < state.liveCount) { state.liveWords = []; state.liveCount = 0; state.liveSents = []; state.liveSentTotal = 0; return; }   // 下轮从头拉
    if (d.words.length) { state.liveWords.push(...d.words); state.liveCount = d.total; }
    let sentChanged = false;
    for (const sd of d.sents || []) { const old = state.liveSents[sd.k]; if (!old || old.zh !== sd.zh) { state.liveSents[sd.k] = sd; sentChanged = true; } }
    state.liveSentTotal = d.sent_total || 0;
    const changed = d.words.length || d.pending !== state.livePending || sentChanged;
    state.livePending = d.pending;
    if (d.mt_error) $("rec-hint").textContent = "实时翻译: " + d.mt_error;
    if (changed || ((state.liveWords.length || state.livePending) && !$("segments").querySelector(".para.live"))) renderLive();
  } finally { state.livePolling = false; }
}
// 录音中每 0.5s 拉一次实时结果（状态轮询是 1s）
setInterval(() => { if (state.recState !== "idle" && state.current === state.recRid) pollLive().catch(() => {}); }, 500);

// ---------------------------------------------------------------- 搜索
$("search").oninput = () => {
  const q = $("search").value.trim().toLowerCase();
  let n = 0;
  document.querySelectorAll("#segments .seg").forEach((el) => { const hit = !q || el.querySelector(".tx").textContent.toLowerCase().includes(q); el.style.display = hit ? "" : "none"; if (hit && q) n++; });
  document.querySelectorAll("#segments .para").forEach((el) => {
    let hitAny = false;
    el.querySelectorAll(".w").forEach((w) => { const hit = q && w.textContent.toLowerCase().includes(q); w.classList.toggle("hit", !!hit); if (hit) { hitAny = true; n++; } });
    el.style.display = !q || hitAny ? "" : "none";
  });
  $("search-count").textContent = q ? `${n} 处` : "";
};

// ---------------------------------------------------------------- 其它操作
$("import-file").onchange = async (ev) => {
  const input = ev.target;
  const f = input.files[0]; if (!f || input.disabled) return;
  const hint = $("import-status");
  input.disabled = true;
  $("import-label").textContent = "导入中…";
  hint.hidden = false;
  hint.textContent = `正在上传 ${f.name}…`;
  const fd = new FormData(); fd.append("file", f); fd.append("name", f.name.replace(/\.[^.]+$/, "")); fd.append("hotwords", $("rec-hotwords").value); fd.append("course", $("rec-course").value); fd.append("translate", $("rec-mt").checked ? "true" : "false");
  fd.append("lang", $("rec-lang").value);
  try {
    const d = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/import");
      xhr.responseType = "json";
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) hint.textContent = `正在上传 ${f.name}：${Math.round(event.loaded / event.total * 100)}%`;
      };
      xhr.upload.onload = () => { hint.textContent = `正在处理 ${f.name}（转换格式与音量归一化），长录音需要一些时间，仍可浏览其他记录。`; };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300 && xhr.response?.recording) resolve(xhr.response);
        else reject(new Error(xhr.response?.detail || `服务器返回 ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("连接中断，请检查服务是否运行；重试前请先查看历史记录是否已导入。"));
      xhr.onabort = () => reject(new Error("上传已中断"));
      xhr.send(fd);
    });
    hint.textContent = `${f.name} 已导入，已提交转录任务。`;
    await select(d.recording.id);
  } catch (e) { hint.textContent = e.message.startsWith("导入失败") ? e.message : "导入失败：" + e.message; }
  finally { $("import-label").textContent = "导入音频"; input.disabled = false; input.value = ""; }
};
$("btn-retranscribe").onclick = async () => {
  if (!(await requireModels([$("tr-asr").value || "asr-turbo", ...($("rec-mt").checked ? ["mt-final"] : [])]))) return;
  const hw = prompt("术语提示（可留空）：", state.list.find((m) => m.id === state.current)?.hotwords || "");
  if (hw === null) return;
  await post(`/api/recordings/${state.current}/transcribe`, { hotwords: hw, lang: $("tr-lang").value, translate: $("tr-lang").value !== "zh" && $("rec-mt").checked, model_id: $("tr-asr").value || null });
  refreshCurrent(); loadList();
};
$("btn-cancel-import").onclick = async () => {
  if (!confirm("停止导入并删除这条记录？（已转码的部分会丢弃，原文件不动）")) return;
  const rid = state.current;
  try { await post(`/api/recordings/${rid}/cancel-import`); } catch (e) { alert("取消失败：" + e.message); return; }
  if (state.current === rid) state.current = null;
  await loadList();
  if (state.list.length) select(state.list[0].id); else { $("tr-name").textContent = "未选择录音"; $("segments").innerHTML = ""; $("tr-actions").hidden = true; }
};
$("btn-rename").onclick = async () => {
  const name = prompt("新名称：", $("tr-name").textContent); if (!name) return;
  await post(`/api/recordings/${state.current}/rename`, { name }); refreshCurrent(); loadList();
};
$("btn-delete").onclick = () => deleteRecording(state.current, $("tr-name").textContent);
$("btn-export").onclick = async (ev) => {
  const mod = prompt("模块号（M04 / M04-part1 / M04-partial）：", ev.currentTarget.dataset.module || "M0");
  if (!mod) return;
  try { const r = await post(`/api/recordings/${state.current}/export`, { module: mod.trim() }); $("rec-hint").textContent = "已导出：" + r.path; refreshCurrent(); }
  catch (e) {
    if (/已存在/.test(e.message)) {
      if (confirm(e.message + "\n覆盖？")) { const r = await post(`/api/recordings/${state.current}/export`, { module: mod.trim(), overwrite: true }); $("rec-hint").textContent = "已导出：" + r.path; refreshCurrent(); }
    } else alert("导出失败：" + e.message);
  }
};
$("btn-dropped").onclick = () => { state.showDropped = !state.showDropped; refreshCurrent(); };
function applyLang() {
  const box = $("segments");
  box.classList.remove("lang-both", "lang-en", "lang-zh");
  const hasTranslation = !!state.translation || state.segments.some(s => s.zh) || (liveForced() && state.liveSents.some((s) => s?.zh));
  box.classList.add("lang-" + (hasTranslation ? state.lang : "en"));
  $("btn-lang").textContent = hasTranslation ? { both: "双语", en: "仅原文", zh: "仅译文" }[state.lang] : "原文";
  $("btn-lang").disabled = !hasTranslation;
}
$("btn-quit").onclick = async () => {
  if (!confirm("关闭后台转录服务？\n下次用桌面的「课堂转录」图标重新打开即可。")) return;
  try { await post("/api/shutdown"); } catch (e) { alert("无法退出：" + e.message); return; }
  state.quit = true;
  document.body.innerHTML = `<div class="quit-note">服务已关闭，可以关掉这个窗口。<br><small>下次用桌面的「课堂转录」图标打开</small></div>`;
  setTimeout(() => { try { window.close(); } catch {} }, 400);
};
$("btn-lang").onclick = () => { state.lang = { both: "en", en: "zh", zh: "both" }[state.lang]; try { localStorage.setItem("lt.lang", state.lang); } catch {} applyLang(); };
try { state.lang = localStorage.getItem("lt.lang") || "both"; } catch {}
$("btn-translate").onclick = async () => {
  if (!(await requireModels(["mt-final"]))) return;
  try { await post(`/api/recordings/${state.current}/translate`); $("btn-translate").disabled = true; state.tRunning = true; refreshCurrent(); }
  catch (e) { alert("翻译失败：" + e.message); }
};
for (const id of ["rec-live-mt", "rec-mt"]) {
  try { const v = localStorage.getItem("lt." + id); if (v !== null) $(id).checked = v === "1"; } catch {}
  $(id).onchange = () => { try { localStorage.setItem("lt." + id, $(id).checked ? "1" : "0"); } catch {} };
}
function applyAudioLanguage() {
  const chinese = $("rec-lang").value === "zh";
  for (const id of ["rec-live-mt", "rec-mt"]) {
    if (chinese) $(id).checked = false;
    const model = id === "rec-live-mt" ? "mt-live" : "mt-final";
    $(id).disabled = chinese || state.recState !== "idle" || !modelInstalled(model) || !state.modelInfo?.translation_runtime;
  }
}
try { $("rec-lang").value = localStorage.getItem("lt.audio-lang") || "auto"; } catch {}
$("rec-lang").onchange = () => { applyAudioLanguage(); try { localStorage.setItem("lt.audio-lang", $("rec-lang").value); } catch {} };
applyAudioLanguage();
for (const action of ["pause", "resume", "cancel"]) {
  $("btn-job-" + action).onclick = async () => {
    const job = state.controlJob; if (!job) return;
    try { await post(`/api/jobs/${job.id}/${action}`); await refreshCurrent(); await loadList(); }
    catch (e) { alert("任务操作失败：" + e.message); }
  };
}
$("btn-view").onclick = () => { state.view = (state.view || "line") === "para" ? "line" : "para"; try { localStorage.setItem("lt.view", state.view); } catch {} renderSegments(state.segments, false); };
try { state.view = localStorage.getItem("lt.view") || "line"; } catch { state.view = "line"; }
$("btn-copy-path").onclick = async (ev) => { const p = ev.currentTarget.dataset.path; try { await navigator.clipboard.writeText(p); $("rec-hint").textContent = "已复制：" + p; } catch { prompt("路径：", p); } };
document.addEventListener("keydown", (e) => { if (e.code === "Space" && !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) { e.preventDefault(); const a = $("audio"); if (a.src) a.paused ? a.play() : a.pause(); } });

// ---------------------------------------------------------------- 启动
async function boot() {
  try {
    await Promise.all([loadDevices(), loadList(), loadCourses(), loadModelStatus()]);
    state.booted = true;
    if (state.list.length && !state.current) select(state.list[0].id);
  } catch (e) { state.booted = false; }  // 服务还没起来：poll 成功后会再试
}
initModelManager();
(async () => { await boot(); poll(); })();
