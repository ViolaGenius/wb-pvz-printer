// app.js — весь клиентский код окна настроек (без сборки, чистый JS,
// чтобы приложение работало офлайн без npm/CDN зависимостей).

const state = {
  config: null,
  currentTemplateName: "default",
  elements: null, // рабочая копия elements текущего шаблона, редактируется в UI
  scalePxPerMm: 6, // масштаб превью на экране (не влияет на реальную печать/DPI)
  drag: null, // {type: 'move'|'resize', key, startX, startY, startEl}
  calibration: null, // {naturalWidth, naturalHeight, rect:{x,y,w,h} в натуральных пикселях, imgEl}
  calibrationMode: "region", // "region" | "color"
  colorTriggers: [], // рабочая копия cfg.capture.color_triggers
  previewTimer: null,
  healthBannerDismissed: false,
};

function $(sel, root = document) { return root.querySelector(sel); }
function $all(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  return res.json();
}

// ---------------- Вкладки ----------------

const ADVANCED_PIN = "1234"; // не для защиты данных — просто чтобы случайно не залезли не глядя

function initHomeAdvancedToggle() {
  const home = $("#view-home");
  const advanced = $("#view-advanced");
  const modal = $("#pinModal");
  const input = $("#pinInput");
  const error = $("#pinError");

  function openPinModal() {
    input.value = "";
    error.style.display = "none";
    modal.style.display = "flex";
    input.focus();
  }

  function closePinModal() {
    modal.style.display = "none";
  }

  function tryEnterAdvanced() {
    if (input.value === ADVANCED_PIN) {
      closePinModal();
      home.style.display = "none";
      advanced.style.display = "flex";
      // капсула активной вкладки была измерена, пока #view-advanced был display:none
      // (нулевой размер) — пересчитываем позицию сейчас, когда раскладка уже видна
      window.dispatchEvent(new Event("resize"));
    } else {
      error.style.display = "block";
      input.value = "";
      input.focus();
    }
  }

  $("#advancedLinkBtn").addEventListener("click", openPinModal);
  $("#pinCancelBtn").addEventListener("click", closePinModal);
  $("#pinSubmitBtn").addEventListener("click", tryEnterAdvanced);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") tryEnterAdvanced();
    if (e.key === "Escape") closePinModal();
  });
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closePinModal(); // клик по тёмному фону — тоже отмена
  });

  $("#backToHomeBtn").addEventListener("click", () => {
    advanced.style.display = "none";
    home.style.display = "block";
  });
}

function initTabs() {
  const pill = $("#navPill");

  function movePillTo(btn) {
    if (!btn || !pill) return;
    const nav = btn.parentElement;
    const navRect = nav.getBoundingClientRect();
    const btnRect = btn.getBoundingClientRect();
    pill.style.width = btnRect.width + "px";
    pill.style.height = btnRect.height + "px";
    pill.style.transform = `translate(${btnRect.left - navRect.left}px, ${btnRect.top - navRect.top}px)`;
    pill.classList.add("ready");
  }

  $all(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $all(".nav-btn").forEach((b) => b.classList.remove("active"));
      $all(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      movePillTo(btn);
    });
  });

  // ставим капсулу на активную вкладку при старте (без анимации подлёта —
  // просто появляется на месте) и пересчитываем позицию при ресайзе окна
  requestAnimationFrame(() => movePillTo($(".nav-btn.active")));
  window.addEventListener("resize", () => movePillTo($(".nav-btn.active")));
}

// ---------------- Статус (дашборд) ----------------

async function refreshStatus() {
  const s = await api("/api/status");
  const dot = $("#statusDot");
  dot.className = "status-dot " + s.status;
  $("#statusText").textContent = s.status_text;
  $("#statusSub").textContent = s.paused ? "Автопечать приостановлена" : "";
  $("#printerInfo").textContent =
    (s.printer_online ? "Онлайн" : "Не найден/офлайн") +
    ` — ${s.printer_status_text}. Осталось этикеток: ${s.roll_remaining ?? "—"}`;
  $("#lastCellInfo").textContent = s.last_cell_number
    ? `${s.last_cell_number} (${new Date(s.last_scan_time * 1000).toLocaleTimeString()}` +
      (s.last_confidence ? `, уверенность ${Math.round(s.last_confidence)}%` : "") + ")"
    : "ещё не печаталось";
  $("#pauseBtn").textContent = s.paused ? "Возобновить" : "Пауза";
  const devEl = $("#scannerDeviceStatusDash");
  if (devEl) {
    devEl.textContent = s.scanner_device_bound
      ? `Сканер привязан по устройству: ${s.scanner_device_name || "—"}`
      : "Сканер: по эвристике времени между символами (устройство не привязано — см. вкладку «Сканер»)";
  }
}

function initPauseRepeat() {
  $("#pauseBtn").addEventListener("click", async () => {
    await api("/api/pause", { method: "POST" });
    refreshStatus();
  });
  $("#repeatBtn").addEventListener("click", async () => {
    const r = await api("/api/print-repeat", { method: "POST" });
    alert(r.ok ? "Повтор печати отправлен" : "Ошибка: " + r.error);
  });
}

function initDashboardExtras() {
  $("#refreshPreviewBtn").addEventListener("click", async () => {
    const wrap = $("#previewRegionsWrap");
    wrap.innerHTML = '<p class="hint">Загрузка снимка...</p>';
    const r = await api("/api/preview-regions");
    if (!r.ok) { wrap.innerHTML = `<p class="hint">Ошибка: ${r.error}</p>`; return; }
    wrap.innerHTML = "";
    const img = document.createElement("img");
    img.src = "data:image/png;base64," + r.image_base64;
    wrap.appendChild(img);
  });

  $("#testScanBtn").addEventListener("click", async () => {
    $("#testScanResult").textContent = "Запущен тестовый скан...";
    const r = await api("/api/test-scan", { method: "POST" });
    if (!r.ok) { $("#testScanResult").textContent = "Ошибка: " + r.error; return; }
    $("#testScanResult").textContent = "Запущено — следите за статусом выше.";
    setTimeout(refreshStatus, 1500);
    setTimeout(refreshStatus, 3000);
  });

  renderPrintModeToggle("ocr"); // безопасный дефолт до загрузки конфига — loadAll() перерисует актуальным значением
  $("#printModeOcrBtn").addEventListener("click", () => setPrintMode("ocr"));
  $("#printModeScreenshotBtn").addEventListener("click", () => setPrintMode("screenshot"));
}

function renderPrintModeToggle(mode) {
  $("#printModeOcrBtn").classList.toggle("active", mode === "ocr");
  $("#printModeScreenshotBtn").classList.toggle("active", mode === "screenshot");
  $("#printModeHint").textContent = mode === "screenshot"
    ? "Сейчас: печать куском скриншота как есть, без распознавания — исключает ошибки OCR, но нет проверки \"похоже ли на номер\" и поиска по номеру в журнале."
    : "Сейчас: распознавание (OCR) и печать заново отрисованным текстом номера — как раньше.";
}

