/**
 * Prism Document Studio - Client Application
 * Zero-disk, composable PDF manipulation studio.
 */

const state = {
  activeVerb: "compose",
  files: [], // Array of File objects
  pageSequence: [], // Array of { pageNum: 1, rotation: 0, isBlank: false }
  processing: false,
};

// DOM References
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const viewport = document.getElementById("viewport");
const pageStrip = document.getElementById("page-strip");
const loadedFilename = document.getElementById("loaded-filename");
const loadedMeta = document.getElementById("loaded-meta");
const btnClearFiles = document.getElementById("btn-clear-files");
const btnExecute = document.getElementById("btn-execute");
const btnExecLabel = document.getElementById("btn-exec-label");
const execSpinner = document.getElementById("exec-spinner");
const execMsg = document.getElementById("execution-msg");
const inspectOutput = document.getElementById("inspect-output");
const inspectContent = document.getElementById("inspect-content");
const verbTitle = document.getElementById("verb-panel-title");

// Verb Navigation Handlers
document.querySelectorAll(".verb-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const verb = btn.getAttribute("data-verb");
    setActiveVerb(verb);
  });
});

function setActiveVerb(verb) {
  state.activeVerb = verb;
  document.querySelectorAll(".verb-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-verb") === verb);
  });
  document.querySelectorAll(".verb-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `panel-${verb}`);
  });

  const titles = {
    compose: "Compose Configuration",
    transform: "Transform & Optimization",
    protect: "Security & Protection",
    inspect: "Document Telemetry & Diff",
    pipeline: "Multi-Op Pipeline Recipe",
  };
  verbTitle.textContent = titles[verb] || "Configuration";

  // If Inspect is chosen, hide or show inspect output
  if (verb !== "inspect") {
    inspectOutput.style.display = "none";
  }
}

// File Drag & Drop Handlers
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("dragover");
});

dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    handleLoadedFiles(Array.from(e.dataTransfer.files));
  }
});

fileInput.addEventListener("change", (e) => {
  if (e.target.files && e.target.files.length > 0) {
    handleLoadedFiles(Array.from(e.target.files));
  }
});

btnClearFiles.addEventListener("click", () => {
  state.files = [];
  state.pageSequence = [];
  fileInput.value = "";
  viewport.style.display = "none";
  dropzone.style.display = "flex";
  inspectOutput.style.display = "none";
  execMsg.textContent = "";
});

async function handleLoadedFiles(newFiles) {
  state.files.push(...newFiles);
  dropzone.style.display = "none";
  viewport.style.display = "flex";

  const primary = state.files[0];
  loadedFilename.textContent = primary.name + (state.files.length > 1 ? ` (+${state.files.length - 1} more)` : "");
  const totalKb = Math.round(state.files.reduce((acc, f) => acc + f.size, 0) / 1024);

  // Inspect the document structure via inspect API
  try {
    const formData = new FormData();
    formData.append("files", primary);
    const resp = await fetch("/api/v1/inspect", {
      method: "POST",
      body: formData,
    });
    if (resp.ok) {
      const data = await resp.json();
      const pCount = data.page_count || 1;
      loadedMeta.textContent = `${pCount} pages • ${totalKb} KB`;

      // Build sequence
      state.pageSequence = [];
      for (let i = 1; i <= pCount; i++) {
        state.pageSequence.push({ pageNum: i, rotation: 0, isBlank: false });
      }
      renderPageStrip();
    } else {
      loadedMeta.textContent = `${totalKb} KB`;
      state.pageSequence = [{ pageNum: 1, rotation: 0, isBlank: false }];
      renderPageStrip();
    }
  } catch (err) {
    loadedMeta.textContent = `${totalKb} KB`;
    state.pageSequence = [{ pageNum: 1, rotation: 0, isBlank: false }];
    renderPageStrip();
  }
}

function renderPageStrip() {
  pageStrip.innerHTML = "";
  state.pageSequence.forEach((item, idx) => {
    const card = document.createElement("div");
    card.className = "page-card";

    const preview = document.createElement("div");
    preview.className = "page-preview";
    preview.textContent = item.isBlank ? "[ Blank ]" : item.pageNum;
    if (item.rotation > 0) {
      preview.style.transform = `rotate(${item.rotation}deg)`;
    }

    const footer = document.createElement("div");
    footer.className = "page-card-footer";

    const numSpan = document.createElement("span");
    numSpan.className = "page-num";
    numSpan.textContent = `P.${idx + 1}`;

    const actions = document.createElement("div");
    actions.className = "page-card-actions";

    const rotBtn = document.createElement("button");
    rotBtn.className = "btn-icon-mini";
    rotBtn.title = "Rotate 90° CW";
    rotBtn.textContent = "↻";
    rotBtn.onclick = () => {
      item.rotation = (item.rotation + 90) % 360;
      renderPageStrip();
    };

    const delBtn = document.createElement("button");
    delBtn.className = "btn-icon-mini";
    delBtn.title = "Remove Page";
    delBtn.textContent = "✕";
    delBtn.onclick = () => {
      state.pageSequence.splice(idx, 1);
      renderPageStrip();
    };

    actions.appendChild(rotBtn);
    actions.appendChild(delBtn);
    footer.appendChild(numSpan);
    footer.appendChild(actions);

    card.appendChild(preview);
    card.appendChild(footer);
    pageStrip.appendChild(card);
  });
}

