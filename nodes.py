"""ComfyUI nodes backed directly by the OpenAI API."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import struct
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
from PIL import Image
from comfy_execution.graph_utils import ExecutionBlocker


IMAGE_MODEL = "gpt-image-2"
PROMPT_MODEL = "gpt-5-mini"
QWEN_DEFAULT_NEGATIVE = (
    "low resolution, low quality, blurry, noisy, deformed geometry, "
    "duplicate object, multiple objects, floating parts, disconnected parts, "
    "cropped object, occlusion, cluttered background, text, logo, watermark"
)
_RUNTIME_API_KEY: str | None = None
_RUNTIME_TRIPO_API_KEY: str | None = None

PROMPT_SYSTEM = """You are a senior game art director and prompt engineer.
Convert the user's possibly multilingual brief into one production-ready English
prompt for an image generation model. Preserve all concrete requirements. Include,
when relevant: subject, pose/action, composition, camera/view direction, palette,
lighting, background, game-art style, asset purpose, and explicit exclusions.
Resolve no ambiguity by inventing story details. Return only the final prompt, with
no heading, explanation, markdown, or quotation marks."""


def _client():
    key = _RUNTIME_API_KEY or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it in the environment that starts ComfyUI."
        )
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The 'openai' package is missing. Install this node's requirements.txt."
        ) from exc
    return OpenAI(api_key=key)


def _require_api_config(api_key):
    if not api_key or not api_key.get("configured"):
        raise RuntimeError(
            "OpenAI API Key Check 노드에서 키 유효성 확인을 먼저 완료하세요."
        )


def _register_api_routes():
    """Register routes in ComfyUI while keeping standalone tests importable."""
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return

    @PromptServer.instance.routes.post("/art_ai_openai/validate_key")
    async def validate_key(request):
        global _RUNTIME_API_KEY
        body = await request.json()
        key = str(body.get("api_key", "")).strip()
        if not key:
            return web.json_response(
                {"ok": False, "message": "API 키를 입력하세요."}, status=400
            )
        try:
            from openai import AsyncOpenAI

            # Model listing validates authentication without generating content.
            await AsyncOpenAI(api_key=key).models.list()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            message = "API 키가 유효하지 않거나 OpenAI에 연결할 수 없습니다."
            if status == 401:
                message = "API 키가 유효하지 않습니다."
            elif status == 429:
                message = "API 키는 인식됐지만 사용 한도 또는 요청 제한을 확인해야 합니다."
            return web.json_response({"ok": False, "message": message}, status=400)
        _RUNTIME_API_KEY = key
        return web.json_response(
            {"ok": True, "message": "API 키 확인 완료 · 서버 메모리에만 보관됨"}
        )

    @PromptServer.instance.routes.post("/art_ai_openai/validate_tripo_key")
    async def validate_tripo_key(request):
        global _RUNTIME_TRIPO_API_KEY
        body = await request.json()
        key = str(body.get("api_key", "")).strip()
        if not key:
            return web.json_response(
                {"ok": False, "message": "Tripo API 키를 입력하세요."}, status=400
            )
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    "https://api.tripo3d.ai/v2/openapi/user/balance",
                    headers={"Authorization": f"Bearer {key}"},
                )
            payload = response.json()
            if response.status_code != 200 or payload.get("code") != 0:
                raise RuntimeError(payload.get("message", "authentication failed"))
            balance = payload.get("data", {}).get("balance", "?")
        except Exception:
            return web.json_response(
                {"ok": False, "message": "Tripo API 키가 유효하지 않거나 연결할 수 없습니다."},
                status=400,
            )
        _RUNTIME_TRIPO_API_KEY = key
        return web.json_response(
            {"ok": True, "message": f"Tripo 키 확인 완료 · 잔액 {balance} credits"}
        )


_register_api_routes()


def _decode_images(items: Iterable[object]) -> torch.Tensor:
    tensors = []
    for item in items:
        encoded = getattr(item, "b64_json", None)
        if not encoded:
            raise RuntimeError("OpenAI returned an image without b64_json data.")
        try:
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        except Exception as exc:
            raise RuntimeError("Could not decode an image returned by OpenAI.") from exc
        tensors.append(torch.from_numpy(array.copy()))
    if not tensors:
        raise RuntimeError("OpenAI returned no images.")
    shapes = {tuple(t.shape) for t in tensors}
    if len(shapes) != 1:
        raise RuntimeError("OpenAI returned images with different dimensions.")
    return torch.stack(tensors, dim=0)


def _tensor_to_png(image: torch.Tensor, name: str = "image.png") -> io.BytesIO:
    if image.ndim != 3 or image.shape[-1] not in (3, 4):
        raise ValueError("IMAGE must have shape [H, W, 3] or [H, W, 4].")
    array = (
        image.detach().cpu().float().clamp(0, 1).mul(255).round().to(torch.uint8).numpy()
    )
    mode = "RGBA" if array.shape[-1] == 4 else "RGB"
    stream = io.BytesIO()
    Image.fromarray(array, mode=mode).save(stream, format="PNG")
    stream.seek(0)
    stream.name = name
    return stream


def _mask_to_png(mask: torch.Tensor, name: str = "mask.png") -> io.BytesIO:
    if mask.ndim == 3:
        mask = mask[0]
    if mask.ndim != 2:
        raise ValueError("MASK must have shape [H, W] or [B, H, W].")
    # ComfyUI MASK: 1 means selected. Images API alpha: 0 means editable.
    alpha = (1.0 - mask.detach().cpu().float().clamp(0, 1))
    alpha_array = alpha.mul(255).round().to(torch.uint8).numpy()
    array = np.zeros((*alpha_array.shape, 4), dtype=np.uint8)
    array[..., :3] = 255
    array[..., 3] = alpha_array
    stream = io.BytesIO()
    Image.fromarray(array, mode="RGBA").save(stream, format="PNG")
    stream.seek(0)
    stream.name = name
    return stream


class GPTPromptInput:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "만들고 싶은 이미지의 내용, 스타일, 구도와 제약사항을 자유롭게 작성하세요.",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("request_text",)
    FUNCTION = "build"
    CATEGORY = "🌊 MingFlow/OpenAI"

    def build(self, prompt):
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("GPT에 보낼 내용을 입력하세요.")
        return (prompt,)


class QwenPromptInput:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "negative_prompt": (
                    "STRING",
                    {"multiline": True, "default": " "},
                ),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "negative_prompt")
    FUNCTION = "build"
    CATEGORY = "🌊 MingFlow/Qwen Local"

    def build(self, prompt, negative_prompt):
        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(20, 100)
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Qwen에 보낼 프롬프트를 입력하세요.")
        progress.update_absolute(100, 100)
        return (prompt, negative_prompt.strip())


class QwenImageGenerateLocal:
    """Run the native ComfyUI Qwen-Image 2512 pipeline behind one node."""

    DEFAULT_UNET = "qwen_image_2512_fp8_e4m3fn.safetensors"
    DEFAULT_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    DEFAULT_VAE = "qwen_image_vae.safetensors"
    DEFAULT_NEGATIVE = QWEN_DEFAULT_NEGATIVE

    def __init__(self):
        self._loaded_key = None
        self._unet = None
        self._clip = None
        self._vae = None
        self._diffusers_pipeline = None
        self._diffusers_pipeline_key = None

    @staticmethod
    def _model_choices(folder_name, preferred):
        import folder_paths

        aliases = {
            "diffusion_models": ("diffusion_models", "unet"),
            "text_encoders": ("text_encoders", "clip"),
            "vae": ("vae",),
        }
        available = folder_paths.folder_names_and_paths
        active_name = next(
            (name for name in aliases.get(folder_name, (folder_name,)) if name in available),
            None,
        )
        choices = (
            list(folder_paths.get_filename_list(active_name)) if active_name else []
        )
        matching_choice = next(
            (choice for choice in choices if Path(choice).name == preferred), None
        )
        default_choice = matching_choice or preferred
        # Keep paths written by older split-file workflows valid during
        # ComfyUI's pre-execution dropdown validation. _load_models resolves
        # these aliases to whichever current entry has the same basename.
        compatibility_choices = [
            preferred,
            f"split_files/{folder_name}/{preferred}",
        ]
        ordered_choices = [default_choice, *compatibility_choices, *choices]
        return list(dict.fromkeys(ordered_choices))

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"forceInput": True}),
                "unet_name": (cls._model_choices("diffusion_models", cls.DEFAULT_UNET),),
                "clip_name": (cls._model_choices("text_encoders", cls.DEFAULT_CLIP),),
                "vae_name": (cls._model_choices("vae", cls.DEFAULT_VAE),),
                "width": ("INT", {"default": 1328, "min": 256, "max": 4096, "step": 16}),
                "height": ("INT", {"default": 1328, "min": 256, "max": 4096, "step": 16}),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                    },
                ),
                "steps": ("INT", {"default": 50, "min": 1, "max": 100, "step": 1}),
                "cfg": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 20.0, "step": 0.1}),
                "negative_prompt": ("STRING", {"forceInput": True}),
                "model_directory": (
                    "STRING",
                    {
                        "default": "/workspace/models/qwen",
                        "multiline": False,
                        "placeholder": "/workspace/models/qwen-image-2512",
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"
    CATEGORY = "🌊 MingFlow/Qwen Local"

    @staticmethod
    def _register_model_directory(model_directory):
        raw_directory = str(model_directory or "").strip()
        if not raw_directory:
            return ""

        import folder_paths

        root = Path(raw_directory).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Qwen 모델 폴더를 찾을 수 없습니다: {root}")

        directory_map = {
            ("diffusion_models", "unet"): ("diffusion_models", "unet"),
            ("text_encoders", "clip"): ("text_encoders", "clip"),
            ("vae",): ("vae",),
        }
        for folder_aliases, candidates in directory_map.items():
            model_path = root
            for candidate in candidates:
                candidate_paths = (
                    root / candidate,
                    root / "split_files" / candidate,
                )
                discovered_path = next(
                    (path for path in candidate_paths if path.is_dir()), None
                )
                if discovered_path is not None:
                    model_path = discovered_path
                    break
            # ComfyUI renamed unet -> diffusion_models and clip ->
            # text_encoders. Register both names so old RunPod templates and
            # current ComfyUI builds resolve the same external model folder.
            for folder_name in folder_aliases:
                try:
                    folder_paths.add_model_folder_path(
                        folder_name, str(model_path), is_default=True
                    )
                except TypeError:
                    # Compatibility with older ComfyUI builds whose helper
                    # does not yet expose the is_default argument.
                    folder_paths.add_model_folder_path(folder_name, str(model_path))
                cache = getattr(folder_paths, "filename_list_cache", None)
                if cache is not None:
                    cache.pop(folder_name, None)
        return str(root)

    def _load_models(self, unet_name, clip_name, vae_name, model_directory=""):
        registered_directory = self._register_model_directory(model_directory)

        import folder_paths

        def resolve_name(folder_aliases, selected_name):
            selected_name = str(selected_name or "").strip()
            available = folder_paths.folder_names_and_paths
            active_name = next(
                (name for name in folder_aliases if name in available), None
            )
            if active_name is None:
                raise RuntimeError(
                    f"ComfyUI 모델 경로 키가 없습니다: {', '.join(folder_aliases)}"
                )
            choices = list(folder_paths.get_filename_list(active_name))
            if selected_name in choices:
                return selected_name
            basename_match = next(
                (
                    choice
                    for choice in choices
                    if Path(choice).name == Path(selected_name).name
                ),
                None,
            )
            if basename_match:
                return basename_match

            # Older ComfyUI builds can retain a stale filename-list cache
            # after add_model_folder_path(). Locate the selected checkpoint
            # directly, register its exact parent for both old/new aliases,
            # then ask folder_paths for the loader-safe name again.
            selected_basename = Path(selected_name).name
            model_root = Path(registered_directory) if registered_directory else None
            direct_matches = (
                [
                    path
                    for path in model_root.rglob(selected_basename)
                    if path.is_file()
                ]
                if model_root and model_root.is_dir()
                else []
            )
            if direct_matches:
                exact_parent = str(direct_matches[0].parent)
                for folder_name in folder_aliases:
                    try:
                        folder_paths.add_model_folder_path(
                            folder_name, exact_parent, is_default=True
                        )
                    except TypeError:
                        folder_paths.add_model_folder_path(folder_name, exact_parent)
                    cache = getattr(folder_paths, "filename_list_cache", None)
                    if cache is not None:
                        cache.pop(folder_name, None)

                active_name = next(
                    (
                        name
                        for name in folder_aliases
                        if name in folder_paths.folder_names_and_paths
                    ),
                    active_name,
                )
                refreshed_choices = list(folder_paths.get_filename_list(active_name))
                direct_choice = next(
                    (
                        choice
                        for choice in refreshed_choices
                        if Path(choice).name == selected_basename
                    ),
                    None,
                )
                if direct_choice:
                    return direct_choice
            raise FileNotFoundError(
                f"{active_name}에서 모델을 찾을 수 없습니다: {selected_name}. "
                f"model_directory={registered_directory or '(ComfyUI 기본 경로)'}"
            )

        unet_name = resolve_name(("diffusion_models", "unet"), unet_name)
        clip_name = resolve_name(("text_encoders", "clip"), clip_name)
        vae_name = resolve_name(("vae",), vae_name)
        key = (registered_directory, unet_name, clip_name, vae_name)
        if self._loaded_key == key:
            return self._unet, self._clip, self._vae

        import nodes

        self._unet = nodes.UNETLoader().load_unet(unet_name, "default")[0]
        self._clip = nodes.CLIPLoader().load_clip(
            clip_name, type="qwen_image", device="default"
        )[0]
        self._vae = nodes.VAELoader().load_vae(vae_name)[0]
        self._loaded_key = key
        return self._unet, self._clip, self._vae

    def _load_diffusers_pipeline(self, model_directory):
        model_path = Path(str(model_directory)).expanduser().resolve()
        model_index = model_path / "model_index.json"
        if not model_index.is_file():
            return None
        pipeline_key = str(model_path)
        if (
            self._diffusers_pipeline is not None
            and self._diffusers_pipeline_key == pipeline_key
        ):
            # The previous run is kept in system RAM so TRELLIS2/CuMesh can
            # use the full GPU. Move it back only when Qwen runs again.
            self._diffusers_pipeline.to("cuda")
            return self._diffusers_pipeline
        try:
            from diffusers import QwenImagePipeline
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Diffusers 형식 Qwen-Image-2512 모델을 로드할 수 없습니다. "
                "Qwen 공식 최신 diffusers와 transformers>=4.51.3,<5를 설치하세요. "
                f"원래 import 오류: {type(exc).__name__}: {exc}"
            ) from exc

        os.environ.setdefault("HF_ENABLE_PARALLEL_LOADING", "yes")
        self._diffusers_pipeline = QwenImagePipeline.from_pretrained(
            pipeline_key,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
            low_cpu_mem_usage=True,
        ).to("cuda")
        self._diffusers_pipeline_key = pipeline_key
        return self._diffusers_pipeline

    def generate(
        self,
        prompt,
        unet_name,
        clip_name,
        vae_name,
        width,
        height,
        seed,
        steps,
        cfg,
        negative_prompt,
        model_directory="",
    ):
        prompt = str(prompt).strip()
        if not prompt:
            raise ValueError("Qwen Prompt Input의 prompt 출력을 연결하세요.")

        import comfy.model_management
        import comfy.utils
        import nodes
        from comfy_extras.nodes_model_advanced import ModelSamplingAuraFlow

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(2, 100)
        diffusers_pipeline = self._load_diffusers_pipeline(model_directory)
        if diffusers_pipeline is not None:
            progress.update_absolute(15, 100)
            generator = torch.Generator(device="cuda").manual_seed(int(seed))

            def report_step(pipe, step_index, timestep, callback_kwargs):
                del pipe, timestep
                completed = 15 + round(
                    78 * (int(step_index) + 1) / max(int(steps), 1)
                )
                progress.update_absolute(min(completed, 93), 100)
                return callback_kwargs

            pipeline_kwargs = {
                "prompt": prompt,
                "negative_prompt": str(negative_prompt),
                "width": int(width),
                "height": int(height),
                "num_inference_steps": int(steps),
                "true_cfg_scale": float(cfg),
                "generator": generator,
                "callback_on_step_end": report_step,
            }
            try:
                pil_image = diffusers_pipeline(**pipeline_kwargs).images[0]
            except TypeError as exc:
                # Compatibility with the earliest QwenImagePipeline release,
                # which did not expose the step-end callback yet.
                if "callback_on_step_end" not in str(exc):
                    raise
                pipeline_kwargs.pop("callback_on_step_end")
                pil_image = diffusers_pipeline(**pipeline_kwargs).images[0]
            progress.update_absolute(96, 100)
            image_array = np.asarray(
                pil_image.convert("RGB"), dtype=np.float32
            ) / 255.0
            result = (torch.from_numpy(image_array).unsqueeze(0),)
            progress.update_absolute(97, 100)
            # Diffusers pipelines are outside ComfyUI's model manager. If we
            # leave Qwen-Image-2512 BF16 on CUDA, TRELLIS2's UV parameterizer
            # can stall while both large pipelines compete for H100 VRAM.
            diffusers_pipeline.to("cpu")
            torch.cuda.empty_cache()
            progress.update_absolute(100, 100)
            return result

        unet, clip, vae = self._load_models(
            unet_name, clip_name, vae_name, model_directory
        )
        progress.update_absolute(12, 100)
        model = ModelSamplingAuraFlow().patch_aura(unet, 3.1)[0]
        positive = nodes.CLIPTextEncode().encode(clip, prompt)[0]
        negative = nodes.CLIPTextEncode().encode(clip, negative_prompt)[0]
        progress.update_absolute(18, 100)
        latent = {
            "samples": torch.zeros(
                [1, 16, int(height) // 8, int(width) // 8],
                device=comfy.model_management.intermediate_device(),
                dtype=comfy.model_management.intermediate_dtype(),
            ),
            "downscale_ratio_spacial": 8,
        }
        samples = nodes.KSampler().sample(
            model=model,
            seed=int(seed),
            steps=int(steps),
            cfg=float(cfg),
            sampler_name="euler",
            scheduler="simple",
            positive=positive,
            negative=negative,
            latent_image=latent,
            denoise=1.0,
        )[0]
        progress.update_absolute(92, 100)
        images = nodes.VAEDecode().decode(vae, samples)
        progress.update_absolute(100, 100)
        return images


class QwenImageGenerateLocalV2(QwenImageGenerateLocal):
    """Previous schema ID kept only so existing workflows can be opened."""


class QwenImageGenerateLocalV3(QwenImageGenerateLocal):
    """Previous single-checkpoint schema kept for workflow compatibility."""


class QwenImageGenerateDiffusersBF16V4(QwenImageGenerateLocal):
    """Load an official local Qwen Diffusers folder and run it in BF16."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"forceInput": True}),
                "negative_prompt": ("STRING", {"forceInput": True}),
                "model_directory": (
                    "STRING",
                    {
                        "default": "/workspace/models/qwen",
                        "multiline": False,
                        "placeholder": "/workspace/models/qwen",
                    },
                ),
                "width": (
                    "INT",
                    {"default": 1328, "min": 256, "max": 4096, "step": 16},
                ),
                "height": (
                    "INT",
                    {"default": 1328, "min": 256, "max": 4096, "step": 16},
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                    },
                ),
                "steps": ("INT", {"default": 50, "min": 1, "max": 100, "step": 1}),
                "cfg": (
                    "FLOAT",
                    {"default": 4.0, "min": 0.0, "max": 20.0, "step": 0.1},
                ),
            }
        }

    def generate(
        self,
        prompt,
        negative_prompt,
        model_directory,
        width,
        height,
        seed,
        steps,
        cfg,
    ):
        model_path = Path(str(model_directory)).expanduser().resolve()
        if not (model_path / "model_index.json").is_file():
            raise FileNotFoundError(
                "Qwen Diffusers model_index.json을 찾을 수 없습니다: "
                f"{model_path / 'model_index.json'}"
            )
        return super().generate(
            prompt=prompt,
            unet_name=self.DEFAULT_UNET,
            clip_name=self.DEFAULT_CLIP,
            vae_name=self.DEFAULT_VAE,
            width=width,
            height=height,
            seed=seed,
            steps=steps,
            cfg=cfg,
            negative_prompt=negative_prompt,
            model_directory=str(model_path),
        )


