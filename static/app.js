const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileName = document.getElementById("fileName");
const startButton = document.getElementById("startButton");
const progressWrap = document.getElementById("progressWrap");
const progressBar = document.getElementById("progressBar");
const progressText = document.getElementById("progressText");
const statusText = document.getElementById("statusText");
const downloadButton = document.getElementById("downloadButton");
const errorBox = document.getElementById("errorBox");
const linkStyle = document.getElementById("linkStyle");

let selectedFile = null;
let activeJobId = null;
const chunkSize = 5 * 1024 * 1024;

function setProgress(percent, message) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent || 0)));
  progressWrap.hidden = false;
  progressBar.style.width = `${safePercent}%`;
  progressText.textContent = `${safePercent}%`;
  if (message) statusText.textContent = message;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  startButton.disabled = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function chooseFile(file) {
  clearError();
  downloadButton.hidden = true;
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showError("Please choose a PDF file.");
    return;
  }
  selectedFile = file;
  fileName.textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
  startButton.disabled = false;
  setProgress(0, "Ready to upload...");
  progressWrap.hidden = true;
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") fileInput.click();
});
fileInput.addEventListener("change", event => chooseFile(event.target.files[0]));

["dragenter", "dragover"].forEach(name => {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach(name => {
  dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  });
});

dropzone.addEventListener("drop", event => chooseFile(event.dataTransfer.files[0]));

async function uploadChunks(file) {
  activeJobId = crypto.randomUUID();
  const totalChunks = Math.ceil(file.size / chunkSize);
  for (let index = 0; index < totalChunks; index += 1) {
    const start = index * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const chunk = file.slice(start, end);
    const formData = new FormData();
    formData.append("job_id", activeJobId);
    formData.append("chunk_index", index);
    formData.append("total_chunks", totalChunks);
    formData.append("filename", file.name);
    formData.append("chunk", chunk, `${file.name}.part${index}`);
    const response = await fetch("/upload-chunk", { method: "POST", body: formData });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Upload failed");
    setProgress(((index + 1) / totalChunks) * 8, `Uploading chunk ${index + 1} of ${totalChunks}...`);
  }
  return totalChunks;
}

async function startProcessing(totalChunks) {
  const response = await fetch("/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: activeJobId,
      filename: selectedFile.name,
      total_chunks: totalChunks,
      link_style: linkStyle.value
    })
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Processing failed");
  pollStatus();
}

async function pollStatus() {
  const response = await fetch(`/status/${activeJobId}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || "Unable to read job status");
  setProgress(payload.progress, payload.message);
  if (payload.status === "complete") {
    downloadButton.href = payload.download_url;
    downloadButton.hidden = false;
    startButton.disabled = false;
    return;
  }
  if (payload.status === "error") throw new Error(payload.message || "Conversion failed");
  setTimeout(() => pollStatus().catch(error => showError(error.message)), 900);
}

startButton.addEventListener("click", async () => {
  if (!selectedFile) return;
  clearError();
  downloadButton.hidden = true;
  startButton.disabled = true;
  try {
    setProgress(0, "Starting upload...");
    const totalChunks = await uploadChunks(selectedFile);
    setProgress(8, "Upload complete. Starting extraction...");
    await startProcessing(totalChunks);
  } catch (error) {
    showError(error.message || "Something went wrong");
  }
});