async function setPrintMode(mode) {
  state.config = await api("/api/config", { method: "POST", body: JSON.stringify({ capture: { print_mode: mode } }) });
  renderPrintModeToggle(mode);
}

// ---------------- Проверка "здоровья" системы ----------------

async function runHealthCheck() {
  const r = await api("/api/health-check");
  renderHealthCheck(r);
}

function renderHealthCheck(r) {
  const overlay = $("#healthCheckOverlay");
  const banner = $("#healthWarningBanner");
  const blocking = r.checks.filter((c) => c.status === "error" && c.blocking);
  const warnings = r.checks.filter((c) => c.status === "warning");

  if (blocking.length) {
    const list = $("#healthCheckList");
    list.innerHTML = "";
    blocking.forEach((c) => {
      const div = document.createElement("div");
      div.className = `health-item status-${c.status}`;
      div.innerHTML = `<div class="hi-title">${c.title}</div><div>${c.message}</div>`;
      list.appendChild(div);
    });
    overlay.style.display = "flex";
  } else {
    overlay.style.display = "none";
  }

  if (warnings.length && !state.healthBannerDismissed) {
    banner.style.display = "flex";
    const text = warnings.map((c) => `${c.title}: ${c.message}`).join(" · ");
    banner.innerHTML = `<div>⚠️ ${text}</div><button class="hb-close" id="healthBannerClose">✕</button>`;
    $("#healthBannerClose").addEventListener("click", () => {
      state.healthBannerDismissed = true;
      banner.style.display = "none";
    });
  } else {
    banner.style.display = "none";
  }
}

function initHealthCheck() {
  $("#healthRecheckBtn").addEventListener("click", runHealthCheck);
}

// ---------------- Самообучающиеся эталоны цифр ----------------

async function refreshDigitTemplatesStatus() {
  const r = await api("/api/digit-templates");
  const grid = $("#digitTemplatesGrid");
  grid.innerHTML = "";
  const minSamples = Number($("#dtMinSamples").value) || 5;
  for (let d = 0; d <= 9; d++) {
    const count = r.sample_counts[String(d)] || 0;
    const cell = document.createElement("div");
    cell.className = "dt-cell" + (count >= minSamples ? " mature" : "");
    cell.innerHTML = `<div class="dt-digit">${d}</div><div class="dt-count">${count}</div>`;
    grid.appendChild(cell);
  }
}

function initDigitTemplates() {
  $("#resetDigitTemplatesBtn").addEventListener("click", async () => {
    if (!confirm("Сбросить все накопленные эталоны цифр? Самообучение начнётся заново с нуля.")) return;
    await api("/api/digit-templates/reset", { method: "POST" });
    refreshDigitTemplatesStatus();
  });
  $("#dtMinSamples").addEventListener("input", refreshDigitTemplatesStatus);
}

// ---------------- Свои звуки (захват/успех/ошибка) ----------------

const SOUND_KINDS = [
  { kind: "capture", label: "«Идёт распознавание»" },
  { kind: "success", label: "«Успех»" },
  { kind: "error", label: "«Ошибка»" },
];

function renderSoundRow(kind, label) {
  const row = document.createElement("div");
  row.className = "sound-upload-row";
  row.innerHTML = `
    <span class="sound-label">${label}</span>
    <audio id="audio-${kind}" preload="none"></audio>
    <button class="btn btn-secondary" data-play="${kind}">▶ Прослушать</button>
    <label class="btn btn-secondary sound-file-label">
      Загрузить WAV
      <input type="file" accept=".wav,audio/wav" data-upload="${kind}" style="display:none">
    </label>
    <button class="btn btn-danger" data-remove="${kind}">Сбросить на системный</button>
    <span class="hint sound-status" id="sound-status-${kind}"></span>
  `;
  return row;
}

async function refreshSoundStatus(kind) {
  const audio = $(`#audio-${kind}`);
  const statusEl = $(`#sound-status-${kind}`);
  // HEAD-подобная проверка через сам GET с кэш-бастером — сервер отдаёт 404, если своего звука нет
  const res = await fetch(`/api/sound/${kind}?t=${Date.now()}`);
  if (res.ok) {
    audio.src = `/api/sound/${kind}?t=${Date.now()}`;
    statusEl.textContent = "загружен свой звук";
  } else {
    audio.removeAttribute("src");
    statusEl.textContent = "используется системный сигнал";
  }
}

function initSoundUploads() {
  const wrap = $("#soundUploadRows");
  SOUND_KINDS.forEach(({ kind, label }) => {
    const row = renderSoundRow(kind, label);
    wrap.appendChild(row);
  });

  $all("[data-play]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const audio = $(`#audio-${btn.dataset.play}`);
      if (audio.src) audio.play().catch(() => {});
    });
  });

  $all("[data-upload]").forEach((input) => {
    input.addEventListener("change", async () => {
      const kind = input.dataset.upload;
      const file = input.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("kind", kind);
      fd.append("file", file);
      const r = await api("/api/upload-sound", { method: "POST", body: fd });
      const statusEl = $(`#sound-status-${kind}`);
      if (r.ok) {
        statusEl.textContent = "загружено";
        refreshSoundStatus(kind);
      } else {
        statusEl.textContent = r.error;
      }
      input.value = "";
    });
  });

  $all("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.remove;
      await api(`/api/sound/${kind}`, { method: "DELETE" });
      refreshSoundStatus(kind);
    });
  });

  SOUND_KINDS.forEach(({ kind }) => refreshSoundStatus(kind));
}

// ---------------- Загрузка конфигурации в поля форм ----------------

// Собрано по открытым источникам (каталоги термоэтикеток) — размеры, ширина
// которых укладывается в диапазон печати XP-370B (20-82 мм). 58x40 отмечен
// отдельно как де-факто стандарт для штрихкодных этикеток маркетплейсов в РФ.
const LABEL_SIZE_PRESETS = [
  { w: 30, h: 20 }, { w: 32, h: 22 }, { w: 33, h: 25 }, { w: 35, h: 15 },
  { w: 35, h: 50 }, { w: 40, h: 25 }, { w: 40, h: 30, label: "40 × 30 мм (по умолчанию)" },
  { w: 40, h: 40 }, { w: 40, h: 57 }, { w: 40, h: 60 }, { w: 40, h: 72 }, { w: 40, h: 80 },
  { w: 45, h: 25 }, { w: 45, h: 45 }, { w: 48, h: 24 }, { w: 50, h: 17 }, { w: 50, h: 25 },
  { w: 50, h: 30 }, { w: 50, h: 40 }, { w: 58, h: 15 }, { w: 58, h: 20 }, { w: 58, h: 30 },
  { w: 58, h: 40, label: "58 × 40 мм (стандарт ШК-этикеток маркетплейсов)" },
  { w: 58, h: 44 }, { w: 58, h: 60 }, { w: 58, h: 80 }, { w: 60, h: 40 }, { w: 63.5, h: 31 },
  { w: 64, h: 15 }, { w: 64, h: 20 }, { w: 64, h: 45 }, { w: 68, h: 40 }, { w: 70, h: 40 },
  { w: 75, h: 40 }, { w: 76, h: 25 }, { w: 76, h: 29 }, { w: 80, h: 40 },
];