class QwenImageEditDiffusersBF16:
    """Edit a ComfyUI IMAGE with a local official Qwen-Image-Edit folder."""

    def __init__(self):
        self._pipeline = None
        self._pipeline_key = None

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "edit_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "이미지에서 바꿀 내용을 구체적으로 입력하세요.",
                    },
                ),
                "negative_prompt": (
                    "STRING",
                    {"multiline": True, "default": " "},
                ),
                "model_directory": (
                    "STRING",
                    {
                        "default": "/workspace/models/Qwen-Image-Edit-2511",
                        "multiline": False,
                        "placeholder": "/workspace/models/Qwen-Image-Edit-2511",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                    },
                ),
                "steps": ("INT", {"default": 40, "min": 1, "max": 100, "step": 1}),
                "cfg": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 20.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("edited_image",)
    FUNCTION = "edit"
    CATEGORY = "🌊 MingFlow/Qwen Local"

    def _load_pipeline(self, model_directory):
        model_path = Path(str(model_directory)).expanduser().resolve()
        if not (model_path / "model_index.json").is_file():
            raise FileNotFoundError(
                "Qwen Image Edit model_index.json을 찾을 수 없습니다: "
                f"{model_path / 'model_index.json'}"
            )

        pipeline_key = str(model_path)
        if self._pipeline is not None and self._pipeline_key == pipeline_key:
            self._pipeline.to("cuda")
            return self._pipeline

        try:
            from diffusers import QwenImageEditPlusPipeline
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Qwen-Image-Edit-2511을 지원하는 최신 diffusers가 필요합니다. "
                "이 노드의 requirements.txt를 다시 설치하세요. "
                f"원래 import 오류: {type(exc).__name__}: {exc}"
            ) from exc

        os.environ.setdefault("HF_ENABLE_PARALLEL_LOADING", "yes")
        try:
            model_config = json.loads((model_path / "model_index.json").read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Qwen model_index.json을 읽을 수 없습니다: {exc}") from exc
        if "Plus" not in str(model_config.get("_class_name", "")):
            raise ValueError(
                "이 노드는 Qwen-Image-Edit-2511의 QwenImageEditPlusPipeline 전용입니다."
            )
        self._pipeline = QwenImageEditPlusPipeline.from_pretrained(
            pipeline_key,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
            low_cpu_mem_usage=True,
        )
        self._pipeline.to("cuda")
        self._pipeline_key = pipeline_key
        return self._pipeline

    @staticmethod
    def _image_to_pil(frame):
        array = frame.detach().cpu().float().clamp(0, 1).mul(255).round()
        return Image.fromarray(array.to(torch.uint8).numpy(), mode="RGB")

    def edit(
        self,
        image,
        edit_prompt,
        negative_prompt,
        model_directory,
        seed,
        steps,
        cfg,
    ):
        edit_prompt = str(edit_prompt).strip()
        if not edit_prompt:
            raise ValueError("Qwen으로 수정할 내용을 입력하세요.")

        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(2, 100)
        pipeline = self._load_pipeline(model_directory)
        outputs = []
        total_frames = len(image)

        try:
            for index, frame in enumerate(image):
                source = self._image_to_pil(frame)
                generator = torch.Generator(device="cuda").manual_seed(
                    int(seed) + index
                )

                def report_step(pipe, step_index, timestep, callback_kwargs):
                    del pipe, timestep
                    completed_steps = index * int(steps) + int(step_index) + 1
                    all_steps = max(total_frames * int(steps), 1)
                    progress.update_absolute(
                        min(10 + round(82 * completed_steps / all_steps), 92), 100
                    )
                    return callback_kwargs

                kwargs = {
                    "image": source,
                    "prompt": edit_prompt,
                    "negative_prompt": str(negative_prompt) or " ",
                    "num_inference_steps": int(steps),
                    "true_cfg_scale": float(cfg),
                    "guidance_scale": 1.0,
                    "generator": generator,
                    "callback_on_step_end": report_step,
                }
                result = pipeline(**kwargs).images[0].convert("RGB")
                array = np.asarray(result, dtype=np.float32) / 255.0
                outputs.append(torch.from_numpy(array.copy()))
        finally:
            # Leave VRAM available to the following TRELLIS/Tripo stages.
            if self._pipeline is not None:
                self._pipeline.to("cpu")
            torch.cuda.empty_cache()

        progress.update_absolute(100, 100)
        return (torch.stack(outputs, dim=0),)


class GPTPromptGenerator:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("OPENAI_API_KEY",),
                "request_text": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "generate"
    CATEGORY = "🌊 MingFlow/OpenAI"

    def generate(self, api_key, request_text):
        _require_api_config(api_key)
        if not request_text.strip():
            raise ValueError("GPT에 보낼 요청 내용을 입력하세요.")
        response = _client().responses.create(
            model=PROMPT_MODEL,
            instructions=PROMPT_SYSTEM,
            input=request_text,
        )
        prompt = (response.output_text or "").strip()
        if not prompt:
            raise RuntimeError("OpenAI returned an empty prompt.")
        return (prompt,)


class OpenAIAPIKey:
    """Frontend-configured, memory-only API key status node."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("OPENAI_API_KEY",)
    RETURN_NAMES = ("api_key",)
    FUNCTION = "status"
    CATEGORY = "🌊 MingFlow/OpenAI"

    def status(self):
        configured = bool(_RUNTIME_API_KEY or os.getenv("OPENAI_API_KEY"))
        if not configured:
            raise RuntimeError(
                "API 키를 입력하고 'API 키 유효성 확인' 버튼을 먼저 누르세요."
            )
        return ({"configured": True},)


class TripoAPIKey:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("TRIPO_API_KEY",)
    RETURN_NAMES = ("api_key",)
    FUNCTION = "status"
    CATEGORY = "🌊 MingFlow/Tripo"

    def status(self):
        configured = bool(_RUNTIME_TRIPO_API_KEY or os.getenv("TRIPO_API_KEY"))
        if not configured:
            raise RuntimeError(
                "Tripo API 키를 입력하고 'Tripo API 키 유효성 확인'을 먼저 누르세요."
            )
        return ({"configured": True},)


class GPTImage2Generate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("OPENAI_API_KEY",),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "size": (["1024x1024", "1536x1024", "1024x1536"],),
                "quality": (["auto", "low", "medium", "high"],),
                "background": (["auto", "opaque", "transparent"],),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"
    CATEGORY = "🌊 MingFlow/OpenAI"

    def generate(self, api_key, prompt, size, quality, background):
        _require_api_config(api_key)
        if background == "transparent":
            raise ValueError(
                "gpt-image-2 currently does not support transparent backgrounds. "
                "Use 'auto' or 'opaque', then connect a background-removal node."
            )
        result = _client().images.generate(
            model=IMAGE_MODEL,
            prompt=prompt,
            size=size,
            quality=quality,
            background=background,
            output_format="png",
            n=1,
        )
        return (_decode_images(result.data),)


class GPTImage2Edit:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("OPENAI_API_KEY",),
                "image": ("IMAGE",),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "size": (["1024x1024", "1536x1024", "1024x1536", "auto"],),
                "quality": (["auto", "low", "medium", "high"],),
                "image_count": ("INT", {"default": 1, "min": 1, "max": 10, "step": 1}),
            },
            "optional": {"mask": ("MASK",)},
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "edit"
    CATEGORY = "🌊 MingFlow/OpenAI"

    def edit(self, api_key, image, prompt, size, quality, image_count, mask=None):
        _require_api_config(api_key)
        all_outputs = []
        for index, frame in enumerate(image):
            image_file = _tensor_to_png(frame, f"image_{index}.png")
            kwargs = {
                "model": IMAGE_MODEL,
                "image": image_file,
                "prompt": prompt,
                "size": size,
                "quality": quality,
                "output_format": "png",
                "n": image_count,
            }
            if mask is not None:
                mask_frame = mask[min(index, len(mask) - 1)] if mask.ndim == 3 else mask
                if tuple(mask_frame.shape) != tuple(frame.shape[:2]):
                    raise ValueError("MASK dimensions must match the input IMAGE dimensions.")
                kwargs["mask"] = _mask_to_png(mask_frame, f"mask_{index}.png")
            result = _client().images.edit(**kwargs)
            all_outputs.append(_decode_images(result.data))
        return (torch.cat(all_outputs, dim=0),)


class GPTImagePartialEdit(GPTImage2Edit):
    """Edit a generated image before passing the approved result to 3D."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("OPENAI_API_KEY",),
                "image": ("IMAGE",),
                "edit_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "수정할 내용을 입력하세요. MASK를 연결하면 흰색 영역만 수정합니다.",
                    },
                ),
                "size": (["입력 이미지 비율", "1024x1024", "1536x1024", "1024x1536", "auto"],),
                "quality": (["auto", "low", "medium", "high"],),
            },
            "optional": {"mask": ("MASK",)},
        }

    RETURN_NAMES = ("edited_image",)
    CATEGORY = "🌊 MingFlow/OpenAI/Edit"

    def edit(self, api_key, image, edit_prompt, size, quality, mask=None):
        if not edit_prompt.strip():
            raise ValueError("수정할 내용을 입력하세요.")
        if size == "입력 이미지 비율":
            height, width = image.shape[1:3]
            if width > height:
                size = "1536x1024"
            elif height > width:
                size = "1024x1536"
            else:
                size = "1024x1024"
        return super().edit(api_key, image, edit_prompt, size, quality, 1, mask)


