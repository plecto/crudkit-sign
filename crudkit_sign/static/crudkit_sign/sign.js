/* Signer page: renders the PDF with pdf.js, overlays the fields, captures a signature, submits. */
(function () {
  "use strict";

  const read = (id) => JSON.parse(document.getElementById(id).textContent);
  const config = read("cs-config");
  const fields = read("cs-fields");
  const completed = read("cs-completed");
  const IMAGE_KINDS = ["Signature", "Initials"];

  const pagesEl = document.getElementById("cs-pages");
  const statusEl = document.getElementById("cs-status");
  const consentEl = document.getElementById("cs-consent");
  const finishEl = document.getElementById("cs-finish");

  const state = { signature: null, signatureKind: "Drawn", values: {}, busy: false };

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.classList.toggle("is-error", Boolean(isError));
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
    if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    return data;
  }

  // ---- validation ---------------------------------------------------------

  function isComplete() {
    const needsImage = fields.some((f) => IMAGE_KINDS.includes(f.kind) && f.required);
    if (needsImage && !state.signature) return false;
    for (const field of fields) {
      if (!field.required) continue;
      if (field.kind === "Text" && !(state.values[field.id] || "").trim()) return false;
      if (field.kind === "Checkbox") {
        const options = field.group ? fields.filter((f) => f.group === field.group) : [field];
        if (!options.some((f) => state.values[f.id] === "true")) return false;
      }
    }
    return consentEl.checked;
  }

  function refresh() {
    finishEl.disabled = state.busy || !isComplete();
  }

  // ---- rendering ----------------------------------------------------------

  function whenPdfjsReady() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    return new Promise((resolve) => window.addEventListener("pdfjs-ready", () => resolve(window.pdfjsLib), { once: true }));
  }

  function place(el, viewport, field) {
    const [x1, y1, x2, y2] = viewport.convertToViewportRectangle([field.x, field.y, field.x + field.width, field.y + field.height]);
    el.style.left = Math.min(x1, x2) + "px";
    el.style.top = Math.min(y1, y2) + "px";
    el.style.width = Math.abs(x2 - x1) + "px";
    el.style.height = Math.abs(y2 - y1) + "px";
  }

  function completedOverlay(field) {
    const el = document.createElement("div");
    el.className = "cs-field cs-field-done";
    if (field.image) {
      const img = document.createElement("img");
      img.src = field.image;
      img.alt = "";
      el.appendChild(img);
    } else if (field.kind === "Checkbox") {
      el.textContent = field.value ? "X" : "";
    } else {
      el.textContent = field.value;
    }
    return el;
  }

  function ownField(field) {
    const el = document.createElement("div");
    el.className = "cs-field cs-field-own cs-kind-" + field.kind.toLowerCase();
    el.dataset.id = field.id;
    if (IMAGE_KINDS.includes(field.kind)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cs-field-button";
      button.textContent = field.kind === "Initials" ? "Initial" : "Sign here";
      button.addEventListener("click", openSignatureModal);
      const img = document.createElement("img");
      img.alt = "";
      img.hidden = true;
      img.addEventListener("click", openSignatureModal);
      el.append(button, img);
    } else if (field.kind === "Date") {
      el.textContent = config.today;
      el.title = "Filled in automatically";
    } else if (field.kind === "Name") {
      el.textContent = config.signerName;
      el.title = "Filled in automatically";
    } else if (field.kind === "Text") {
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = field.name || "Text";
      input.required = field.required;
      input.addEventListener("input", () => { state.values[field.id] = input.value; refresh(); });
      el.appendChild(input);
    } else if (field.kind === "Checkbox") {
      const input = document.createElement("input");
      input.title = field.name || "";
      if (field.group) {
        // A pick-one group: the browser unchecks the others, so record the whole group.
        input.type = "radio";
        input.name = "cs-group-" + field.group;
        input.addEventListener("change", () => {
          for (const f of fields.filter((f) => f.group === field.group)) state.values[f.id] = f === field ? "true" : "";
          refresh();
        });
      } else {
        input.type = "checkbox";
        input.addEventListener("change", () => { state.values[field.id] = input.checked ? "true" : ""; refresh(); });
      }
      el.appendChild(input);
    }
    return el;
  }

  function applySignatureToFields() {
    for (const el of pagesEl.querySelectorAll(".cs-kind-signature, .cs-kind-initials")) {
      const img = el.querySelector("img");
      const button = el.querySelector("button");
      img.src = state.signature;
      img.hidden = false;
      button.hidden = true;
      el.classList.add("is-filled");
    }
  }

  async function render() {
    const pdfjsLib = await whenPdfjsReady();
    const pdf = await pdfjsLib.getDocument({ url: config.documentUrl }).promise;
    pagesEl.textContent = "";
    const width = Math.min(pagesEl.clientWidth, 900);
    for (let number = 1; number <= pdf.numPages; number += 1) {
      const page = await pdf.getPage(number);
      const scale = width / page.getViewport({ scale: 1 }).width;
      const viewport = page.getViewport({ scale });
      const wrapper = document.createElement("div");
      wrapper.className = "cs-page";
      wrapper.style.width = viewport.width + "px";
      wrapper.style.height = viewport.height + "px";
      const canvas = document.createElement("canvas");
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = viewport.width + "px";
      canvas.style.height = viewport.height + "px";
      wrapper.appendChild(canvas);
      pagesEl.appendChild(wrapper);
      const context = canvas.getContext("2d");
      context.scale(ratio, ratio);
      await page.render({ canvasContext: context, viewport }).promise;
      for (const field of completed.filter((f) => f.page === number)) {
        const el = completedOverlay(field);
        place(el, viewport, field);
        wrapper.appendChild(el);
      }
      for (const field of fields.filter((f) => f.page === number)) {
        const el = ownField(field);
        place(el, viewport, field);
        wrapper.appendChild(el);
      }
    }
    if (state.signature) applySignatureToFields();
    refresh();
  }

  // ---- signature modal ----------------------------------------------------

  const modal = document.getElementById("cs-modal");
  const drawCanvas = document.getElementById("cs-draw");
  const typedInput = document.getElementById("cs-typed");
  const typedCanvas = document.getElementById("cs-typed-preview");
  let activeTab = "draw";
  let drawing = false;
  let hasInk = false;

  function openSignatureModal() {
    modal.hidden = false;
    clearDraw();
    typedInput.value = "";
    renderTyped();
    selectTab("draw");
  }

  function closeSignatureModal() {
    modal.hidden = true;
  }

  function selectTab(name) {
    activeTab = name;
    for (const tab of modal.querySelectorAll(".cs-tab")) tab.classList.toggle("is-active", tab.dataset.tab === name);
    for (const pane of modal.querySelectorAll(".cs-pane")) pane.hidden = pane.dataset.pane !== name;
    if (name === "type") typedInput.focus();
  }

  function pointerPosition(event) {
    const rect = drawCanvas.getBoundingClientRect();
    return [((event.clientX - rect.left) / rect.width) * drawCanvas.width, ((event.clientY - rect.top) / rect.height) * drawCanvas.height];
  }

  function clearDraw() {
    const context = drawCanvas.getContext("2d");
    context.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
    context.lineWidth = 3;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.strokeStyle = "#111827";
    hasInk = false;
  }

  drawCanvas.addEventListener("pointerdown", (event) => {
    drawing = true;
    hasInk = true;
    drawCanvas.setPointerCapture(event.pointerId);
    const context = drawCanvas.getContext("2d");
    const [x, y] = pointerPosition(event);
    context.beginPath();
    context.moveTo(x, y);
    context.lineTo(x + 0.1, y + 0.1);
    context.stroke();
  });
  drawCanvas.addEventListener("pointermove", (event) => {
    if (!drawing) return;
    const context = drawCanvas.getContext("2d");
    const [x, y] = pointerPosition(event);
    context.lineTo(x, y);
    context.stroke();
  });
  const stopDrawing = () => { drawing = false; };
  drawCanvas.addEventListener("pointerup", stopDrawing);
  drawCanvas.addEventListener("pointercancel", stopDrawing);
  drawCanvas.addEventListener("pointerleave", stopDrawing);

  function renderTyped() {
    const context = typedCanvas.getContext("2d");
    context.clearRect(0, 0, typedCanvas.width, typedCanvas.height);
    context.fillStyle = "#111827";
    context.font = "64px 'Brush Script MT', 'Segoe Script', 'Snell Roundhand', 'Apple Chancery', cursive";
    context.textBaseline = "middle";
    context.fillText(typedInput.value.trim(), 30, typedCanvas.height / 2, typedCanvas.width - 60);
  }
  typedInput.addEventListener("input", renderTyped);

  function trimmedDataUrl(source) {
    const context = source.getContext("2d");
    const { data, width, height } = context.getImageData(0, 0, source.width, source.height);
    let top = height, left = width, right = -1, bottom = -1;
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        if (data[(y * width + x) * 4 + 3] > 0) {
          if (x < left) left = x;
          if (x > right) right = x;
          if (y < top) top = y;
          if (y > bottom) bottom = y;
        }
      }
    }
    if (right < 0) return null;
    const pad = 6;
    left = Math.max(0, left - pad); top = Math.max(0, top - pad);
    right = Math.min(width - 1, right + pad); bottom = Math.min(height - 1, bottom + pad);
    const out = document.createElement("canvas");
    out.width = right - left + 1;
    out.height = bottom - top + 1;
    out.getContext("2d").drawImage(source, left, top, out.width, out.height, 0, 0, out.width, out.height);
    return out.toDataURL("image/png");
  }

  document.getElementById("cs-clear").addEventListener("click", clearDraw);
  document.getElementById("cs-modal-cancel").addEventListener("click", closeSignatureModal);
  for (const tab of modal.querySelectorAll(".cs-tab")) tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  document.getElementById("cs-modal-apply").addEventListener("click", () => {
    let dataUrl = null;
    if (activeTab === "draw") {
      if (!hasInk) return setStatus("Draw your signature first.", true);
      dataUrl = trimmedDataUrl(drawCanvas);
      state.signatureKind = "Drawn";
    } else {
      if (!typedInput.value.trim()) return setStatus("Type your name first.", true);
      dataUrl = trimmedDataUrl(typedCanvas);
      state.signatureKind = "Typed";
    }
    if (!dataUrl) return;
    if (dataUrl.length * 0.75 > config.maxSignatureBytes) return setStatus("The signature is too large. Try a simpler one.", true);
    state.signature = dataUrl;
    setStatus("");
    applySignatureToFields();
    closeSignatureModal();
    refresh();
  });

  // ---- decline ------------------------------------------------------------

  const declineModal = document.getElementById("cs-decline-modal");
  document.getElementById("cs-decline").addEventListener("click", () => { declineModal.hidden = false; });
  document.getElementById("cs-decline-cancel").addEventListener("click", () => { declineModal.hidden = true; });
  document.getElementById("cs-decline-confirm").addEventListener("click", async () => {
    if (state.busy) return;
    state.busy = true;
    try {
      const data = await post(config.declineUrl, { reason: document.getElementById("cs-decline-reason").value });
      window.location.href = data.redirect;
    } catch (error) {
      state.busy = false;
      declineModal.hidden = true;
      setStatus(error.message, true);
    }
  });

  // ---- submit -------------------------------------------------------------

  consentEl.addEventListener("change", refresh);
  finishEl.addEventListener("click", async () => {
    if (state.busy || !isComplete()) return;
    state.busy = true;
    refresh();
    setStatus("Submitting…");
    try {
      const data = await post(config.submitUrl, {
        consent: consentEl.checked,
        signature: state.signature,
        signature_kind: state.signatureKind,
        fields: state.values,
      });
      window.location.href = data.redirect;
    } catch (error) {
      state.busy = false;
      setStatus(error.message, true);
      refresh();
    }
  });

  render().catch((error) => {
    pagesEl.textContent = "";
    setStatus("The document could not be displayed: " + error.message, true);
  });
})();