// Quick Strip Action Buttons
document.getElementById("chip-rot-cw").addEventListener("click", () => {
  state.pageSequence.forEach((p) => {
    p.rotation = (p.rotation + 90) % 360;
  });
  renderPageStrip();
});

document.getElementById("chip-reverse").addEventListener("click", () => {
  state.pageSequence.reverse();
  renderPageStrip();
});

document.getElementById("chip-insert-blank").addEventListener("click", () => {
  state.pageSequence.push({ pageNum: 0, rotation: 0, isBlank: true });
  renderPageStrip();
});

// Dynamic form display toggles
document.getElementById("transform-mode").addEventListener("change", (e) => {
  const mode = e.target.value;
  document.getElementById("grp-compress-level").style.display = mode === "compress" ? "flex" : "none";
  document.getElementById("grp-image-format").style.display = mode === "to_images" ? "flex" : "none";
});

document.getElementById("protect-mode").addEventListener("change", (e) => {
  const mode = e.target.value;
  document.getElementById("grp-password").style.display = mode === "encrypt" || mode === "decrypt" ? "flex" : "none";
  document.getElementById("grp-redact-terms").style.display = mode === "redact" ? "flex" : "none";
  document.getElementById("grp-cert-file").style.display = mode === "sign" ? "flex" : "none";
});

// Execution Handler
btnExecute.addEventListener("click", async () => {
  if (state.files.length === 0) {
    execMsg.textContent = "Please upload at least one document first.";
    execMsg.className = "exec-status error";
    return;
  }

  setBusy(true);
  execMsg.textContent = "Processing with in-memory streaming engine...";
  execMsg.className = "exec-status";

  try {
    const startTime = performance.now();
    let endpoint = "";
    const formData = new FormData();

    // Attach all files
    state.files.forEach((f) => formData.append("files", f));

    if (state.activeVerb === "compose") {
      endpoint = "/api/v1/compose";
      const userSel = document.getElementById("compose-pages").value.trim();
      const nupVal = parseInt(document.getElementById("compose-nup").value, 10);
      const targetSize = document.getElementById("compose-target-size").value;

      // Build rotation map from interactive page sequence
      const rotMap = {};
      state.pageSequence.forEach((p, idx) => {
        if (p.rotation > 0) rotMap[idx + 1] = p.rotation;
      });

      const plan = {
        page_selection: userSel || state.pageSequence.filter(p => !p.isBlank).map(p => p.pageNum),
        rotation: Object.keys(rotMap).length > 0 ? rotMap : undefined,
        nup: nupVal,
        target_size: targetSize || undefined,
      };
      formData.append("plan", JSON.stringify(plan));

    } else if (state.activeVerb === "transform") {
      endpoint = "/api/v1/transform";
      const mode = document.getElementById("transform-mode").value;
      const level = document.getElementById("compress-level").value;
      const fmt = document.getElementById("transform-img-fmt").value;
      formData.append("options", JSON.stringify({ mode, level, image_format: fmt }));

    } else if (state.activeVerb === "protect") {
      endpoint = "/api/v1/protect";
      const mode = document.getElementById("protect-mode").value;
      const pw = document.getElementById("protect-password").value;
      const redactTerms = document.getElementById("redact-terms").value;
      const certFile = document.getElementById("cert-input").files[0];
      if (certFile) {
        formData.append("certificate", certFile);
      }
      formData.append("options", JSON.stringify({
        mode,
        password: pw,
        redact_terms: redactTerms,
      }));

    } else if (state.activeVerb === "inspect") {
      endpoint = "/api/v1/inspect";
      const diffFile = document.getElementById("diff-file-input").files[0];
      if (diffFile) {
        formData.append("files", diffFile);
      }

    } else if (state.activeVerb === "pipeline") {
      endpoint = "/api/v1/pipeline";
      const recipe = [
        { verb: "compose", options: { rotation: 0 } },
        { verb: "transform", options: { mode: "compress", level: "medium" } },
        { verb: "protect", options: { mode: "sanitize" } },
      ];
      formData.append("recipe", JSON.stringify(recipe));
    }

    const resp = await fetch(endpoint, {
      method: "POST",
      body: formData,
    });

    const elapsed = Math.round(performance.now() - startTime);

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "Engine error" }));
      throw new Error(err.detail || `Server returned ${resp.status}`);
    }

    if (state.activeVerb === "inspect") {
      const report = await resp.json();
      inspectContent.textContent = JSON.stringify(report, null, 2);
      inspectOutput.style.display = "block";
      execMsg.textContent = `Telemetry inspection completed in ${elapsed} ms.`;
      execMsg.className = "exec-status success";
    } else {
      const blob = await resp.blob();
      const contentDisp = resp.headers.get("Content-Disposition");
      let downloadName = "prism_output.pdf";
      if (contentDisp && contentDisp.includes("filename=")) {
        const match = contentDisp.match(/filename="?([^";]+)"?/);
        if (match && match[1]) downloadName = match[1];
      }

      // Trigger instant browser download
      const downloadUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = downloadUrl;
      a.download = downloadName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(downloadUrl);

      execMsg.textContent = `Export complete (${Math.round(blob.size / 1024)} KB) in ${elapsed} ms!`;
      execMsg.className = "exec-status success";
    }

  } catch (err) {
    execMsg.textContent = `Error: ${err.message}`;
    execMsg.className = "exec-status error";
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  state.processing = busy;
  btnExecute.disabled = busy;
  execSpinner.style.display = busy ? "inline-block" : "none";
  btnExecLabel.textContent = busy ? "Executing Stream..." : "Run & Export Document";
}