class MingFlowImageCheckpoint:
    """Persist a generated image and lazily detach later stages from generation."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (["새 이미지 저장", "저장 이미지 재사용"],),
                "checkpoint_name": ("STRING", {"default": "gpt_source"}),
            },
            "optional": {"image": ("IMAGE", {"lazy": True})},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("fixed_image",)
    FUNCTION = "checkpoint"
    CATEGORY = "🌊 MingFlow/Image Edit"

    @classmethod
    def check_lazy_status(cls, mode, checkpoint_name, image=None):
        if mode == "새 이미지 저장" and image is None:
            return ["image"]

    @staticmethod
    def _path(checkpoint_name):
        import folder_paths

        safe_name = "".join(
            character
            for character in checkpoint_name.strip()
            if character.isalnum() or character in "-_"
        )
        if not safe_name:
            raise ValueError("체크포인트 이름을 입력하세요.")
        directory = Path(folder_paths.get_output_directory()) / "MingFlow" / "checkpoints"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_name}.png"

    def checkpoint(self, mode, checkpoint_name, image=None):
        path = self._path(checkpoint_name)
        if mode == "새 이미지 저장":
            if image is None:
                raise ValueError("저장할 생성 이미지를 연결하세요.")
            if image.shape[0] != 1:
                raise ValueError("생성 이미지 고정 노드는 이미지 한 장만 지원합니다.")
            pixels = image[0].detach().cpu().clamp(0, 1).mul(255).round().byte().numpy()
            Image.fromarray(pixels).save(path, format="PNG")
            return (image,)

        if not path.is_file():
            raise FileNotFoundError(
                f"저장된 생성 이미지가 없습니다: {path}. 먼저 '새 이미지 저장'을 실행하세요."
            )
        with Image.open(path) as stored:
            pixels = np.asarray(stored.convert("RGB"), dtype=np.float32) / 255.0
        return (torch.from_numpy(pixels.copy()).unsqueeze(0),)


class MingFlowRegionSelector:
    """Draw and return an edit mask for a single generated image."""

    def __init__(self):
        from nodes import PreviewImage

        self._preview = PreviewImage()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "mask_data": ("STRING", {"default": ""}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)
    FUNCTION = "select"
    CATEGORY = "🌊 MingFlow/Image Edit"
    OUTPUT_NODE = True

    def select(self, image, mask_data, prompt=None, extra_pnginfo=None):
        if image.shape[0] != 1:
            raise ValueError("영역 선택 노드는 이미지 한 장만 지원합니다.")

        height, width = image.shape[1:3]
        if mask_data:
            try:
                encoded = mask_data.split(",", 1)[-1]
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as mask_image:
                    mask_image = mask_image.convert("L")
                    if mask_image.size != (width, height):
                        raise ValueError("저장된 마스크 크기가 입력 이미지와 다릅니다. 마스크를 초기화하세요.")
                    mask_array = np.asarray(mask_image, dtype=np.float32) / 255.0
                mask = torch.from_numpy(mask_array.copy()).unsqueeze(0)
            except (ValueError, TypeError, binascii.Error, OSError) as exc:
                raise ValueError("영역 선택 마스크 데이터를 읽을 수 없습니다. 마스크를 초기화하세요.") from exc
        else:
            mask = torch.zeros((1, height, width), dtype=torch.float32)

        response = self._preview.save_images(
            image,
            filename_prefix="MingFlow/region_selector",
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
        )
        response["ui"]["region_image"] = response["ui"].pop("images")
        response["result"] = (mask,)
        return response


class MingFlowEditApprovalGate:
    """Choose masked edit, whole-image edit, original, or pause lazily."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "stage": (
                    [
                        "마스킹 중 · 정지",
                        "수정 실행",
                        "전체 수정 실행",
                        "수정 없이 진행",
                    ],
                ),
            },
            "optional": {"mask": ("MASK",)},
        }

    RETURN_TYPES = ("IMAGE", "MASK", "MINGFLOW_EDIT_ROUTE")
    RETURN_NAMES = ("image", "mask", "route")
    FUNCTION = "approve"
    CATEGORY = "🌊 MingFlow/Image Edit"

    def approve(self, image, stage, mask=None):
        if stage == "수정 없이 진행":
            blocker = ExecutionBlocker(None)
            return (blocker, blocker, "original")
        if stage == "마스킹 중 · 정지":
            blocker = ExecutionBlocker(None)
            return (blocker, blocker, "blocked")
        if stage == "전체 수정 실행":
            return (image, None, "edited")
        if mask is None or not torch.any(mask > 0):
            raise ValueError("부분 수정할 영역을 먼저 칠하거나 '전체 수정 실행'을 선택하세요.")
        return (image, mask, "edited")


