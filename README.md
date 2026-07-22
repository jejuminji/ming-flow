# ComfyUI용 MingFlow 이미지·3D 생성 노드

`OPENAI_API_KEY`를 사용해 OpenAI API를 직접 호출하고, 로컬 이미지 생성 및 이미지 기반 3D 변환까지 연결하는 ComfyUI 커스텀 노드입니다.

OpenAI Partner Nodes나 Comfy 크레딧을 사용하지 않습니다.

## 제공 노드

- **OpenAI API Key Check**: 입력한 OpenAI API 키의 유효성을 확인하고 ComfyUI 서버 메모리에만 보관합니다.
- **GPT Prompt Input**: API 호출 없이 다국어 요청을 작성할 수 있는 여러 줄 입력란을 제공합니다.
- **GPT Prompt Generator**: 다국어 게임 아트 요청을 영어 이미지 생성 프롬프트로 변환합니다.
- **GPT Image 2 Generate**: 생성된 이미지를 ComfyUI `IMAGE` 배치로 반환합니다.
- **GPT Image 2 Edit**: 입력 배치의 모든 이미지를 수정하며 선택적으로 ComfyUI `MASK`를 받습니다.
- **GPT 이미지 부분 수정**: GPT로 생성된 이미지를 다시 GPT Image API에 보내 수정합니다. `MASK`를 연결하지 않으면 이미지 전체를, 연결하면 흰색 영역만 수정하며 결과 한 장을 `edited_image`로 반환합니다.
- **MingFlow 생성 이미지 고정**: 첫 실행에서 GPT 생성 이미지를 체크포인트 PNG로 저장합니다. 이후 `저장 이미지 재사용` 상태에서는 이미지 입력을 지연 평가하여 상류 GPT 생성 노드를 실행하지 않고 저장된 이미지를 불러옵니다.
- **MingFlow 수정 영역 선택**: 입력 이미지를 노드 안에 표시하고 브러시로 칠한 영역을 표준 ComfyUI `MASK`로 반환합니다. 초록색 영역이 GPT의 수정 대상입니다.
- **GPT 부분 수정 여부 결정**: GPT 편집 전용 게이트입니다. `마스킹 중 · 정지`에서 영역을 준비하고, `수정 실행`은 마스크 부분 수정, `전체 수정 실행`은 마스크 없는 전체 이미지 수정, `수정 없이 진행`은 GPT 편집을 건너뜁니다. Qwen 플로우에서는 이 노드를 사용하지 않습니다.
- **Qwen 수정 여부 결정**: Qwen-Image-Edit-2511 전용 단순 분기입니다. `수정 실행`은 Qwen 전체 편집을 실행하고, `수정 없이 진행`은 Qwen을 로드하지 않고 원본 경로를 선택합니다.
- **MingFlow 수정 결과 선택**: 결정 노드의 경로 신호에 따라 원본 또는 수정본 하나만 지연 평가합니다. `수정 없이 진행`에서는 연결된 GPT/Qwen 편집 노드를 자동으로 건너뜁니다.
- **GPT Image Display**: 이미지 배치를 노드 안에 표시하고 브라우저 다운로드 버튼을 제공하며 `IMAGE`를 다음 노드로 전달합니다.
- **Qwen Prompt Input**: 로컬 Qwen-Image에 전달할 positive 프롬프트와 negative 프롬프트를 함께 입력하고 각각의 소켓으로 출력합니다.
- **Qwen Image Preview & Download**: 로컬 Qwen 출력 이미지를 미리 보고 PNG로 내려받으며 `IMAGE`를 다음 노드로 전달합니다.
- **Qwen Image Edit 2511 · Diffusers BF16**: 공식 Diffusers 형식의 로컬 Qwen-Image-Edit-2511 모델과 Plus 파이프라인으로 전체 이미지를 수정합니다. 2511은 마스크 입력을 지원하지 않습니다.
- **Tripo API Key Check**: Tripo 개발자 API 키를 확인하고 API 크레딧 잔액을 표시합니다.
- **Tripo Image to 3D · Smart LowPoly**: 이미지 한 장을 업로드하고 Tripo v3.1로 텍스처가 포함된 PBR Smart LowPoly 메시를 생성합니다. UV와 원본 이미지 정렬 텍스처를 생성하고 PBR GLB 다운로드 버튼을 제공합니다.
- **Tripo 3D Preview · Animation**: `glb_path`를 받아 ComfyUI 공식 애니메이션 3D 뷰어에 표시하고, 현재 GLB를 브라우저로 내려받는 버튼을 제공합니다.
- **Tripo Extract Base Color Texture**: GLB에 포함된 PBR 베이스 컬러 텍스처를 읽어 미리보기 또는 저장용 ComfyUI `IMAGE`로 반환합니다.