function presetKey(w, h) { return `${w}x${h}`; }

function fillLabelSizePresetSelect() {
  const sel = $("#labelSizePreset");
  sel.innerHTML = "";
  LABEL_SIZE_PRESETS.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = presetKey(p.w, p.h);
    opt.textContent = p.label || `${p.w} × ${p.h} мм`;
    sel.appendChild(opt);
  });
  const customOpt = document.createElement("option");
  customOpt.value = "custom";
  customOpt.textContent = "Другой размер (ввести вручную)";
  sel.appendChild(customOpt);
}

function syncLabelSizePresetFromInputs() {
  const w = Number($("#labelWidth").value);
  const h = Number($("#labelHeight").value);
  const key = presetKey(w, h);
  const sel = $("#labelSizePreset");
  const match = Array.from(sel.options).some((o) => o.value === key);
  sel.value = match ? key : "custom";
}

function initLabelSizePreset() {
  fillLabelSizePresetSelect();
  $("#labelSizePreset").addEventListener("change", () => {
    const val = $("#labelSizePreset").value;
    if (val === "custom") return; // оставляем текущие значения полей — пользователь введёт вручную
    const [w, h] = val.split("x").map(Number);
    $("#labelWidth").value = w;
    $("#labelHeight").value = h;
    renderLabelCanvas(); // сразу видно новый размер на превью этикетки
  });
  // если пользователь правит ширину/высоту напрямую — выпадающий список должен
  // подстроиться сам (показать конкретный размер, если он совпал со стандартным,
  // либо "Другой размер", если нет)
  $("#labelWidth").addEventListener("input", syncLabelSizePresetFromInputs);
  $("#labelHeight").addEventListener("input", syncLabelSizePresetFromInputs);
}

function fillPrinterTab(cfg) {
  $("#labelWidth").value = cfg.printer.label_width_mm;
  $("#labelHeight").value = cfg.printer.label_height_mm;
  $("#labelGap").value = cfg.printer.gap_mm;
  $("#labelDpi").value = cfg.printer.dpi;
  $("#labelSpeed").value = cfg.printer.speed;
  $("#labelDensity").value = cfg.printer.density;
  $("#bitmapInvert").checked = !!cfg.printer.bitmap_invert;
  $("#offsetX").value = cfg.printer.offset_x_mm ?? 0;
  $("#offsetY").value = cfg.printer.offset_y_mm ?? 0;
  $("#rollTotal").value = cfg.printer.roll_total_labels;
  $("#rollRemaining").value = cfg.printer.roll_remaining_labels;
  const conv = cfg.printer.image_conversion || {};
  $("#imgConvMethod").value = conv.method || "dither";
  $("#imgConvThreshold").value = conv.threshold_value ?? 160;
  $("#imgConvContrast").value = conv.contrast ?? 1.0;
  $("#imgConvBrightness").value = conv.brightness ?? 1.0;
  $("#imgConvInvert").checked = !!conv.invert;
  syncLabelSizePresetFromInputs();
}

async function loadPrinterList(selected) {
  const r = await api("/api/printers");
  const sel = $("#printerSelect");
  sel.innerHTML = "";
  r.printers.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    if (name === selected) opt.selected = true;
    sel.appendChild(opt);
  });
  if (selected && !r.printers.includes(selected)) {
    const opt = document.createElement("option");
    opt.value = selected;
    opt.textContent = selected + " (не найден сейчас)";
    opt.selected = true;
    sel.appendChild(opt);
  }
}

function fillCaptureTab(cfg) {
  $("#windowTitle").value = cfg.capture.window_title;
  $("#exactTitle").checked = !!cfg.capture.exact_title_match;
  $("#minDigits").value = cfg.capture.min_digits;
  $("#maxDigits").value = cfg.capture.max_digits;
  $("#digitsOnly").checked = !!cfg.capture.ocr_digits_only;
  $("#screenshotBold").checked = !!cfg.capture.screenshot_bold;
  $("#screenshotBoldStrength").value = cfg.capture.screenshot_bold_strength ?? 1;
  $("#ocrUpscale").value = cfg.capture.ocr_upscale_factor ?? 3;
  $("#ocrThresholdMode").value = cfg.capture.ocr_threshold_mode || "auto";
  $("#ocrThresholdValue").value = cfg.capture.ocr_threshold_value ?? 150;
  $("#ocrInvertMode").value = cfg.capture.ocr_invert_mode || "auto";
  $("#ocrMinConfidence").value = cfg.capture.ocr_min_confidence ?? 40;
  $("#ocrAutocrop").checked = cfg.capture.ocr_autocrop !== false;
  $("#ocrStabilityCheck").checked = cfg.capture.ocr_stability_check_enabled !== false;
  $("#ocrStabilityMaxWait").value = cfg.capture.ocr_stability_max_wait_ms ?? 150;
  $("#plausibleMin").value = cfg.capture.plausible_min_number ?? 0;
  $("#plausibleMax").value = cfg.capture.plausible_max_number ?? 0;
  $("#ocrSegmentationEnabled").checked = cfg.capture.ocr_segmentation_enabled !== false;
  $("#ocrOpencvEnabled").checked = cfg.capture.ocr_opencv_enabled !== false;
  $("#digitTemplatesEnabled").checked = cfg.capture.digit_templates_enabled !== false;
  $("#dtHarvestMinConfidence").value = cfg.capture.digit_templates_harvest_min_confidence ?? 90;
  $("#dtHarvestMinAgreement").value = cfg.capture.digit_templates_harvest_min_agreement ?? 2;
  $("#dtMinSamples").value = cfg.capture.digit_templates_min_samples ?? 5;
  $("#dtMaxSamples").value = cfg.capture.digit_templates_max_samples_per_digit ?? 5;
  $("#dtMinMatchScore").value = cfg.capture.digit_templates_min_match_score ?? 75;
  refreshDigitTemplatesStatus();
  $("#colorLogic").value = cfg.capture.color_triggers_logic || "AND";
  state.colorTriggers = JSON.parse(JSON.stringify(cfg.capture.color_triggers || []));
  renderColorTriggersList();
}

