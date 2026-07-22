import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

function addBoundedDOMWidget(node, name, type, element, height) {
    const container = document.createElement("div");
    container.style.cssText = "width:100%;max-width:100%;height:100%;box-sizing:border-box;overflow:hidden;";
    container.appendChild(element);
    const widget = node.addDOMWidget(name, type, container, {
        serialize: false,
        hideOnZoom: false,
    });
    widget.computeSize = (width) => [width, height];
    return widget;
}

// Show backend progress inside the TRELLIS2 generation node. Some current
// ComfyUI frontends no longer render numeric progress for custom nodes.
app.registerExtension({
    name: "MingFlow.Trellis2InlineProgress",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const supportedNodes = new Set([
            "ARTAI_Trellis2ImageToGLBLocal",
            "ARTAI_Trellis2ImageToGLBLocalV2",
        ]);
        if (!supportedNodes.has(nodeData.name)) return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);

            const container = document.createElement("div");
            container.style.cssText = "width:100%;box-sizing:border-box;padding:5px 2px;color:#ddd;font:12px sans-serif";
            const label = document.createElement("div");
            label.textContent = "대기 중 · 0%";
            label.style.marginBottom = "4px";
            const track = document.createElement("div");
            track.style.cssText = "height:8px;background:#292929;border-radius:4px;overflow:hidden";
            const bar = document.createElement("div");
            bar.style.cssText = "width:0%;height:100%;background:#38bdf8;transition:width .2s ease";
            track.appendChild(bar);
            container.append(label, track);
            this.addDOMWidget("trellis_progress", "TRELLIS_PROGRESS", container, {
                serialize: false,
                hideOnZoom: false,
            });

            const updateProgress = (value, max) => {
                const safeMax = Number(max) || 100;
                const percent = Math.max(0, Math.min(100, Math.round((Number(value) || 0) * 100 / safeMax)));
                bar.style.width = `${percent}%`;
                label.textContent = percent >= 100 ? "완료 · 100%" : `TRELLIS2 처리 중 · ${percent}%`;
            };
            const progressHandler = (event) => {
                const detail = event?.detail || {};
                if (String(detail.node) !== String(this.id)) return;
                updateProgress(detail.value, detail.max);
            };
            api.addEventListener("progress", progressHandler);

            const onExecuted = this.onExecuted;
            this.onExecuted = function () {
                onExecuted?.apply(this, arguments);
                updateProgress(100, 100);
            };
            const onRemoved = this.onRemoved;
            this.onRemoved = function () {
                api.removeEventListener("progress", progressHandler);
                onRemoved?.apply(this, arguments);
            };
        };
    },
});

// Reuse ComfyUI's current 3D viewer for our connectable Tripo wrapper.
app.registerExtension({
    name: "ARTAI.TripoPreview3DAnimation",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_TripoPreview3DAnimation") return;
        nodeData.input.required.image = ["PREVIEW_3D"];
        nodeType.comfyClass = "Preview3D";

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            this.mingFlowTripoGlbFile = null;

            this.addWidget("button", "Tripo GLB 다운로드", null, () => {
                const file = this.mingFlowTripoGlbFile;
                if (!file?.filename) {
                    alert("먼저 Tripo 3D 생성과 프리뷰를 실행하세요.");
                    return;
                }
                const params = new URLSearchParams({
                    filename: file.filename,
                    subfolder: file.subfolder || "",
                    type: file.type || "output",
                });
                const anchor = document.createElement("a");
                anchor.href = api.apiURL(`/view?${params.toString()}`);
                anchor.download = file.filename;
                document.body.appendChild(anchor);
                anchor.click();
                anchor.remove();
            });

            const onExecuted = this.onExecuted;
            this.onExecuted = function (message) {
                onExecuted?.apply(this, arguments);
                this.mingFlowTripoGlbFile = message?.glb_file?.[0] || null;
            };
        };
    },
});

// Reuse ComfyUI's current Preview3D viewer and add a GLB download button.
app.registerExtension({
    name: "ARTAI.Trellis2PreviewGLBDownload",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_Trellis2PreviewGLBDownload") return;

        // Preview3DAnimation was merged into Preview3D in current ComfyUI.
        // The hidden PREVIEW_3D widget creates the actual Three.js canvas.
        // Keeping our server-side glb_path socket avoids breaking workflows.
        nodeData.input.required.image = ["PREVIEW_3D"];
        nodeType.comfyClass = "Preview3D";

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            this.artAiTrellisGlbFile = null;

            this.addWidget("button", "TRELLIS2 GLB 다운로드", null, () => {
                const file = this.artAiTrellisGlbFile;
                if (!file?.filename) {
                    alert("먼저 TRELLIS2 생성 및 Export Mesh를 실행하세요.");
                    return;
                }
                const params = new URLSearchParams({
                    filename: file.filename,
                    subfolder: file.subfolder || "",
                    type: file.type || "output",
                });
                const anchor = document.createElement("a");
                anchor.href = api.apiURL(`/view?${params.toString()}`);
                anchor.download = file.filename;
                document.body.appendChild(anchor);
                anchor.click();
                anchor.remove();
            });

            const onExecuted = this.onExecuted;
            this.onExecuted = function (message) {
                onExecuted?.apply(this, arguments);
                this.artAiTrellisGlbFile = message?.glb_file?.[0] || null;
            };
        };
    },
});