## 설치

이 디렉터리를 `ComfyUI/custom_nodes/` 안에 복사한 다음 ComfyUI에서 사용하는 Python 인터프리터로 의존성을 설치합니다.

```powershell
python -m pip install -r requirements.txt
```

ComfyUI를 실행하는 환경에 API 키를 설정합니다.

```powershell
$env:OPENAI_API_KEY = "your-api-key"
python main.py
```

Windows에서 API 키를 계속 사용하려면 ComfyUI를 실행하는 서비스, 런처 또는 사용자 환경변수에 `OPENAI_API_KEY`를 설정한 후 ComfyUI를 다시 시작합니다. API 키를 워크플로우 파일에 직접 저장하지 마세요.

또는 워크플로우의 첫 번째 노드로 **OpenAI API Key Check**를 사용할 수 있습니다. 키를 입력하고 **API 키 유효성 확인**을 누르세요. 입력한 키는 워크플로우 JSON에 저장되지 않으며 확인에 성공하면 입력란에서 지워집니다. 키는 서버 메모리에만 보관되므로 ComfyUI를 다시 시작한 후에는 유효성 확인을 다시 실행해야 합니다.

## 사용 시 참고사항

- 이미지 모델은 `gpt-image-2`, 프롬프트 모델은 `gpt-5-mini`로 고정되어 있습니다.
- **GPT Image 2 Generate**는 공식 표준 크기인 `1024x1024`, `1536x1024`, `1024x1536`만 제공합니다.
- **GPT Image 2 Generate**는 실행할 때마다 이미지 한 장을 생성합니다.
- API로 생성된 이미지는 `[B,H,W,C]` 형태의 RGB 실수 텐서로 변환되므로 **Save Image**와 표준 후처리 노드에 바로 연결할 수 있습니다.
- `gpt-image-2`는 현재 투명 배경 생성을 허용하지 않습니다. `auto` 또는 `opaque`를 선택한 후 ComfyUI 배경 제거 노드를 사용하세요.
- ComfyUI 마스크에서는 흰색 영역이 선택 및 수정 대상입니다. 편집 노드는 이 규칙을 Images API가 요구하는 알파 마스크 규칙으로 변환합니다.
- GPT Image API를 사용하려면 OpenAI 조직 인증이 필요할 수 있습니다.

### 로컬 Qwen 프롬프트 연결

`Qwen Prompt Input`에서 positive와 negative 프롬프트를 함께 작성합니다. `prompt` 출력은 Qwen 생성 노드의 `prompt`에, `negative_prompt` 출력은 `negative_prompt`에 연결합니다. 생성 노드 내부에는 negative 프롬프트 입력란이 별도로 표시되지 않습니다.

### 로컬 Qwen 이미지 편집