function fillScannerTab(cfg) {
  $("#maxInterkey").value = cfg.scanner_detection.max_interkey_ms;
  $("#minCodeLength").value = cfg.scanner_detection.min_code_length;
  $("#postScanDelay").value = cfg.scanner_detection.post_scan_delay_ms;
  $("#debounceMs").value = cfg.scanner_detection.duplicate_debounce_ms;
  $("#retryCount").value = cfg.recognition.retry_count;
  $("#retryDelay").value = cfg.recognition.retry_delay_ms;
  $("#soundCapture").checked = !!cfg.sounds.on_capture_enabled;
  $("#soundSuccess").checked = !!cfg.sounds.on_success_enabled;
  $("#soundError").checked = !!cfg.sounds.on_error_enabled;
  renderScannerDeviceStatus(cfg.scanner_detection.bound_device_path, cfg.scanner_detection.bound_device_name);
}

// ---------------- Привязка сканера к устройству (см. raw_input_listener.py) ----------------

let scannerDetectPollTimer = null;

function renderScannerDeviceStatus(devicePath, deviceName) {
  const hint = $("#scannerDeviceHint");
  const unbindBtn = $("#scannerUnbindBtn");
  if (devicePath) {
    hint.textContent = `Сканер привязан: ${deviceName || devicePath}. Программа реагирует только на ввод с этого устройства — обычная клавиатура и другие сканеры игнорируются.`;
    unbindBtn.style.display = "";
  } else {
    hint.textContent = "Сканер не привязан — используется общая эвристика по времени между символами (настройки ниже). Определите конкретное устройство, если сканер иногда не срабатывает или срабатывает ложно.";
    unbindBtn.style.display = "none";
  }
}

function stopScannerDetectPolling() {
  if (scannerDetectPollTimer) {
    clearInterval(scannerDetectPollTimer);
    scannerDetectPollTimer = null;
  }
}

async function startScannerDetect() {
  const r = await api("/api/scanner-device/start-detect", { method: "POST" });
  if (!r.ok) {
    $("#scannerDeviceHint").textContent = "Не удалось запустить определение устройства: " + r.error;
    return;
  }
  $("#scannerDetectPanel").style.display = "";
  $("#scannerDetectResult").style.display = "none";
  $("#scannerDetectStatus").style.display = "";
  $("#scannerDetectStatus").textContent = "Ожидание скана… отсканируйте QR-код выше нужным сканером.";
  $("#scannerQrImg").src = "/api/scanner-device/qr.png?_=" + Date.now();

  stopScannerDetectPolling();
  scannerDetectPollTimer = setInterval(async () => {
    const poll = await api("/api/scanner-device/poll");
    if (poll.ok && poll.detected) {
      stopScannerDetectPolling();
      showScannerDetectResult(poll.detected.device_path, poll.detected.device_name);
    }
  }, 500);
}

function showScannerDetectResult(devicePath, deviceName) {
  $("#scannerDetectStatus").style.display = "none";
  $("#scannerDetectResult").style.display = "";
  $("#scannerDetectedName").textContent = `Обнаружено устройство: ${deviceName}. Это тот сканер, который должен печатать этикетки?`;
  $("#scannerDetectResult").dataset.devicePath = devicePath;
  $("#scannerDetectResult").dataset.deviceName = deviceName;
}

async function cancelScannerDetect() {
  stopScannerDetectPolling();
  await api("/api/scanner-device/cancel-detect", { method: "POST" });
  $("#scannerDetectPanel").style.display = "none";
}

function initScannerDevice() {
  $("#scannerDetectBtn").addEventListener("click", startScannerDetect);
  $("#scannerCancelDetectBtn").addEventListener("click", cancelScannerDetect);
  $("#scannerRetryBtn").addEventListener("click", startScannerDetect);

  $("#scannerConfirmBtn").addEventListener("click", async () => {
    const devicePath = $("#scannerDetectResult").dataset.devicePath;
    const deviceName = $("#scannerDetectResult").dataset.deviceName;
    state.config = await api("/api/scanner-device/bind", {
      method: "POST",
      body: JSON.stringify({ device_path: devicePath, device_name: deviceName }),
    }).then(() => api("/api/config"));
    $("#scannerDetectPanel").style.display = "none";
    renderScannerDeviceStatus(state.config.scanner_detection.bound_device_path, state.config.scanner_detection.bound_device_name);
  });

  $("#scannerUnbindBtn").addEventListener("click", async () => {
    await api("/api/scanner-device/unbind", { method: "POST" });
    state.config = await api("/api/config");
    renderScannerDeviceStatus(state.config.scanner_detection.bound_device_path, state.config.scanner_detection.bound_device_name);
  });
}

function fillGeneralTab(cfg) {
  $("#autostart").checked = !!cfg.app.autostart;
  $("#hotkeysEnabled").checked = cfg.app.hotkeys_enabled !== false;
  $("#hotkeyPause").value = cfg.app.hotkey_pause || "ctrl+alt+p";
  $("#hotkeyRepeat").value = cfg.app.hotkey_repeat_print || "ctrl+alt+r";
  $("#hotkeyOpenSettings").value = cfg.app.hotkey_open_settings || "ctrl+alt+o";
}

async function loadAll() {
  const cfg = await api("/api/config");
  state.config = cfg;
  fillPrinterTab(cfg);
  await loadPrinterList(cfg.printer.name);
  fillCaptureTab(cfg);
  fillScannerTab(cfg);
  fillGeneralTab(cfg);
  fillTemplateSelect(cfg);
  loadTemplateIntoEditor(cfg.active_template);
  renderPrintModeToggle(cfg.capture.print_mode || "ocr");
}

// ---------------- Сохранение по вкладкам ----------------