class MingFlowQwenEditDecision:
    """Pause for review, run Qwen edit, or lazily pass the original through."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "decision": (
                    ["결정 대기 · 정지", "수정 실행", "수정 없이 진행"],
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "MINGFLOW_EDIT_ROUTE")
    RETURN_NAMES = ("image_for_edit", "route")
    FUNCTION = "decide"
    CATEGORY = "🌊 MingFlow/Qwen Local"

    def decide(self, image, decision):
        if decision == "결정 대기 · 정지":
            blocker = ExecutionBlocker(None)
            return (blocker, "blocked")
        if decision == "수정 없이 진행":
            return (ExecutionBlocker(None), "original")
        return (image, "edited")


class MingFlowEditResultRouter:
    """Lazily choose the original image or the selected editor's result."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"route": ("MINGFLOW_EDIT_ROUTE",)},
            "optional": {
                "original_image": ("IMAGE", {"lazy": True}),
                "edited_image": ("IMAGE", {"lazy": True}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("selected_image",)
    FUNCTION = "select"
    CATEGORY = "🌊 MingFlow/Image Edit"

    @classmethod
    def check_lazy_status(cls, route, original_image=None, edited_image=None):
        if route == "original" and original_image is None:
            return ["original_image"]
        if route == "edited" and edited_image is None:
            return ["edited_image"]

    def select(self, route, original_image=None, edited_image=None):
        if route == "original":
            if original_image is None:
                raise ValueError("원본 이미지 경로가 연결되지 않았습니다.")
            return (original_image,)
        if route == "edited":
            if edited_image is None:
                raise ValueError("수정 이미지 경로가 연결되지 않았습니다.")
            return (edited_image,)
        return (ExecutionBlocker(None),)


class GPTImageDisplay:
    """Display an IMAGE batch inside ComfyUI without permanent output files."""

    def __init__(self):
        # ComfyUI's built-in preview implementation handles temp files and UI payloads.
        from nodes import PreviewImage

        self._preview = PreviewImage()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"images": ("IMAGE",)},
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "display"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/OpenAI"

    def display(self, images, prompt=None, extra_pnginfo=None):
        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(10, 100)
        response = self._preview.save_images(
            images,
            filename_prefix="ART_AI_preview",
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
        )
        progress.update_absolute(100, 100)
        response["result"] = (images,)
        return response


class QwenImagePreviewDownload:
    """Preview local Qwen output, provide download UI, and pass IMAGE onward."""

    def __init__(self):
        from nodes import SaveImage

        self._preview = SaveImage()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"images": ("IMAGE",)},
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "display"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/Qwen Local"

    def display(self, images, prompt=None, extra_pnginfo=None):
        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(10, 100)
        response = self._preview.save_images(
            images,
            filename_prefix="MingFlow/qwen/qwen_image",
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
        )
        progress.update_absolute(100, 100)
        response["result"] = (images,)
        return response


