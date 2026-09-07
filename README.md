# 교과서 PDF 구조화 파서

원고 및 기존 교과서 PDF에서 텍스트, 이미지, 표, 악보 영역과 페이지 좌표를
편집·검수용 JSON으로 추출합니다. 판독이 불확실한 내용은 추측하지 않고
`review_required: true`로 남깁니다.

## 설치와 실행

```powershell
python -m pip install -r requirements.txt
python test.py parse "원고_0130 대취타와 취타.pdf" --output output/pdf
python test.py parse "." --recursive --output output/pdf
python test.py review-html output/pdf
python test.py audit "원고_0130 대취타와 취타.pdf" --curriculum "22교육개정_음감비_교육과정.pdf" --textbooks "★타사 교과서"
python test.py serve-audit output/audit --port 8765
python test.py team-serve --port 8780
```

스캔 PDF의 한국어 OCR이 필요하면 PaddleOCR와 현재 플랫폼용 PaddlePaddle을
추가 설치합니다. 설치하지 않아도 텍스트층이 있는 PDF는 정상 처리되며,
스캔 페이지는 `OCR_UNAVAILABLE` 확인 항목으로 보존됩니다.

PyMuPDF 실행이 Windows 보안 정책에 의해 차단되는 환경에서는 자동으로
pdfplumber와 PDFium 백엔드를 사용합니다.

## 출력

문서마다 다음 파일과 폴더가 생성됩니다.

- `document.json`: 페이지와 모든 구조 요소
- `review.json`: 확인이 필요한 요소만 모은 목록과 통계
- `pages/`: 전체 페이지 렌더링
- `assets/<document_id>/page_XXXX/`: 이미지, 표, 악보 및 OCR 원본 영역
- `review_overlays/`: 확인 대상에 빨간 테두리를 표시한 페이지
- `review.html`: 문서·페이지 이동, 유형 필터, 요소 상세 확인이 가능한 통합 검수 화면

좌표는 PDF 포인트 단위의 `bbox`와 0~1 범위의 `normalized_bbox`를 함께
기록합니다. `document.schema.json`으로 결과 형식을 검증할 수 있습니다.

`audit` 명령은 원고에 성취기준, 학습 목표, 활동이 포함되어 있는지 검사하고
교육과정의 성취기준 후보를 비교해 `audit.json`과 `audit.html`을 생성합니다.
기본 판정은 로컬 유사도 방식이며, `adapter.audit(payload)`를 구현한 모듈을
`--ai-module`로 전달하면 AI 챗봇 판정으로 교체할 수 있습니다.

`audit.html`은 왼쪽 원고 페이지와 오른쪽 같은 페이지의 점검 과정, 기존 교과서
표본에 근거한 A4 레이아웃 추천, 교육과정 비교 근거, 자동 추천 문구와 전체
완성도를 함께 표시합니다.
`serve-audit`은 최신 점검 화면을 `http://127.0.0.1:8765/`에서 제공하며
기본 설정에서는 같은 컴퓨터에서만 접근할 수 있습니다.

## 팀용 업로드 프로그램

`team-serve`는 교육과정, 이전 개정 교과서, 편수자료를 기준 자료로 한 번
등록하고 원고 PDF만 반복해서 업로드하는 웹 화면을 제공합니다. 기준 파일의
해시·버전과 원고별 분석 이력은 `team_data/team.db`에 저장되고, 실제 PDF와
결과는 `team_data/uploads`, `team_data/results`에 보관됩니다.

```powershell
# 현재 컴퓨터에서 사용
python test.py team-serve --host 127.0.0.1 --port 8780

# 같은 사내 네트워크의 팀원과 공유
python test.py team-serve --host 0.0.0.0 --port 8780
```

기본 제한은 원고 한 개당 200MB, 300쪽이며 분석은 한 건씩 대기열로 처리합니다.
현재 버전은 로그인 기능이 없는 사내망용이므로 인터넷에 직접 공개하지 마세요.

## 선택적 AI 재판독

전체 페이지를 전송하지 않도록 AI 연동은 기본적으로 비활성화되어 있습니다.
`adapter.review(crop_path, element)`를 구현한 모듈을 만든 뒤
`--ai-review-module 모듈명`으로 지정하면 자산 파일이 있는 확인 항목만 전달됩니다.