function initSaveHandlers() {
  $("#savePrinterBtn").addEventListener("click", async () => {
    const patch = {
      printer: {
        name: $("#printerSelect").value,
        label_width_mm: Number($("#labelWidth").value),
        label_height_mm: Number($("#labelHeight").value),
        gap_mm: Number($("#labelGap").value),
        dpi: Number($("#labelDpi").value),
        speed: Number($("#labelSpeed").value),
        density: Number($("#labelDensity").value),
        bitmap_invert: $("#bitmapInvert").checked,
        offset_x_mm: Number($("#offsetX").value),
        offset_y_mm: Number($("#offsetY").value),
        roll_total_labels: Number($("#rollTotal").value),
        roll_remaining_labels: Number($("#rollRemaining").value),
        image_conversion: {
          method: $("#imgConvMethod").value,
          threshold_value: Number($("#imgConvThreshold").value),
          contrast: Number($("#imgConvContrast").value),
          brightness: Number($("#imgConvBrightness").value),
          invert: $("#imgConvInvert").checked,
        },
      },
    };
    state.config = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
    renderLabelCanvas(); // размеры этикетки/конвертация могли поменяться — обновим честное превью
  });

  $("#refreshPrintersBtn").addEventListener("click", () => loadPrinterList($("#printerSelect").value));

  $("#testPrintBtn").addEventListener("click", async () => {
    const cell_number = $("#testCellNumber").value.trim();
    if (!cell_number) return;
    const r = await api("/api/print-test", { method: "POST", body: JSON.stringify({ cell_number }) });
    $("#testPrintResult").textContent = r.ok ? "Отправлено на печать" : "Ошибка: " + r.error;
  });

  $("#saveCaptureBtn").addEventListener("click", async () => {
    const patch = {
      capture: {
        window_title: $("#windowTitle").value,
        exact_title_match: $("#exactTitle").checked,
        min_digits: Number($("#minDigits").value),
        max_digits: Number($("#maxDigits").value),
        ocr_digits_only: $("#digitsOnly").checked,
        screenshot_bold: $("#screenshotBold").checked,
        screenshot_bold_strength: Number($("#screenshotBoldStrength").value),
        ocr_upscale_factor: Number($("#ocrUpscale").value),
        ocr_threshold_mode: $("#ocrThresholdMode").value,
        ocr_threshold_value: Number($("#ocrThresholdValue").value),
        ocr_invert_mode: $("#ocrInvertMode").value,
        ocr_min_confidence: Number($("#ocrMinConfidence").value),
        ocr_autocrop: $("#ocrAutocrop").checked,
        ocr_stability_check_enabled: $("#ocrStabilityCheck").checked,
        ocr_stability_max_wait_ms: Number($("#ocrStabilityMaxWait").value),
        plausible_min_number: Number($("#plausibleMin").value),
        plausible_max_number: Number($("#plausibleMax").value),
        ocr_segmentation_enabled: $("#ocrSegmentationEnabled").checked,
        ocr_opencv_enabled: $("#ocrOpencvEnabled").checked,
        digit_templates_enabled: $("#digitTemplatesEnabled").checked,
        digit_templates_harvest_min_confidence: Number($("#dtHarvestMinConfidence").value),
        digit_templates_harvest_min_agreement: Number($("#dtHarvestMinAgreement").value),
        digit_templates_min_samples: Number($("#dtMinSamples").value),
        digit_templates_max_samples_per_digit: Number($("#dtMaxSamples").value),
        digit_templates_min_match_score: Number($("#dtMinMatchScore").value),
        color_triggers_logic: $("#colorLogic").value,
      },
    };
    state.config = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
  });

  $("#colorLogic").addEventListener("change", async () => {
    await api("/api/config", { method: "POST", body: JSON.stringify({ capture: { color_triggers_logic: $("#colorLogic").value } }) });
  });

  $("#saveScannerBtn").addEventListener("click", async () => {
    const patch = {
      scanner_detection: {
        max_interkey_ms: Number($("#maxInterkey").value),
        min_code_length: Number($("#minCodeLength").value),
        post_scan_delay_ms: Number($("#postScanDelay").value),
        duplicate_debounce_ms: Number($("#debounceMs").value),
      },
      recognition: {
        retry_count: Number($("#retryCount").value),
        retry_delay_ms: Number($("#retryDelay").value),
      },
      sounds: {
        on_capture_enabled: $("#soundCapture").checked,
        on_success_enabled: $("#soundSuccess").checked,
        on_error_enabled: $("#soundError").checked,
      },
    };
    state.config = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
  });

  $("#saveGeneralBtn").addEventListener("click", async () => {
    const patch = {
      app: {
        autostart: $("#autostart").checked,
        hotkeys_enabled: $("#hotkeysEnabled").checked,
        hotkey_pause: $("#hotkeyPause").value.trim() || "ctrl+alt+p",
        hotkey_repeat_print: $("#hotkeyRepeat").value.trim() || "ctrl+alt+r",
        hotkey_open_settings: $("#hotkeyOpenSettings").value.trim() || "ctrl+alt+o",
      },
    };
    state.config = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
    alert("Сохранено. Горячие клавиши переустановлены.");
  });

  $("#importBtn").addEventListener("click", async () => {
    const file = $("#importInput").files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const r = await api("/api/import", { method: "POST", body: fd });
    $("#importResult").textContent = r.ok ? "Настройки импортированы" : "Ошибка: " + r.error;
    if (r.ok) loadAll();
  });
}

// ---------------- Калибровка области + цветовых точек ----------------

function initCalibration() {
  $("#calibrateBtn").addEventListener("click", async () => {
    $("#calibrateBtn").disabled = true;
    $("#calibrateBtn").textContent = "Переключитесь на «Мой ПВЗ»...";
    const r = await api("/api/calibrate/start", { method: "POST", body: JSON.stringify({ seconds: 4 }) });
    $("#calibrateBtn").disabled = false;
    $("#calibrateBtn").textContent = "Откалибровать (снимок через 4 сек)";
    if (!r.ok) { alert(r.error); return; }

    const area = $("#calibrateArea");
    area.innerHTML = "";
    const img = document.createElement("img");
    img.src = "data:image/png;base64," + r.image_base64;
    area.appendChild(img);
    $("#calibrateModeRow").style.display = "flex";
    $("#colorTriggersPanel").style.display = "block";
    setCalibrationMode("region");

    img.onload = () => {
      state.calibration = { naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight, rect: null, imgEl: img };
      setupSelectionDrag(area, img);
      setupColorPick(area, img);
    };
  });

  $("#modeRegionBtn").addEventListener("click", () => setCalibrationMode("region"));
  $("#modeColorBtn").addEventListener("click", () => setCalibrationMode("color"));

  $("#saveRegionBtn").addEventListener("click", async () => {
    const rect = state.calibration && state.calibration.rect;
    if (!rect) return;
    const r = await api("/api/calibrate/save", {
      method: "POST",
      body: JSON.stringify({ x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.w), height: Math.round(rect.h) }),
    });
    if (r.ok) alert("Область сохранена");
  });

  $("#testRegionBtn").addEventListener("click", async () => {
    $("#testRegionBtn").disabled = true;
    const r = await api("/api/test-region", { method: "POST" });
    $("#testRegionBtn").disabled = false;
    const box = $("#testRegionResult");
    if (!r.ok) { box.textContent = "Ошибка: " + r.error; return; }
    box.innerHTML = "";

    const row = document.createElement("div");
    row.className = "row-inline";
    const img1 = document.createElement("img");
    img1.src = "data:image/png;base64," + r.image_base64;
    img1.title = "Исходная область";
    const img2 = document.createElement("img");
    img2.src = "data:image/png;base64," + r.processed_image_base64;
    img2.title = "После предобработки (то, что видит OCR)";
    row.appendChild(img1);
    row.appendChild(img2);
    if (r.screenshot_preview_base64) {
      const img3 = document.createElement("img");
      img3.src = "data:image/png;base64," + r.screenshot_preview_base64;
      img3.title = "Что уйдёт на печать в режиме «Со скриншота»";
      row.appendChild(img3);
    }
    box.appendChild(row);

    const p = document.createElement("p");
    const confOk = r.recognized && r.confidence >= r.min_confidence;
    p.innerHTML = `Распознано: <b>${r.recognized || "(не распознано)"}</b>` +
      (r.recognized ? ` — уверенность ${r.confidence}% ${confOk ? "✅" : "⚠️ ниже порога " + r.min_confidence + "%"}` : "");
    box.appendChild(p);

    if (r.color_triggers && r.color_triggers.details && r.color_triggers.details.length) {
      const cp = document.createElement("p");
      cp.innerHTML = "Цветовые точки: " + (r.color_triggers.passed ? "✅ совпали" : "❌ не совпали") +
        " (" + r.color_triggers.details.map(d => (d.ok ? "✅" : "❌")).join(" ") + ")";
      box.appendChild(cp);
    }
  });
}