`Qwen Image Edit 2511 · Diffusers BF16`의 `model_directory`에는 공식 Diffusers 형식 `Qwen-Image-Edit-2511` 폴더를 지정합니다. 입력 `IMAGE`와 수정 지시를 연결하면 공식 `QwenImageEditPlusPipeline`으로 전체 편집을 수행합니다. 기본값은 공식 예제에 맞춘 40 steps, CFG 4.0, guidance 1.0입니다. 2511 공식 Plus 파이프라인에는 마스크 입력이 없으므로 `MASK` 부분 수정은 지원하지 않습니다. 생성용 `Qwen-Image-2512` 체크포인트와 편집용 모델은 서로 다른 모델이므로 편집 모델을 별도로 준비해야 합니다.

## 바로 사용할 수 있는 워크플로우

- `workflows/api_generate.json`: 다국어 요청 → 프롬프트 생성 → 이미지 생성 → 이미지 저장
- `workflows/api_edit.json`: 이미지 불러오기 → 이미지 수정 → 이미지 저장

워크플로우를 불러오고 사용하는 방법은 [`workflows/README.md`](./workflows/README.md)를 참고하세요. 생성과 편집 워크플로우는 의도하지 않은 API 중복 호출과 비용 발생을 방지하기 위해 별도 파일로 제공됩니다.

### GPT 생성 이미지 수정 연결

```text
GPT Image 2 Generate
  ↓ 최초 한 번
MingFlow 생성 이미지 고정
  ├─ fixed_image ───────────────────────┐
  └→ MingFlow 수정 영역 선택 → MASK ───┤
                                       ↓
                              MingFlow 수정 여부 결정
                              [마스킹 중 · 정지]
                                       ↓
                              GPT 이미지 부분 수정
                                       ↓
                              Tripo 또는 TRELLIS
```

1. **MingFlow 생성 이미지 고정**을 `새 이미지 저장`으로 두고 최초 한 번 실행합니다. GPT 생성 결과가 `ComfyUI/output/MingFlow/checkpoints/`에 저장됩니다.
2. 저장이 끝나면 즉시 `저장 이미지 재사용`으로 변경합니다. 이후 실행에서는 상류 GPT 생성 노드를 평가하지 않습니다.
3. **MingFlow 수정 여부 결정**을 `마스킹 중 · 정지`로 두고 영역 선택 노드에서 수정 영역을 초록색으로 칠합니다. 편집과 3D 생성은 차단됩니다.
4. `edit_prompt`를 작성하고 **MingFlow 수정 여부 결정**을 `수정 실행`으로 변경해 다시 실행합니다. 마스크 없이 전체를 수정하려면 `전체 수정 실행`을 선택합니다.
5. 수정 결과가 선택되면 연결된 Tripo 또는 TRELLIS가 바로 실행됩니다.

### 이미지 수정 없이 3D로 진행

이미지 수정이 필요하지 않으면 다음과 같이 설정합니다.

1. **MingFlow 생성 이미지 고정**을 `저장 이미지 재사용`으로 설정합니다.
2. **MingFlow 수정 여부 결정**을 `수정 없이 진행`으로 변경합니다.
3. Queue를 실행하면 원본 이미지가 Tripo 또는 TRELLIS로 바로 전달됩니다.

`MingFlow 수정 결과 선택`이 원본 경로만 평가하므로 GPT 이미지 부분 수정 노드를 직접 Bypass할 필요가 없으며 GPT Image Edit API도 호출되지 않습니다.

## 목표 제작 플로우

MingFlow는 로컬과 API를 조합해 이미지 생성부터 부분수정, 최종 3D 에셋 변환까지 연결하는 제작 플로우를 지향합니다.

> 로컬 Qwen 또는 GPT API로 이미지를 만들고, 필요한 부분만 로컬 편집 모델이나 GPT API로 다시 수정한 뒤, 사용자가 확정한 이미지만 Tripo 또는 로컬 TRELLIS를 통해 3D 에셋으로 변환합니다.

자세한 제품 방향과 부분수정 노드 요구사항은 [`플로우_차별점.md`](./플로우_차별점.md)를 참고하세요.