class TripoImageTo3DSmartLowPoly:
    """Upload one ComfyUI image, generate a Smart LowPoly model, and download GLB."""

    API_ROOT = "https://api.tripo3d.ai/v2/openapi"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("TRIPO_API_KEY",),
                "image": ("IMAGE",),
                "face_limit": ("INT", {"default": 10000, "min": 1000, "max": 20000, "step": 500}),
                "texture_quality": (["standard", "detailed"],),
                "timeout_minutes": ("INT", {"default": 20, "min": 2, "max": 60, "step": 1}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("glb_path", "task_id")
    FUNCTION = "generate"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/Tripo"

    @staticmethod
    def _json(response, operation):
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"Tripo {operation} returned invalid JSON.") from exc
        if response.status_code != 200 or payload.get("code") != 0:
            message = payload.get("message") or response.text[:300]
            raise RuntimeError(f"Tripo {operation} failed: {message}")
        return payload.get("data", {})

    def generate(self, api_key, image, face_limit, texture_quality, timeout_minutes):
        if not api_key or not api_key.get("configured"):
            raise RuntimeError("Tripo API Key 노드에서 키 확인을 먼저 완료하세요.")
        key = _RUNTIME_TRIPO_API_KEY or os.getenv("TRIPO_API_KEY")
        if not key:
            raise RuntimeError("Validated Tripo API key is unavailable.")
        if image.shape[0] != 1:
            raise ValueError("Tripo Image to 3D accepts exactly one IMAGE.")

        import requests

        headers = {"Authorization": f"Bearer {key}"}
        image_bytes = _tensor_to_png(image[0], "tripo_input.png").getvalue()
        upload = requests.post(
            f"{self.API_ROOT}/upload/sts",
            headers=headers,
            files={"file": ("tripo_input.png", image_bytes, "image/png")},
            timeout=60,
        )
        upload_data = self._json(upload, "image upload")
        image_token = upload_data.get("image_token")
        if not image_token:
            raise RuntimeError("Tripo upload response did not include image_token.")

        task_payload = {
            "type": "image_to_model",
            "model_version": "v3.1-20260211",
            "file": {"type": "png", "file_token": image_token},
            "texture": True,
            "pbr": True,
            "texture_quality": texture_quality,
            "texture_alignment": "original_image",
            "export_uv": True,
            "smart_low_poly": True,
            "face_limit": int(face_limit),
            "quad": False,
        }
        task_response = requests.post(
            f"{self.API_ROOT}/task",
            headers={**headers, "Content-Type": "application/json"},
            json=task_payload,
            timeout=60,
        )
        task_id = self._json(task_response, "task creation").get("task_id")
        if not task_id:
            raise RuntimeError("Tripo task response did not include task_id.")

        deadline = time.monotonic() + timeout_minutes * 60
        task_data = None
        while time.monotonic() < deadline:
            poll = requests.get(
                f"{self.API_ROOT}/task/{task_id}", headers=headers, timeout=30
            )
            task_data = self._json(poll, "task polling")
            status = task_data.get("status")
            if status == "success":
                break
            if status in {"failed", "cancelled", "banned", "expired", "unknown"}:
                detail = task_data.get("error_msg") or status
                raise RuntimeError(f"Tripo task {task_id} ended with {detail}.")
            time.sleep(5)
        else:
            raise TimeoutError(f"Tripo task {task_id} exceeded {timeout_minutes} minutes.")

        output = task_data.get("output", {})
        # A successful ART AI result must contain the textured PBR GLB. Do not
        # silently fall back to base_model, which can be geometry-only.
        model_url = output.get("pbr_model")
        if not model_url:
            raise RuntimeError(
                "Tripo 생성은 완료됐지만 텍스처가 포함된 pbr_model을 반환하지 않았습니다. "
                "Tripo 크레딧과 texture_quality 설정을 확인하세요."
            )
        model_response = requests.get(model_url, timeout=120)
        model_response.raise_for_status()

        try:
            import folder_paths

            output_root = Path(folder_paths.get_output_directory())
        except ImportError:
            output_root = Path.cwd() / "output"
        output_dir = output_root / "ART_AI" / "tripo"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"tripo_smart_lowpoly_{task_id}.glb"
        output_path.write_bytes(model_response.content)
        return {
            "ui": {
                "glb_file": [
                    {
                        "filename": output_path.name,
                        "subfolder": "ART_AI/tripo",
                        "type": "output",
                    }
                ]
            },
            "result": (str(output_path.resolve()), task_id),
        }