function setCalibrationMode(mode) {
  state.calibrationMode = mode;
  $("#modeRegionBtn").classList.toggle("active", mode === "region");
  $("#modeColorBtn").classList.toggle("active", mode === "color");
  $("#calibrateArea").style.cursor = mode === "color" ? "crosshair" : "crosshair";
}

function setupSelectionDrag(area, img) {
  let startX, startY, rectEl;

  function toImageCoords(clientX, clientY) {
    const r = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / r.width;
    const scaleY = img.naturalHeight / r.height;
    return { x: (clientX - r.left) * scaleX, y: (clientY - r.top) * scaleY, dispRect: r };
  }

  img.addEventListener("mousedown", (e) => {
    if (state.calibrationMode !== "region") return;
    e.preventDefault();

    // ФИКС БАГА: старая рамка выделения не удалялась перед новой — убираем
    // все предыдущие .select-rect из области перед тем, как начать новое выделение.
    $all(".select-rect", area).forEach((el) => el.remove());

    const p = toImageCoords(e.clientX, e.clientY);
    startX = p.x; startY = p.y;
    rectEl = document.createElement("div");
    rectEl.className = "select-rect";
    area.appendChild(rectEl);

    function onMove(ev) {
      const cur = toImageCoords(ev.clientX, ev.clientY);
      const dispR = cur.dispRect;
      const x0 = Math.min(startX, cur.x), y0 = Math.min(startY, cur.y);
      const x1 = Math.max(startX, cur.x), y1 = Math.max(startY, cur.y);
      const scaleXInv = dispR.width / img.naturalWidth;
      const scaleYInv = dispR.height / img.naturalHeight;
      rectEl.style.left = x0 * scaleXInv + "px";
      rectEl.style.top = y0 * scaleYInv + "px";
      rectEl.style.width = (x1 - x0) * scaleXInv + "px";
      rectEl.style.height = (y1 - y0) * scaleYInv + "px";
      state.calibration.rect = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (state.calibration.rect && state.calibration.rect.w > 2 && state.calibration.rect.h > 2) {
        $("#saveRegionBtn").style.display = "inline-block";
      }
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// Пипетка: кликом по калибровочному скриншоту читаем цвет пикселя (через
// оффскрин-canvas с getImageData) и добавляем цветовую точку-триггер.
function setupColorPick(area, img) {
  const pickCanvas = document.createElement("canvas");
  pickCanvas.width = img.naturalWidth;
  pickCanvas.height = img.naturalHeight;
  const ctx = pickCanvas.getContext("2d");
  ctx.drawImage(img, 0, 0);

  img.addEventListener("click", async (e) => {
    if (state.calibrationMode !== "color") return;
    const r = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / r.width;
    const scaleY = img.naturalHeight / r.height;
    const x = Math.round((e.clientX - r.left) * scaleX);
    const y = Math.round((e.clientY - r.top) * scaleY);
    const pixel = ctx.getImageData(x, y, 1, 1).data; // [r,g,b,a]
    const colorRgb = [pixel[0], pixel[1], pixel[2]];

    const res = await api("/api/color-triggers", {
      method: "POST",
      body: JSON.stringify({ x, y, color_rgb: colorRgb, tolerance_percent: 12 }),
    });
    if (res.ok) {
      state.colorTriggers = res.color_triggers;
      renderColorTriggersList();
    }
  });
}

function renderColorTriggersList() {
  const list = $("#colorTriggersList");
  list.innerHTML = "";
  if (!state.colorTriggers.length) {
    list.innerHTML = '<p class="hint">Точек пока нет — переключитесь в режим «Пипетка цвета» на снимке выше и кликните по нужному месту.</p>';
    return;
  }
  state.colorTriggers.forEach((t) => {
    const row = document.createElement("div");
    row.className = "color-trigger-row";

    const swatch = document.createElement("div");
    swatch.className = "color-swatch";
    swatch.style.background = `rgb(${t.color_rgb.join(",")})`;
    row.appendChild(swatch);

    const coords = document.createElement("div");
    coords.className = "ct-coords";
    coords.textContent = `X:${t.x} Y:${t.y}`;
    row.appendChild(coords);

    const tolLabel = document.createElement("label");
    tolLabel.textContent = "Допуск, %";
    const tolInput = document.createElement("input");
    tolInput.type = "number";
    tolInput.min = 0; tolInput.max = 100;
    tolInput.value = t.tolerance_percent;
    tolInput.addEventListener("change", async () => {
      const res = await api(`/api/color-triggers/${t.id}`, {
        method: "POST", body: JSON.stringify({ tolerance_percent: Number(tolInput.value) }),
      });
      state.colorTriggers = res.color_triggers;
    });
    tolLabel.appendChild(tolInput);
    row.appendChild(tolLabel);

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "checkbox-row";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = t.enabled !== false;
    enabledInput.addEventListener("change", async () => {
      const res = await api(`/api/color-triggers/${t.id}`, {
        method: "POST", body: JSON.stringify({ enabled: enabledInput.checked }),
      });
      state.colorTriggers = res.color_triggers;
    });
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode("Включена"));
    row.appendChild(enabledLabel);

    const spacer = document.createElement("div");
    spacer.className = "ct-spacer";
    row.appendChild(spacer);

    const delBtn = document.createElement("button");
    delBtn.className = "btn btn-danger";
    delBtn.textContent = "Удалить";
    delBtn.addEventListener("click", async () => {
      const res = await api(`/api/color-triggers/${t.id}`, { method: "DELETE" });
      state.colorTriggers = res.color_triggers;
      renderColorTriggersList();
    });
    row.appendChild(delBtn);

    list.appendChild(row);
  });
}

// ---------------- Журнал печати ----------------

function logStatusToText(success) {
  if (success === true) return { text: "Успех", cls: "ok" };
  if (success === false) return { text: "Ошибка", cls: "fail" };
  return { text: "Пропущено", cls: "skip" };
}

function currentLogQuery() {
  const params = new URLSearchParams();
  params.set("limit", "300");
  const status = $("#logStatusFilter").value;
  const search = $("#logSearch").value.trim();
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  return params;
}

async function loadLog() {
  const params = currentLogQuery();
  const r = await api("/api/log?" + params.toString());
  const body = $("#logBody");
  body.innerHTML = "";
  r.entries.forEach((e) => {
    const tr = document.createElement("tr");
    const time = new Date(e.timestamp * 1000).toLocaleString();
    const st = logStatusToText(e.success);
    tr.innerHTML = `<td>${time}</td><td>${e.cell_number ?? "—"}</td>` +
      `<td class="${st.cls}">${st.text}</td>` +
      `<td>${e.note || ""}</td>`;
    body.appendChild(tr);
  });
}

function initLogTab() {
  $("#refreshLogBtn").addEventListener("click", loadLog);
  $("#logStatusFilter").addEventListener("change", loadLog);
  $("#logSearch").addEventListener("input", () => {
    clearTimeout(state._logSearchTimer);
    state._logSearchTimer = setTimeout(loadLog, 300);
  });
  $("#exportLogBtn").addEventListener("click", (e) => {
    e.preventDefault();
    window.open("/api/log/export.csv?" + currentLogQuery().toString(), "_blank");
  });
  $("#clearLogBtn").addEventListener("click", async () => {
    if (!confirm("Точно очистить весь журнал печати? Это действие необратимо.")) return;
    await api("/api/log", { method: "DELETE" });
    loadLog();
  });
}

// ---------------- Редактор макета этикетки ----------------

function fillTemplateSelect(cfg) {
  const sel = $("#templateSelect");
  sel.innerHTML = "";
  Object.keys(cfg.templates).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name + (name === cfg.active_template ? " (активный)" : "");
    sel.appendChild(opt);
  });
  sel.value = cfg.active_template;
}

function loadTemplateIntoEditor(name) {
  const tmpl = state.config.templates[name];
  if (!tmpl) return;
  state.currentTemplateName = name;
  // глубокая копия, чтобы правки применялись только по кнопке "Сохранить шаблон"
  state.elements = JSON.parse(JSON.stringify(tmpl.elements));
  $("#templateSelect").value = name;
  syncInputsFromElements();
  syncConvInputsFromElements();
  renderLabelCanvas();
}

function syncInputsFromElements() {
  $all("[data-el]").forEach((input) => {
    const el = state.elements[input.dataset.el];
    if (!el) return;
    const field = input.dataset.field;
    const val = el[field];
    if (input.type === "checkbox") {
      input.checked = !!val;
    } else {
      input.value = val === undefined ? "" : val;
    }
  });
}

// Настройки перевода картинки в Ч/Б (image.image_conversion) — вложенный объект,
// поэтому живут отдельно от общего data-el/data-field биндинга выше. Дефолты
// нужны, т.к. шаблоны, сохранённые до появления otsu/adaptive/уровней, могут не
// содержать этих полей.
const CONV_DEFAULTS = {
  method: "dither", threshold_value: 160, edge_threshold: 60, halftone_cell_px: 6,
  contrast: 1.0, brightness: 1.0, gamma: 1.0, black_point: 0, white_point: 255,
  adaptive_block_px: 25, adaptive_bias: 10, sharpen: false, invert: false,
};

function getImageConversion() {
  const img = state.elements.image;
  if (!img.image_conversion) img.image_conversion = {};
  const conv = img.image_conversion;
  for (const k in CONV_DEFAULTS) {
    if (conv[k] === undefined) conv[k] = CONV_DEFAULTS[k];
  }
  return conv;
}

function syncConvInputsFromElements() {
  if (!state.elements || !state.elements.image) return;
  const conv = getImageConversion();
  $("#convMethod").value = conv.method;
  $("#convThreshold").value = conv.threshold_value;
  $("#convEdgeThreshold").value = conv.edge_threshold;
  $("#convHalftoneCell").value = conv.halftone_cell_px;
  $("#convAdaptiveBlock").value = conv.adaptive_block_px;
  $("#convAdaptiveBias").value = conv.adaptive_bias;
  $("#convContrast").value = conv.contrast;
  $("#convBrightness").value = conv.brightness;
  $("#convGamma").value = conv.gamma;
  $("#convBlackPoint").value = conv.black_point;
  $("#convWhitePoint").value = conv.white_point;
  $("#convSharpen").checked = !!conv.sharpen;
  $("#convInvert").checked = !!conv.invert;
}

// [id элемента, поле в image_conversion, тип значения]
const CONV_BINDINGS = [
  ["convMethod", "method", "str"],
  ["convThreshold", "threshold_value", "num"],
  ["convEdgeThreshold", "edge_threshold", "num"],
  ["convHalftoneCell", "halftone_cell_px", "num"],
  ["convAdaptiveBlock", "adaptive_block_px", "num"],
  ["convAdaptiveBias", "adaptive_bias", "num"],
  ["convContrast", "contrast", "num"],
  ["convBrightness", "brightness", "num"],
  ["convGamma", "gamma", "num"],
  ["convBlackPoint", "black_point", "num"],
  ["convWhitePoint", "white_point", "num"],
  ["convSharpen", "sharpen", "bool"],
  ["convInvert", "invert", "bool"],
];

function initConvInputs() {
  CONV_BINDINGS.forEach(([id, field, type]) => {
    const input = $("#" + id);
    if (!input) return;
    // числовые/чекбокс/селект поля — как только отпустили (change), а не на
    // каждый символ ввода, чтобы не гонять честное превью с сервера на каждую цифру
    input.addEventListener("change", () => {
      const conv = getImageConversion();
      if (type === "bool") conv[field] = input.checked;
      else if (type === "num") conv[field] = Number(input.value);
      else conv[field] = input.value;
      renderLabelCanvas();
    });
  });
}

function initTemplateInputs() {
  $all("[data-el]").forEach((input) => {
    input.addEventListener("change", () => {
      const el = state.elements[input.dataset.el];
      if (!el) return;
      const field = input.dataset.field;
      if (input.type === "checkbox") {
        el[field] = input.checked;
      } else {
        el[field] = input.type === "number" ? Number(input.value) : input.value;
      }
      renderLabelCanvas();
    });
  });

  $("#templateSelect").addEventListener("change", (e) => loadTemplateIntoEditor(e.target.value));

  $("#previewCellNumber").addEventListener("input", requestPreviewDebounced);

  $("#newTemplateBtn").addEventListener("click", async () => {
    const name = prompt("Название нового шаблона (например, Возврат):");
    if (!name) return;
    const elements = JSON.parse(JSON.stringify(state.elements));
    await api("/api/templates", { method: "POST", body: JSON.stringify({ name, elements }) });
    state.config = await api("/api/config");
    fillTemplateSelect(state.config);
    loadTemplateIntoEditor(name);
  });

  $("#deleteTemplateBtn").addEventListener("click", async () => {
    if (state.currentTemplateName === "default") { alert("Нельзя удалить шаблон по умолчанию"); return; }
    if (!confirm("Удалить шаблон \"" + state.currentTemplateName + "\"?")) return;
    await api("/api/templates/" + encodeURIComponent(state.currentTemplateName), { method: "DELETE" });
    state.config = await api("/api/config");
    fillTemplateSelect(state.config);
    loadTemplateIntoEditor(state.config.active_template);
  });

  $("#activateTemplateBtn").addEventListener("click", async () => {
    await api("/api/templates/active", { method: "POST", body: JSON.stringify({ name: state.currentTemplateName }) });
    state.config = await api("/api/config");
    fillTemplateSelect(state.config);
  });

  $("#saveTemplateBtn").addEventListener("click", async () => {
    await api("/api/templates", {
      method: "POST",
      body: JSON.stringify({ name: state.currentTemplateName, elements: state.elements }),
    });
    alert("Шаблон сохранён");
  });

  $("#imageUpload").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const r = await api("/api/upload-image", { method: "POST", body: fd });
    if (r.ok) {
      state.elements.image.path = r.path;
      renderLabelCanvas();
    }
  });
}

