# API workflows

ComfyUI에서 상단 메뉴의 **Workflow > Open**으로 JSON 파일을 불러옵니다.

두 워크플로우의 첫 노드에서 API 키를 입력하고 **API 키 유효성 확인**을
누릅니다. 성공한 키는 워크플로우 파일이 아니라 ComfyUI 서버 메모리에만
보관되며, ComfyUI를 재시작하면 다시 확인해야 합니다.
첫 노드의 `api_key` 출력은 실제 API를 호출하는 GPT Prompt Generator와
GPT Image 2 Generate에 연결됩니다. 편집 워크플로우에서는 GPT Image 2 Edit에
연결됩니다. 텍스트만 전달하는 GPT Prompt Input에는 API 키가 필요하지 않습니다.

## `api_generate.json`

```text
한국어/다국어 아트 브리프
  -> GPT Prompt Input
  -> GPT Prompt Generator
  -> GPT Image 2 Generate
       ├-> GPT Image Display
       │    -> Tripo Image to 3D · Smart LowPoly -> Preview 3D - Animation
       └-> Save Image
```

GPT Prompt Input의 큰 입력창 하나에 원하는 내용을 자유롭게 작성한 후 실행합니다. 기본값은
`1024x1024`, `medium`, `opaque`이며 생성 수는 항상 1장으로 고정됩니다.

## `api_edit.json`

```text
Load Image
  -> GPT Image 2 Edit
  -> Save Image
```

Load Image에서 원본을 선택하고 수정 프롬프트를 작성한 후 실행합니다. 부분 수정이
필요하면 별도의 마스크 생성/편집 노드의 `MASK` 출력을 Edit 노드의 선택적 `mask`
입력에 연결합니다. ComfyUI의 흰색 마스크 영역이 수정 대상입니다.

생성과 편집을 별도 파일로 둔 이유는 ComfyUI가 한 그래프의 모든 Save Image 출력을
실행할 때 의도하지 않은 API 호출과 비용이 발생하는 것을 방지하기 위해서입니다.

## 실행 전 확인

1. 이 커스텀 노드의 `requirements.txt`를 설치합니다.
2. ComfyUI를 실행하는 프로세스에 `OPENAI_API_KEY`를 설정합니다.
3. ComfyUI를 재시작하고 JSON 워크플로우를 엽니다.
4. 첫 테스트는 `medium` 또는 `low`, 이미지 1장으로 실행합니다.
