# ART AI OpenAI Nodes for ComfyUI

ComfyUI custom nodes that call the OpenAI API directly with `OPENAI_API_KEY`.
They do not use Partner Nodes or Comfy credits.

## Nodes

- **OpenAI API Key Check** validates a key from a password field and keeps it in
  ComfyUI server memory only.
- **GPT Prompt Input** provides one multiline field for the complete multilingual
  request without calling an API.
- **GPT Prompt Generator** converts a multilingual game-art brief into an English image prompt.
- **GPT Image 2 Generate** returns one or more generated images as a ComfyUI `IMAGE` batch.
- **GPT Image 2 Edit** edits every image in an input batch and accepts an optional ComfyUI `MASK`.
- **GPT Image Display** shows an input image batch inside the node using temporary
  preview files, provides a browser download button, and passes the IMAGE onward.
- **Qwen Prompt Input** provides one multiline prompt field for local Qwen-Image.
- **Qwen Image Preview & Download** previews local Qwen output, downloads the
  displayed PNG in the browser, and passes the ComfyUI `IMAGE` onward.
- **Tripo API Key Check** validates a Tripo developer key and shows API credit balance.
- **Tripo Image to 3D · Smart LowPoly** uploads one image, generates a textured PBR
  Smart LowPoly mesh with Tripo v3.1, generates UVs and source-image-aligned
  textures, downloads the PBR GLB, and provides an in-node browser download button.
- **Tripo 3D Preview · Animation** accepts that `glb_path` through a normal socket
  and displays it with ComfyUI's official animated 3D viewer.
- **Tripo Extract Base Color Texture** reads the PBR base-color texture embedded
  in the generated GLB and returns it as a ComfyUI `IMAGE` for preview or saving.

## Install

Copy this directory into `ComfyUI/custom_nodes/`, then install dependencies with
ComfyUI's Python interpreter:

```powershell
python -m pip install -r requirements.txt
```

Set the API key in the same environment used to launch ComfyUI:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
python main.py
```

For a persistent Windows setup, configure `OPENAI_API_KEY` in the service,
launcher, or user environment and restart ComfyUI. Never put the key in a workflow.

Alternatively, use **OpenAI API Key Check** as the first workflow node. Enter the
key and click **API 키 유효성 확인**. The input is not saved in workflow JSON and
is cleared after successful validation. Because the key is memory-only, repeat the
check after restarting ComfyUI.

## Notes

- The image model is fixed to `gpt-image-2`; the prompt model is fixed to `gpt-5-mini`.
- GPT Image 2 Generate exposes only the official standard sizes: `1024x1024`,
  `1536x1024`, and `1024x1536`.
- GPT Image 2 Generate always creates exactly one image per run.
- Generated API images are decoded to RGB float tensors shaped `[B,H,W,C]`, so they
  connect directly to **Save Image** and standard post-processing nodes.
- `gpt-image-2` currently rejects transparent-background generation. Choose `auto`
  or `opaque`, then use a ComfyUI background-removal node.
- In a ComfyUI mask, white means selected/editable. The node converts that convention
  to the alpha-mask convention expected by the Images API.
- GPT Image API access may require OpenAI organization verification.

## Ready-to-use workflows

- `workflows/api_generate.json`: multilingual brief → prompt generation → image generation → Save Image
- `workflows/api_edit.json`: Load Image → image edit → Save Image

See `workflows/README.md` for loading and usage instructions. Generation and edit
are separate workflows to prevent accidental duplicate API calls and cost.