function ensureCanvasStructure(canvas) {
  if ($("#labelPreviewImg", canvas)) return;
  canvas.innerHTML = "";
  const img = document.createElement("img");
  img.id = "labelPreviewImg";
  img.className = "label-preview-img";
  img.alt = "Реальное превью этикетки";
  canvas.appendChild(img);
  const overlay = document.createElement("div");
  overlay.id = "labelOverlayLayer";
  overlay.className = "label-overlay-layer";
  canvas.appendChild(overlay);
}

function renderLabelCanvas() {
  if (!state.elements || !state.config) return;
  const scale = state.scalePxPerMm;
  const canvas = $("#labelCanvas");
  const wMm = state.config.printer.label_width_mm;
  const hMm = state.config.printer.label_height_mm;
  canvas.style.width = wMm * scale + "px";
  canvas.style.height = hMm * scale + "px";
  ensureCanvasStructure(canvas);

  const overlay = $("#labelOverlayLayer", canvas);
  overlay.innerHTML = "";
  addCanvasElement(overlay, "cell_number", "Номер ячейки");
  addCanvasElement(overlay, "bar", "Полоса");
  addCanvasElement(overlay, "static_text", "Текст");
  addCanvasElement(overlay, "image", "Картинка");

  requestPreviewDebounced();
}