class TripoPreview3DAnimation:
    """Connectable wrapper around ComfyUI's official animated 3D preview."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # The official viewer updates this widget after execution.
                "glb_path": ("STRING", {"forceInput": True}),
                "model_file": ("STRING", {"default": "", "multiline": False}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "preview"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/Tripo"
    EXPERIMENTAL = True

    @classmethod
    def IS_CHANGED(cls, glb_path, model_file=""):
        # The upstream Tripo node creates a new file while the graph wiring stays
        # unchanged. Never reuse an old Preview3D UI payload for a new GLB.
        return float("nan")

    def preview(self, glb_path, model_file=""):
        raw_path = str(glb_path or model_file).strip()
        if not raw_path:
            raise ValueError("Tripo GLB 경로를 glb_path 입력에 연결하세요.")
        path = Path(raw_path).expanduser().resolve()
        if Path(path).suffix.lower() not in {".glb", ".gltf", ".fbx", ".obj", ".stl"}:
            raise ValueError(f"지원하지 않는 3D 파일 형식입니다: {Path(path).suffix}")
        if not path.is_file():
            raise FileNotFoundError(f"Tripo 3D 파일을 찾을 수 없습니다: {path}")

        import folder_paths

        output_root = Path(folder_paths.get_output_directory()).resolve()
        try:
            output_relative = path.relative_to(output_root)
        except ValueError as exc:
            raise ValueError("Tripo GLB가 ComfyUI output 폴더 밖에 있습니다.") from exc

        relative_path = output_relative.as_posix()
        file_info = {
            "filename": path.name,
            "subfolder": output_relative.parent.as_posix()
            if output_relative.parent != Path(".")
            else "",
            "type": "output",
        }
        return {
            "ui": {
                "result": [relative_path, None, None],
                "model_file": [relative_path],
                "glb_file": [file_info],
            },
            "result": (),
        }


class Trellis2PreviewGLBDownload:
    """Preview a GLB exported by ComfyUI-Trellis2 and expose it for download."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "glb_path": ("STRING", {"forceInput": True}),
            },
            "optional": {
                "relative_path": ("STRING", {"forceInput": True}),
                "model_file": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "preview"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/TRELLIS2"
    EXPERIMENTAL = True

    def preview(self, glb_path, relative_path="", model_file=""):
        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(10, 100)
        raw_path = str(glb_path or model_file).strip()
        if not raw_path:
            raise ValueError("TRELLIS2 Export Mesh의 glb_path 출력을 연결하세요.")
        path = Path(raw_path).expanduser().resolve()
        if path.suffix.lower() != ".glb":
            raise ValueError(f"TRELLIS2 미리보기는 GLB만 지원합니다: {path.suffix}")
        if not path.is_file():
            raise FileNotFoundError(f"TRELLIS2 GLB 파일을 찾을 수 없습니다: {path}")

        import folder_paths

        output_root = Path(folder_paths.get_output_directory()).resolve()
        try:
            output_relative = path.relative_to(output_root)
        except ValueError as exc:
            raise ValueError(
                "GLB가 ComfyUI output 폴더 밖에 있습니다. "
                "Trellis2 Export Mesh 노드로 먼저 저장하세요."
            ) from exc

        provided_relative = str(relative_path or "").strip().replace("\\", "/")
        actual_relative = output_relative.as_posix()
        if provided_relative and Path(provided_relative).name != path.name:
            raise ValueError("relative_path와 glb_path가 서로 다른 파일을 가리킵니다.")

        file_info = {
            "filename": path.name,
            "subfolder": output_relative.parent.as_posix()
            if output_relative.parent != Path(".")
            else "",
            "type": "output",
        }
        progress.update_absolute(100, 100)
        return {
            "ui": {
                # Current Preview3D expects result=[model, camera, bg]. Older
                # frontends read model_file, so publish both payload shapes.
                "result": [actual_relative, None, None],
                "model_file": [actual_relative],
                "glb_file": [file_info],
                "relative_path": [actual_relative],
            },
            "result": (),
        }


class Trellis2ImageToGLBLocal:
    """Run Microsoft's TRELLIS.2 locally and export a textured PBR GLB."""

    MAX_SEED = 2_147_483_647
    _pipeline = None
    _pipeline_key = None

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "resolution": (["512", "1024", "1536"], {"default": "1024"}),
                "seed": (
                    "INT",
                    {"default": 0, "min": 0, "max": cls.MAX_SEED, "step": 1},
                ),
                "randomize_seed": ("BOOLEAN", {"default": True}),
                "decimation_target": (
                    "INT",
                    {"default": 500000, "min": 100000, "max": 1000000, "step": 10000},
                ),
                "texture_size": (["1024", "2048", "4096"], {"default": "2048"}),
                "stage1_guidance_strength": (
                    "FLOAT",
                    {"default": 7.5, "min": 1.0, "max": 10.0, "step": 0.1},
                ),
                "stage1_guidance_rescale": (
                    "FLOAT",
                    {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
                "stage1_sampling_steps": (
                    "INT",
                    {"default": 12, "min": 1, "max": 50, "step": 1},
                ),
                "stage1_rescale_t": (
                    "FLOAT",
                    {"default": 5.0, "min": 1.0, "max": 6.0, "step": 0.1},
                ),
                "stage2_guidance_strength": (
                    "FLOAT",
                    {"default": 7.5, "min": 1.0, "max": 10.0, "step": 0.1},
                ),
                "stage2_guidance_rescale": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
                "stage2_sampling_steps": (
                    "INT",
                    {"default": 12, "min": 1, "max": 50, "step": 1},
                ),
                "stage2_rescale_t": (
                    "FLOAT",
                    {"default": 3.0, "min": 1.0, "max": 6.0, "step": 0.1},
                ),
                "stage3_guidance_strength": (
                    "FLOAT",
                    {"default": 1.0, "min": 1.0, "max": 10.0, "step": 0.1},
                ),
                "stage3_guidance_rescale": (
                    "FLOAT",
                    {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
                "stage3_sampling_steps": (
                    "INT",
                    {"default": 12, "min": 1, "max": 50, "step": 1},
                ),
                "stage3_rescale_t": (
                    "FLOAT",
                    {"default": 3.0, "min": 1.0, "max": 6.0, "step": 0.1},
                ),
            },
            "optional": {
                "model_directory": (
                    "STRING",
                    {
                        "default": "/workspace/models/Trellis2",
                        "multiline": False,
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT")
    RETURN_NAMES = ("glb_path", "relative_path", "used_seed")
    FUNCTION = "generate"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/TRELLIS2"

    @classmethod
    def _get_pipeline(cls, model_directory):
        raw_directory = str(model_directory or "").strip()
        if not raw_directory:
            raise ValueError("TRELLIS2 모델 폴더 경로를 입력하세요.")
        model_path = Path(raw_directory).expanduser().resolve()
        if not model_path.is_dir():
            raise FileNotFoundError(
                f"TRELLIS2 모델 폴더를 찾을 수 없습니다: {model_path}"
            )

        pipeline_config = model_path / "pipeline.json"
        if not pipeline_config.is_file():
            raise FileNotFoundError(
                f"TRELLIS2 pipeline.json을 찾을 수 없습니다: {pipeline_config}"
            )
        try:
            pipeline_data = json.loads(pipeline_config.read_text(encoding="utf-8"))
            model_entries = pipeline_data["args"]["models"].values()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"잘못된 TRELLIS2 pipeline.json: {pipeline_config}") from exc

        missing_files = []
        lfs_pointer_files = []
        for entry in model_entries:
            checkpoint = model_path / str(entry)
            for suffix in (".json", ".safetensors"):
                required_file = Path(f"{checkpoint}{suffix}")
                if not required_file.is_file():
                    missing_files.append(required_file.relative_to(model_path).as_posix())
                elif suffix == ".safetensors" and required_file.stat().st_size < 1024:
                    lfs_pointer_files.append(
                        required_file.relative_to(model_path).as_posix()
                    )
        if missing_files or lfs_pointer_files:
            details = []
            if missing_files:
                details.append("누락: " + ", ".join(missing_files[:12]))
            if lfs_pointer_files:
                details.append("Git LFS 포인터: " + ", ".join(lfs_pointer_files[:12]))
            raise FileNotFoundError(
                "TRELLIS2 로컬 모델이 완전히 다운로드되지 않았습니다. "
                + " | ".join(details)
            )

        pipeline_key = str(model_path)
        if cls._pipeline is None or cls._pipeline_key != pipeline_key:
            try:
                from trellis2.pipelines import Trellis2ImageTo3DPipeline
            except ImportError as exc:
                raise RuntimeError(
                    "TRELLIS.2가 설치되지 않았습니다. RunPod ComfyUI 환경에 "
                    "Microsoft TRELLIS.2 또는 ComfyUI-Trellis2 의존성을 설치하세요."
                ) from exc
            cls._pipeline = Trellis2ImageTo3DPipeline.from_pretrained(pipeline_key)
            cls._pipeline.cuda()
            cls._pipeline_key = pipeline_key
        return cls._pipeline

    @staticmethod
    def _to_pil(image):
        if image.shape[0] != 1:
            raise ValueError("TRELLIS2 Image to GLB는 IMAGE 한 장만 받습니다.")
        array = (
            image[0].detach().cpu().clamp(0, 1).mul(255).round().byte().numpy()
        )
        mode = "RGBA" if array.shape[-1] == 4 else "RGB"
        return Image.fromarray(array, mode=mode)

    def generate(
        self,
        image,
        resolution,
        seed,
        randomize_seed,
        decimation_target,
        texture_size,
        stage1_guidance_strength,
        stage1_guidance_rescale,
        stage1_sampling_steps,
        stage1_rescale_t,
        stage2_guidance_strength,
        stage2_guidance_rescale,
        stage2_sampling_steps,
        stage2_rescale_t,
        stage3_guidance_strength,
        stage3_guidance_rescale,
        stage3_sampling_steps,
        stage3_rescale_t,
        model_directory="/workspace/models/Trellis2",
    ):
        try:
            import comfy.utils
            import folder_paths
            import o_voxel
        except ImportError as exc:
            raise RuntimeError(
                "TRELLIS.2의 o_voxel 의존성이 설치되지 않았습니다."
            ) from exc

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(2, 100)
        used_seed = (
            int.from_bytes(os.urandom(4), "little") % (self.MAX_SEED + 1)
            if randomize_seed
            else int(seed)
        )
        pipeline = self._get_pipeline(model_directory)
        progress.update_absolute(12, 100)
        input_image = pipeline.preprocess_image(self._to_pil(image))
        progress.update_absolute(18, 100)
        pipeline_type = {
            "512": "512",
            "1024": "1024_cascade",
            "1536": "1536_cascade",
        }[str(resolution)]

        outputs, latents = pipeline.run(
            input_image,
            seed=used_seed,
            preprocess_image=False,
            sparse_structure_sampler_params={
                "steps": int(stage1_sampling_steps),
                "guidance_strength": float(stage1_guidance_strength),
                "guidance_rescale": float(stage1_guidance_rescale),
                "rescale_t": float(stage1_rescale_t),
            },
            shape_slat_sampler_params={
                "steps": int(stage2_sampling_steps),
                "guidance_strength": float(stage2_guidance_strength),
                "guidance_rescale": float(stage2_guidance_rescale),
                "rescale_t": float(stage2_rescale_t),
            },
            tex_slat_sampler_params={
                "steps": int(stage3_sampling_steps),
                "guidance_strength": float(stage3_guidance_strength),
                "guidance_rescale": float(stage3_guidance_rescale),
                "rescale_t": float(stage3_rescale_t),
            },
            pipeline_type=pipeline_type,
            return_latent=True,
        )
        progress.update_absolute(78, 100)
        # Match the official Gradio demo's Extract GLB path exactly. The demo
        # preserves the generated latents and decodes them again for export,
        # instead of exporting the preview mesh returned by pipeline.run().
        shape_slat, tex_slat, latent_resolution = latents
        mesh = pipeline.decode_latent(
            shape_slat, tex_slat, latent_resolution
        )[0]
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices,
            faces=mesh.faces,
            attr_volume=mesh.attrs,
            coords=mesh.coords,
            attr_layout=pipeline.pbr_attr_layout,
            grid_size=latent_resolution,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=int(decimation_target),
            texture_size=int(texture_size),
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            use_tqdm=True,
        )
        progress.update_absolute(94, 100)

        output_root = Path(folder_paths.get_output_directory())
        output_dir = output_root / "ART_AI" / "trellis2"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"trellis2_{stamp}_{used_seed}.glb"
        glb.export(str(output_path), extension_webp=True)
        progress.update_absolute(100, 100)
        relative_path = output_path.relative_to(output_root).as_posix()
        torch.cuda.empty_cache()

        return {
            "ui": {
                "glb_file": [
                    {
                        "filename": output_path.name,
                        "subfolder": "ART_AI/trellis2",
                        "type": "output",
                    }
                ]
            },
            "result": (str(output_path.resolve()), relative_path, used_seed),
        }


class Trellis2ImageToGLBLocalV2(Trellis2ImageToGLBLocal):
    """Stable schema ID that avoids legacy TRELLIS2 widget-value shifting."""


class Trellis2PrepareImageRemoveBG:
    """Use TRELLIS.2's own background model and return an RGBA IMAGE."""

    def __init__(self):
        from nodes import SaveImage

        self._preview = SaveImage()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "model_directory": (
                    "STRING",
                    {
                        "default": "/workspace/models/Trellis2",
                        "multiline": False,
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("rgba_image",)
    FUNCTION = "remove_background"
    OUTPUT_NODE = True
    CATEGORY = "🌊 MingFlow/TRELLIS2"

    def remove_background(
        self, image, model_directory, prompt=None, extra_pnginfo=None
    ):
        import comfy.utils

        progress = comfy.utils.ProgressBar(100)
        progress.update_absolute(5, 100)
        pipeline = Trellis2ImageToGLBLocal._get_pipeline(model_directory)
        progress.update_absolute(25, 100)
        input_image = Trellis2ImageToGLBLocal._to_pil(image)

        has_alpha = False
        if input_image.mode == "RGBA":
            alpha = np.asarray(input_image)[:, :, 3]
            has_alpha = not np.all(alpha == 255)

        if has_alpha:
            output = input_image
        else:
            if pipeline.rembg_model is None:
                raise RuntimeError("TRELLIS2 파이프라인에 배경 제거 모델이 없습니다.")
            if pipeline.low_vram:
                pipeline.rembg_model.to(pipeline.device)
            output = pipeline.rembg_model(input_image.convert("RGB"))
            if pipeline.low_vram:
                pipeline.rembg_model.cpu()

        progress.update_absolute(80, 100)
        output = output.convert("RGBA")
        array = np.asarray(output).astype(np.float32) / 255.0
        rgba_image = torch.from_numpy(array).unsqueeze(0)
        response = self._preview.save_images(
            rgba_image,
            filename_prefix="MingFlow/remove_bg/removed_bg",
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
        )
        progress.update_absolute(100, 100)
        response["result"] = (rgba_image,)
        return response


class TripoExtractBaseColorTexture:
    """Extract the first PBR base-color texture embedded in a GLB as IMAGE."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"glb_path": ("STRING", {"forceInput": True})}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("texture",)
    FUNCTION = "extract"
    CATEGORY = "🌊 MingFlow/Tripo"

    @staticmethod
    def _read_glb(path):
        raw = path.read_bytes()
        if len(raw) < 12 or raw[:4] != b"glTF":
            raise ValueError("유효한 GLB 파일이 아닙니다.")
        version, total_length = struct.unpack_from("<II", raw, 4)
        if version != 2 or total_length > len(raw):
            raise ValueError("지원하지 않거나 손상된 GLB 파일입니다.")

        document = None
        binary = b""
        offset = 12
        while offset + 8 <= total_length:
            chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
            offset += 8
            chunk = raw[offset : offset + chunk_length]
            offset += chunk_length
            if chunk_type == 0x4E4F534A:
                document = json.loads(chunk.rstrip(b"\x00 \t\r\n").decode("utf-8"))
            elif chunk_type == 0x004E4942:
                binary = chunk
        if document is None:
            raise ValueError("GLB에 JSON 장면 정보가 없습니다.")
        return document, binary

    @staticmethod
    def _image_bytes(document, binary, glb_path, image_index):
        image_spec = document.get("images", [])[image_index]
        if "bufferView" in image_spec:
            view = document.get("bufferViews", [])[image_spec["bufferView"]]
            start = int(view.get("byteOffset", 0))
            end = start + int(view["byteLength"])
            return binary[start:end]
        uri = image_spec.get("uri", "")
        if uri.startswith("data:") and "," in uri:
            return base64.b64decode(uri.split(",", 1)[1])
        if uri:
            texture_path = (glb_path.parent / uri).resolve()
            return texture_path.read_bytes()
        raise ValueError("GLB 이미지 데이터의 위치를 찾을 수 없습니다.")

    def extract(self, glb_path):
        path = Path(str(glb_path)).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"GLB 파일을 찾을 수 없습니다: {path}")
        document, binary = self._read_glb(path)

        image_index = None
        for material in document.get("materials", []):
            texture_info = material.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if texture_info is None:
                continue
            texture_index = int(texture_info["index"])
            image_index = int(document["textures"][texture_index]["source"])
            break
        if image_index is None:
            raise ValueError("GLB에서 PBR baseColor 텍스처를 찾지 못했습니다.")

        encoded = self._image_bytes(document, binary, path, image_index)
        with Image.open(io.BytesIO(encoded)) as image:
            rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        return (torch.from_numpy(rgb).unsqueeze(0),)


NODE_CLASS_MAPPINGS = {
    "ARTAI_OpenAIAPIKey": OpenAIAPIKey,
    "ARTAI_TripoAPIKey": TripoAPIKey,
    "ARTAI_GPTPromptInput": GPTPromptInput,
    "ARTAI_QwenPromptInput": QwenPromptInput,
    "ARTAI_QwenImageGenerateLocal": QwenImageGenerateLocal,
    "ARTAI_QwenImageGenerateLocalV2": QwenImageGenerateLocalV2,
    "ARTAI_QwenImageGenerateLocalV3": QwenImageGenerateLocalV3,
    "ARTAI_QwenImageGenerateDiffusersBF16V4": QwenImageGenerateDiffusersBF16V4,
    "ARTAI_QwenImageEditDiffusersBF16": QwenImageEditDiffusersBF16,
    "ARTAI_GPTPromptGenerator": GPTPromptGenerator,
    "ARTAI_GPTImage2Generate": GPTImage2Generate,
    "ARTAI_GPTImage2Edit": GPTImage2Edit,
    "ARTAI_GPTImagePartialEdit": GPTImagePartialEdit,
    "ARTAI_MingFlowImageCheckpoint": MingFlowImageCheckpoint,
    "ARTAI_MingFlowRegionSelector": MingFlowRegionSelector,
    "ARTAI_MingFlowEditApprovalGate": MingFlowEditApprovalGate,
    "ARTAI_MingFlowQwenEditDecision": MingFlowQwenEditDecision,
    "ARTAI_MingFlowEditResultRouter": MingFlowEditResultRouter,
    "ARTAI_GPTImageDisplay": GPTImageDisplay,
    "ARTAI_QwenImagePreviewDownload": QwenImagePreviewDownload,
    "ARTAI_TripoImageTo3DSmartLowPoly": TripoImageTo3DSmartLowPoly,
    "ARTAI_TripoPreview3DAnimation": TripoPreview3DAnimation,
    "ARTAI_Trellis2PreviewGLBDownload": Trellis2PreviewGLBDownload,
    "ARTAI_Trellis2ImageToGLBLocal": Trellis2ImageToGLBLocal,
    "ARTAI_Trellis2ImageToGLBLocalV2": Trellis2ImageToGLBLocalV2,
    "ARTAI_Trellis2PrepareImageRemoveBG": Trellis2PrepareImageRemoveBG,
    "ARTAI_TripoExtractBaseColorTexture": TripoExtractBaseColorTexture,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ARTAI_OpenAIAPIKey": "OpenAI API Key Check",
    "ARTAI_TripoAPIKey": "Tripo API Key Check",
    "ARTAI_GPTPromptInput": "GPT Prompt Input",
    "ARTAI_QwenPromptInput": "✍️ Qwen Prompt Input · Positive & Negative",
    "ARTAI_QwenImageGenerateLocal": "⚠️ Qwen Image Generate · Legacy (delete)",
    "ARTAI_QwenImageGenerateLocalV2": "⚠️ Qwen Image Generate · V2 (delete)",
    "ARTAI_QwenImageGenerateLocalV3": "⚠️ Qwen Image Generate · V3 (delete)",
    "ARTAI_QwenImageGenerateDiffusersBF16V4": "⚡ Qwen Image Generate · Diffusers BF16 V4",
    "ARTAI_QwenImageEditDiffusersBF16": "🖌️ Qwen Image Edit 2511 · Diffusers BF16",
    "ARTAI_GPTPromptGenerator": "GPT Prompt Generator",
    "ARTAI_GPTImage2Generate": "GPT Image 2 Generate",
    "ARTAI_GPTImage2Edit": "GPT Image 2 Edit",
    "ARTAI_GPTImagePartialEdit": "🖌️ GPT 이미지 부분 수정",
    "ARTAI_MingFlowImageCheckpoint": "🔒 MingFlow 생성 이미지 고정",
    "ARTAI_MingFlowRegionSelector": "🟩 MingFlow 수정 영역 선택",
    "ARTAI_MingFlowEditApprovalGate": "🖌️ GPT 부분 수정 여부 결정",
    "ARTAI_MingFlowQwenEditDecision": "❓ Qwen 수정 여부 결정",
    "ARTAI_MingFlowEditResultRouter": "🔀 MingFlow 수정 결과 선택",
    "ARTAI_GPTImageDisplay": "GPT Image Display",
    "ARTAI_QwenImagePreviewDownload": "🖼️ Qwen Image Preview & Download",
    "ARTAI_TripoImageTo3DSmartLowPoly": "Tripo Image to 3D · Smart LowPoly",
    "ARTAI_TripoPreview3DAnimation": "Tripo 3D Preview · Animation",
    "ARTAI_Trellis2PreviewGLBDownload": "🧊 TRELLIS2 GLB Preview & Download",
    "ARTAI_Trellis2ImageToGLBLocal": "⚠️ TRELLIS2 Image to GLB · Legacy (delete)",
    "ARTAI_Trellis2ImageToGLBLocalV2": "⚙️ TRELLIS2 Image to GLB · Local V2",
    "ARTAI_Trellis2PrepareImageRemoveBG": "✂️ TRELLIS2 Prepare Image · Remove BG",
    "ARTAI_TripoExtractBaseColorTexture": "Tripo Extract Base Color Texture",
}