app.registerExtension({
    name: "ARTAI.OpenAIAPIKey",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_OpenAIAPIKey") return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);

            const input = document.createElement("input");
            input.type = "password";
            input.placeholder = "sk-...";
            input.autocomplete = "off";
            input.style.cssText = "display:block;width:100%;max-width:100%;min-width:0;height:34px;box-sizing:border-box;background:#181818;color:#eee;border:1px solid #555;border-radius:4px;padding:6px;";
            addBoundedDOMWidget(this, "api_key", "ARTAI_API_KEY", input, 42);

            const status = document.createElement("div");
            status.textContent = "키가 설정되지 않았습니다.";
            status.style.cssText = "width:100%;max-width:100%;box-sizing:border-box;padding:6px 2px;color:#bbb;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;user-select:none;";
            addBoundedDOMWidget(this, "status", "ARTAI_STATUS", status, 36);

            this.addWidget("button", "API 키 유효성 확인", null, async () => {
                const key = input.value.trim();
                if (!key) {
                    status.textContent = "API 키를 입력하세요.";
                    status.style.color = "#ffb74d";
                    return;
                }
                status.textContent = "OpenAI에서 확인 중...";
                status.style.color = "#90caf9";
                try {
                    const response = await api.fetchApi("/art_ai_openai/validate_key", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ api_key: key }),
                    });
                    const result = await response.json();
                    status.textContent = result.message || (response.ok ? "확인 완료" : "확인 실패");
                    status.style.color = response.ok ? "#81c784" : "#ef9a9a";
                    if (response.ok) input.value = "";
                } catch (_) {
                    status.textContent = "ComfyUI 서버와 통신할 수 없습니다.";
                    status.style.color = "#ef9a9a";
                }
            });

            this.size = [360, 190];
        };
    },
});

app.registerExtension({
    name: "ARTAI.TripoAPIKey",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_TripoAPIKey") return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            const input = document.createElement("input");
            input.type = "password";
            input.placeholder = "tsk_...";
            input.autocomplete = "off";
            input.style.cssText = "display:block;width:100%;max-width:100%;min-width:0;height:34px;box-sizing:border-box;background:#181818;color:#eee;border:1px solid #555;border-radius:4px;padding:6px;";
            addBoundedDOMWidget(this, "tripo_api_key", "ARTAI_TRIPO_API_KEY", input, 42);

            const status = document.createElement("div");
            status.textContent = "Tripo 키가 설정되지 않았습니다.";
            status.style.cssText = "width:100%;max-width:100%;box-sizing:border-box;padding:6px 2px;color:#bbb;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;user-select:none;";
            addBoundedDOMWidget(this, "tripo_status", "ARTAI_TRIPO_STATUS", status, 36);

            this.addWidget("button", "Tripo API 키 유효성 확인", null, async () => {
                const key = input.value.trim();
                if (!key) {
                    status.textContent = "Tripo API 키를 입력하세요.";
                    status.style.color = "#ffb74d";
                    return;
                }
                status.textContent = "Tripo에서 확인 중...";
                status.style.color = "#90caf9";
                try {
                    const response = await api.fetchApi("/art_ai_openai/validate_tripo_key", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ api_key: key }),
                    });
                    const result = await response.json();
                    status.textContent = result.message || (response.ok ? "확인 완료" : "확인 실패");
                    status.style.color = response.ok ? "#81c784" : "#ef9a9a";
                    if (response.ok) input.value = "";
                } catch (_) {
                    status.textContent = "ComfyUI 서버와 통신할 수 없습니다.";
                    status.style.color = "#ef9a9a";
                }
            });
            this.size = [360, 190];
        };
    },
});

app.registerExtension({
    name: "ARTAI.GPTImageDisplaySave",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_GPTImageDisplay") return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            this.addWidget("button", "표시 이미지 저장", null, async () => {
                const images = this.imgs || [];
                const index = Math.max(0, Math.min(this.imageIndex || 0, images.length - 1));
                const src = images[index]?.src;
                if (!src) {
                    alert("먼저 워크플로우를 실행해 이미지를 표시하세요.");
                    return;
                }
                const response = await fetch(src);
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `gpt-image-${Date.now()}.png`;
                anchor.click();
                URL.revokeObjectURL(url);
            });
        };
    },
});

app.registerExtension({
    name: "ARTAI.QwenImagePreviewDownload",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_QwenImagePreviewDownload") return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            this.addWidget("button", "Qwen 이미지 다운로드", null, async () => {
                const images = this.imgs || [];
                const index = Math.max(0, Math.min(this.imageIndex || 0, images.length - 1));
                const src = images[index]?.src;
                if (!src) {
                    alert("먼저 Qwen 워크플로우를 실행해 이미지를 표시하세요.");
                    return;
                }
                const response = await fetch(src);
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `qwen-image-${Date.now()}.png`;
                anchor.click();
                URL.revokeObjectURL(url);
            });
        };
    },
});
