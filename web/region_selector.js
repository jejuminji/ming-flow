import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

function imageUrl(file) {
    const params = new URLSearchParams({
        filename: file.filename,
        subfolder: file.subfolder || "",
        type: file.type || "temp",
        preview: "webp;90",
        v: Date.now().toString(),
    });
    return api.apiURL(`/view?${params.toString()}`);
}

app.registerExtension({
    name: "MingFlow.RegionSelector",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ARTAI_MingFlowRegionSelector") return;

        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);

            const maskWidget = this.widgets?.find((widget) => widget.name === "mask_data");
            if (!maskWidget) return;
            maskWidget.type = "hidden";
            maskWidget.hidden = true;
            maskWidget.computeSize = () => [0, 0];
            maskWidget.draw = () => {};

            const root = document.createElement("div");
            root.style.cssText = "display:flex;flex-direction:column;gap:8px;width:100%;height:100%;padding:6px;box-sizing:border-box;color:#ddd;overflow:hidden;";

            const stage = document.createElement("div");
            stage.style.cssText = "position:relative;flex:0 0 auto;width:100%;min-height:220px;background:#181818;border:1px solid #555;border-radius:6px;overflow:hidden;touch-action:none;";
            const emptyState = document.createElement("div");
            emptyState.textContent = "먼저 이 노드까지 실행하면 이미지가 표시됩니다.";
            emptyState.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;text-align:center;color:#999;font-size:12px;";
            const image = document.createElement("img");
            image.draggable = false;
            image.style.display = "none";
            const overlay = document.createElement("canvas");
            overlay.style.cssText = "display:none;width:100%;height:auto;cursor:crosshair;touch-action:none;";
            stage.append(emptyState, overlay);

            const controls = document.createElement("div");
            controls.style.cssText = "display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:12px;";
            const brushLabel = document.createElement("span");
            brushLabel.textContent = "브러시 48";
            const brush = document.createElement("input");
            brush.type = "range";
            brush.min = "4";
            brush.max = "240";
            brush.value = "48";
            brush.style.flex = "1";
            brush.addEventListener("input", () => brushLabel.textContent = `브러시 ${brush.value}`);

            const erase = document.createElement("button");
            erase.type = "button";
            erase.textContent = "지우개 끔";
            erase.style.cssText = "padding:3px 7px;";
            let erasing = false;
            erase.addEventListener("click", () => {
                erasing = !erasing;
                erase.textContent = erasing ? "지우개 켬" : "지우개 끔";
            });

            const invert = document.createElement("button");
            invert.type = "button";
            invert.textContent = "반전";
            invert.style.cssText = "padding:3px 7px;";
            const clear = document.createElement("button");
            clear.type = "button";
            clear.textContent = "초기화";
            clear.style.cssText = "padding:3px 7px;";
            controls.append(brushLabel, brush, erase, invert, clear);

            const help = document.createElement("div");
            help.textContent = "초록색으로 칠한 영역만 GPT가 수정합니다.";
            help.style.cssText = "font-size:11px;color:#9ccc9c;";
            root.append(stage, controls, help);
            let editorHeight = 330;
            const editorWidget = this.addDOMWidget("region_selector", "MINGFLOW_MASK_EDITOR", root, {
                serialize: false,
                hideOnZoom: false,
            });
            editorWidget.computeSize = (width) => {
                if (image.naturalWidth && image.naturalHeight) {
                    editorHeight = Math.round(Math.max(width - 12, 1) * image.naturalHeight / image.naturalWidth) + 82;
                }
                return [width, editorHeight];
            };
            this.setSize([420, 400]);

            const onResize = this.onResize;
            this.onResize = function (size) {
                onResize?.apply(this, arguments);
                const minimumHeight = this.computeSize()[1];
                if (size[1] < minimumHeight) size[1] = minimumHeight;
            };

            const maskCanvas = document.createElement("canvas");
            const maskContext = maskCanvas.getContext("2d", { willReadFrequently: true });
            const overlayContext = overlay.getContext("2d");
            let drawing = false;
            let lastPoint = null;

            const saveMask = () => {
                maskWidget.value = maskCanvas.toDataURL("image/png");
                maskWidget.callback?.(maskWidget.value);
                app.graph.setDirtyCanvas(true, true);
            };

            const redrawOverlay = () => {
                if (!image.naturalWidth || !overlay.width) return;
                overlayContext.clearRect(0, 0, overlay.width, overlay.height);
                overlayContext.drawImage(image, 0, 0, overlay.width, overlay.height);
                const source = maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height);
                const visible = overlayContext.getImageData(0, 0, overlay.width, overlay.height);
                for (let index = 0; index < source.data.length; index += 4) {
                    const opacity = source.data[index] / 255 * 0.55;
                    visible.data[index] = Math.round(visible.data[index] * (1 - opacity) + 30 * opacity);
                    visible.data[index + 1] = Math.round(visible.data[index + 1] * (1 - opacity) + 255 * opacity);
                    visible.data[index + 2] = Math.round(visible.data[index + 2] * (1 - opacity) + 90 * opacity);
                }
                overlayContext.putImageData(visible, 0, 0);
            };

            const pointFromEvent = (event) => {
                const bounds = overlay.getBoundingClientRect();
                return {
                    x: (event.clientX - bounds.left) * overlay.width / bounds.width,
                    y: (event.clientY - bounds.top) * overlay.height / bounds.height,
                };
            };

            const drawLine = (from, to) => {
                maskContext.save();
                maskContext.globalCompositeOperation = erasing ? "destination-out" : "source-over";
                maskContext.strokeStyle = "white";
                maskContext.lineCap = "round";
                maskContext.lineJoin = "round";
                maskContext.lineWidth = Number(brush.value) * overlay.width / Math.max(overlay.clientWidth, 1);
                maskContext.beginPath();
                maskContext.moveTo(from.x, from.y);
                maskContext.lineTo(to.x, to.y);
                maskContext.stroke();
                maskContext.restore();
                redrawOverlay();
            };

            overlay.addEventListener("pointerdown", (event) => {
                if (!overlay.width) return;
                event.preventDefault();
                overlay.setPointerCapture(event.pointerId);
                drawing = true;
                lastPoint = pointFromEvent(event);
                drawLine(lastPoint, lastPoint);
            });
            overlay.addEventListener("pointermove", (event) => {
                if (!drawing) return;
                const point = pointFromEvent(event);
                drawLine(lastPoint, point);
                lastPoint = point;
            });
            const finishDrawing = () => {
                if (!drawing) return;
                drawing = false;
                lastPoint = null;
                saveMask();
            };
            overlay.addEventListener("pointerup", finishDrawing);
            overlay.addEventListener("pointercancel", finishDrawing);

            clear.addEventListener("click", () => {
                maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
                redrawOverlay();
                saveMask();
            });
            invert.addEventListener("click", () => {
                const pixels = maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height);
                for (let index = 0; index < pixels.data.length; index += 4) {
                    const value = 255 - pixels.data[index];
                    pixels.data[index] = value;
                    pixels.data[index + 1] = value;
                    pixels.data[index + 2] = value;
                    pixels.data[index + 3] = 255;
                }
                maskContext.putImageData(pixels, 0, 0);
                redrawOverlay();
                saveMask();
            });

            const loadStoredMask = () => {
                if (!maskWidget.value) {
                    redrawOverlay();
                    return;
                }
                const stored = new Image();
                stored.onload = () => {
                    if (stored.naturalWidth !== maskCanvas.width || stored.naturalHeight !== maskCanvas.height) {
                        maskWidget.value = "";
                        redrawOverlay();
                        return;
                    }
                    maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
                    maskContext.drawImage(stored, 0, 0);
                    redrawOverlay();
                };
                stored.src = maskWidget.value;
            };

            image.addEventListener("load", () => {
                const width = image.naturalWidth;
                const height = image.naturalHeight;
                const dimensionsChanged = maskCanvas.width !== width || maskCanvas.height !== height;
                maskCanvas.width = overlay.width = width;
                maskCanvas.height = overlay.height = height;
                stage.style.minHeight = "0";
                emptyState.style.display = "none";
                overlay.style.display = "block";
                if (dimensionsChanged && !maskWidget.value) {
                    maskContext.clearRect(0, 0, width, height);
                }
                loadStoredMask();
                const nodeWidth = Math.max(this.size[0], 420);
                const computed = this.computeSize();
                this.setSize([nodeWidth, computed[1]]);
                app.graph.setDirtyCanvas(true, true);
            });

            const onExecuted = this.onExecuted;
            this.onExecuted = function (message) {
                onExecuted?.apply(this, arguments);
                const file = message?.region_image?.[0] || message?.images?.[0];
                if (file) {
                    image.src = imageUrl(file);
                    this.imgs = null;
                }
            };

            const showUpstreamPreview = () => {
                const linkId = this.inputs?.[0]?.link;
                const link = linkId == null ? null : app.graph.links?.[linkId];
                const sourceNode = link ? app.graph.getNodeById(link.origin_id) : null;
                const source = sourceNode?.imgs?.[sourceNode.imageIndex || 0]?.src;
                if (source && image.src !== source) image.src = source;
            };

            const executedHandler = (event) => {
                const detail = event?.detail || {};
                if (String(detail.node) !== String(this.id)) return;
                const output = detail.output || detail;
                const file = output?.region_image?.[0] || output?.images?.[0];
                if (file) image.src = imageUrl(file);
            };
            api.addEventListener("executed", executedHandler);
            root.addEventListener("pointerenter", showUpstreamPreview);
            setTimeout(showUpstreamPreview, 0);

            const onRemoved = this.onRemoved;
            this.onRemoved = function () {
                api.removeEventListener("executed", executedHandler);
                onRemoved?.apply(this, arguments);
            };
        };
    },
});