// Запрашивает у сервера ЧЕСТНЫЙ рендер всей этикетки (та же функция, что и при
// реальной печати — включая автоцентровку/автоподбор шрифта и дизеринг картинки)
// и подставляет его как фон под интерактивные рамки. Debounce 250мс, чтобы не
// заваливать сервер запросами при перетаскивании/наборе текста.
function requestPreviewDebounced() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(requestPreviewNow, 250);
}

async function requestPreviewNow() {
  if (!state.elements) return;
  const cellNumber = ($("#previewCellNumber") && $("#previewCellNumber").value) || "42";
  const r = await api("/api/template-preview", {
    method: "POST",
    body: JSON.stringify({ elements: state.elements, cell_number: cellNumber }),
  });
  if (!r.ok) return;
  const img = $("#labelPreviewImg");
  if (img) img.src = "data:image/png;base64," + r.image_base64;
}

function addCanvasElement(overlay, key, label) {
  const el = state.elements[key];
  if (!el) return;
  const scale = state.scalePxPerMm;
  const div = document.createElement("div");
  div.className = "label-el";
  div.style.left = el.x_mm * scale + "px";
  div.style.top = el.y_mm * scale + "px";
  div.style.width = (el.width_mm || 10) * scale + "px";
  div.style.height = (el.height_mm || 10) * scale + "px";

  const tag = document.createElement("div");
  tag.className = "el-tag";
  tag.textContent = label;
  div.appendChild(tag);

  const handle = document.createElement("div");
  handle.className = "resize-handle";
  div.appendChild(handle);

  div.addEventListener("mousedown", (e) => {
    if (e.target === handle) return;
    startDrag(e, key, "move");
  });
  handle.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    startDrag(e, key, "resize");
  });

  overlay.appendChild(div);
}

function startDrag(e, key, type) {
  e.preventDefault();
  const scale = state.scalePxPerMm;
  const startClientX = e.clientX, startClientY = e.clientY;
  const el = state.elements[key];
  const start = JSON.parse(JSON.stringify(el));

  function onMove(ev) {
    const dxMm = (ev.clientX - startClientX) / scale;
    const dyMm = (ev.clientY - startClientY) / scale;
    if (type === "move") {
      el.x_mm = Math.max(0, round1(start.x_mm + dxMm));
      el.y_mm = Math.max(0, round1(start.y_mm + dyMm));
    } else {
      // все элементы (включая текстовые) ресайзятся как обычная рамка —
      // авто-вписывание текста само подберёт максимальный шрифт под неё
      el.width_mm = Math.max(2, round1((start.width_mm || 10) + dxMm));
      el.height_mm = Math.max(2, round1((start.height_mm || 10) + dyMm));
    }
    // во время перетаскивания двигаем только саму рамку (дёшево), а честное
    // превью запрашиваем с debounce, чтобы не заваливать сервер на каждый пиксель
    renderOverlayOnly();
    syncInputsFromElements();
    requestPreviewDebounced();
  }
  function onUp() {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  }
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

// Быстрая перерисовка только рамок (без пересоздания слоя превью и без запроса
// к серверу) — используется во время активного перетаскивания для отзывчивости.
function renderOverlayOnly() {
  const canvas = $("#labelCanvas");
  const overlay = $("#labelOverlayLayer", canvas);
  if (!overlay) return;
  overlay.innerHTML = "";
  addCanvasElement(overlay, "cell_number", "Номер ячейки");
  addCanvasElement(overlay, "bar", "Полоса");
  addCanvasElement(overlay, "static_text", "Текст");
  addCanvasElement(overlay, "image", "Картинка");
}

function round1(n) { return Math.round(n * 10) / 10; }

// ---------------- Инициализация ----------------

document.addEventListener("DOMContentLoaded", async () => {
  initHomeAdvancedToggle();
  initTabs();
  initPauseRepeat();
  initDashboardExtras();
  initLabelSizePreset();
  initHealthCheck();
  initDigitTemplates();
  initSoundUploads();
  initSaveHandlers();
  initCalibration();
  initTemplateInputs();
  initConvInputs();
  initScannerDevice();
  initLogTab();
  await loadAll();
  await refreshStatus();
  loadLog();
  runHealthCheck();
  setInterval(refreshStatus, 2000);
});
