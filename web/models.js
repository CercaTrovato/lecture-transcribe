/* Shared model management UI. No implicit downloads from inference actions. */
function initModelManager() {
  $("btn-runtime").hidden = !window.desktop?.runtimeSettings;
  $("btn-runtime").onclick = () => window.desktop.runtimeSettings();
  $("btn-models").onclick = () => { $("model-manager").showModal(); loadModelStatus().catch(modelMessage); };
  $("close-models").onclick = () => $("model-manager").close();
  $("tr-draft").onchange = () => refreshCurrent();
  $("choose-model-storage").onclick = async () => {
    try {
      const path = window.desktop?.pickModelPath ? await window.desktop.pickModelPath(true) : prompt("选择模型存储目录。原目录文件保留，不会自动搬移；已有外部模型可通过引用使用。", state.modelInfo.storage);
      if (path) {
        await post("/api/models/storage", {path});
        modelMessage("已切换模型目录；原目录文件全部保留。");
        await loadModelStatus();
      }
    } catch (error) { modelMessage(error); }
  };
}
function modelMessage(error) { $("model-message").textContent = error?.message || String(error || ""); }
function modelInstalled(id) { return state.modelInfo?.models.some(m => m.id === id && m.installed); }
async function requireModels(ids) {
  await loadModelStatus();
  const missing = ids.filter(id => id && !modelInstalled(id));
  if (!missing.length) return true;
  modelMessage("此功能需要 " + state.modelInfo.models.filter(m => missing.includes(m.id)).map(m => m.name).join("、") + "。请选择下载；不会自动安装。");
  $("model-manager").showModal();
  return false;
}
async function loadModelStatus() {
  state.modelInfo = await api("/api/models");
  const asr = state.modelInfo.models.filter(m => m.kind === "asr" && m.installed);
  const options = asr.length ? asr.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("") : '<option value="">未安装转录模型 · 可仅录音</option>';
  for (const id of ["rec-asr", "tr-asr"]) {
    const select = $(id), value = select.value;
    if (select.dataset.options !== options) {
      select.innerHTML = options; select.dataset.options = options;
      select.value = asr.some(m => m.id === value) ? value : (state.modelInfo.asr_model || "");
    }
  }
  if (!state.modelsInitialized) {
    $("rec-live").checked = asr.length > 0;
    $("rec-live-mt").checked = modelInstalled("mt-live") && state.modelInfo.translation_runtime;
    $("rec-mt").checked = false;
    state.modelsInitialized = true;
  }
  const active = state.recState !== "idle";
  $("rec-asr").disabled = active;
  $("rec-live").disabled = active || !asr.length;
  $("rec-live").title = asr.length ? "使用所选模型实时识别" : "在模型管理中主动安装转录模型后可开启";
  if (!asr.length && !active) $("rec-live").checked = false;
  for (const [id, model] of [["rec-live-mt", "mt-live"], ["rec-mt", "mt-final"]]) {
    const available = modelInstalled(model) && state.modelInfo.translation_runtime;
    $(id).disabled = active || !available || $("rec-lang").value === "zh";
    if (!available && !active) $(id).checked = false;
  }
  if ($("model-manager").open) renderModelCards();
}
function renderModelCards() {
  const rows = state.modelInfo.models;
  $("model-storage").textContent = "模型目录：" + state.modelInfo.storage;
  $("model-runtime").textContent = `当前推理设备：${state.modelInfo.device}。` + (state.modelInfo.runtime_error?.startsWith("缺少 llama-server") ? " 翻译运行组件尚未就绪，请通过安装器获取组件或设置已有运行程序路径。" : "");
  $("model-cards").innerHTML = rows.map(m => {
    const task = m.download || {}, busy = ["downloading", "cancelling", "verifying"].includes(task.state);
    const percent = task.total ? Math.min(100, task.bytes * 100 / task.total).toFixed(1) : "0.0";
    const label = m.installed ? (m.source === "external" ? "已引用外部模型" : "已安装") : m.source === "external" ? "外部模型文件缺失，请更换或移除引用" : {downloading:"下载中",cancelling:"正在取消",verifying:"校验中",cancelled:"已取消",error:"下载失败"}[task.state] || "未安装";
    return `<article class="model-card" data-model="${m.id}"><div class="model-heading"><strong>${esc(m.name)}${m.recommended ? '<span class="model-recommended">推荐</span>' : ''}</strong><span>${(m.bytes/1e9).toFixed(2)} GB · ${label}</span></div><p>${esc(m.purpose)}</p><p class="model-impact">未安装或卸载后：${esc(m.impact)}</p>${busy ? `<progress max="100" value="${percent}"></progress><span class="hint">${percent}% · ${(task.bytes/1e6).toFixed(1)} / ${(task.total/1e6).toFixed(1)} MB</span>` : ''}${task.error ? `<p class="model-error">${esc(task.error)}</p>` : ''}<div class="row">${m.installed ? `<button class="btn small danger-text" data-action="remove" ${m.in_use ? 'disabled' : ''}>${m.in_use ? '任务使用中' : m.source === 'external' ? '移除引用' : '卸载模型'}</button>` : busy ? `<button class="btn small" data-action="cancel" ${task.state==='cancelling'?'disabled':''}>取消下载</button>` : `<button class="btn primary small" data-action="download">${['error','cancelled'].includes(task.state)?'重试 / 继续下载':'下载模型'}</button><button class="btn small" data-action="reference">引用已有模型</button>${['error','cancelled'].includes(task.state)?'<button class="btn small" data-action="discard">清理下载文件</button>':''}`}</div></article>`;
  }).join("");
  for (const card of $("model-cards").querySelectorAll(".model-card")) {
    const model = rows.find(m => m.id === card.dataset.model);
    if (model.source === "external" && !model.installed) card.querySelector('.row').innerHTML = `<button class="btn small danger-text" data-action="remove" ${model.in_use?'disabled':''}>移除失效引用</button><button class="btn small" data-action="reference" ${model.in_use?'disabled':''}>更换引用</button>`;
    for (const button of card.querySelectorAll("button")) button.onclick = async () => {
      button.disabled = true;
      try {
        if (button.dataset.action === "download") await post(`/api/models/${model.id}/download`);
        if (button.dataset.action === "cancel") await post(`/api/models/${model.id}/cancel`);
        if (button.dataset.action === "discard" && confirm("清理本模型未完成的下载？录音与历史不会改变。")) await api(`/api/models/${model.id}/partial`, {method:"DELETE"});
        if (button.dataset.action === "remove") {
          const text = model.source === "external" ? "只移除引用，原模型文件不会删除。" : `将释放约 ${(model.installed_bytes/1e9).toFixed(2)} GB 模型空间。`;
          if (confirm(`${model.name}\n${text}\n${model.impact}\n录音、转录和译文全部保留。继续？`)) await api(`/api/models/${model.id}`, {method:"DELETE"});
        }
        if (button.dataset.action === "reference") {
          const directory = model.kind === "asr" && model.backend === "ctranslate2";
          const path = window.desktop?.pickModelPath ? await window.desktop.pickModelPath(directory) : prompt(directory ? "选择已有模型目录（包含 model.bin）" : "输入已有模型文件路径");
          if (path) await post(`/api/models/${model.id}/reference`, {path});
        }
        modelMessage(""); await loadModelStatus();
      } catch (error) { modelMessage(error); button.disabled = false; }
    };
  }
}
