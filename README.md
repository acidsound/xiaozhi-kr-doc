# Xiaozhi 문서 Markdown 아카이브

Feishu의 Xiaozhi AI 위키 문서를 브라우저 자동화 없이 Markdown으로 아카이빙한 프로젝트입니다.

원본 시작 URL:

```text
https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb
```

## 현재 산출물

```text
archive/markdown/     중국어 원문 Markdown 18개
archive/markdown_kr/  한국어 번역 Markdown 18개
archive/assets/       Markdown에서 참조하는 이미지 파일
```

한국어 번역본은 원문과 같은 파일명을 사용합니다. 본문 제목과 설명은 한국어로 옮겼고, URL, 펌웨어 파일명, 보드 타입, GPIO, 명령어 같은 기술 식별자는 그대로 유지했습니다.

`archive/`는 로컬 작업 산출물입니다. GitHub에 올릴 소스 저장소에는 포함하지 않고, 필요할 때만 이 산출물로부터 GitHub Wiki용 페이지를 별도로 생성하는 구조를 권장합니다.

각 문서 상단의 `Source:` 또는 `원문:` URL은 원래 Feishu URL을 그대로 둡니다. 대신 본문 안의 Feishu wiki 링크는 같은 디렉터리에 아카이브된 문서의 `Source:`/`원문:` URL과 일치하면 상대 Markdown 링크로 바꿉니다.

이번 수집에서는 공개 SSR로 접근 가능한 문서와 공개 `docx` URL을 함께 확인해 저장했습니다. `my.feishu.cn` Wiki URL이 실제 공개 테넌트 도메인으로 리디렉션되는 경우에는 로그인 페이지로 판단해 버리지 않고 `redirect_uri`의 문서 URL을 다시 사용합니다. 카메라 배선 문서와 개발자 인증 문서처럼 Feishu Wiki URL만으로 부족한 경우에는 브라우저에서 저장한 로컬 HTML 또는 같은 토큰의 `docx` URL을 함께 사용했습니다.

Markdown 본문의 이미지 링크는 원격 Feishu URL을 그대로 두지 않고 `archive/assets/` 아래 파일로 내려받은 뒤 상대 경로로 연결합니다.

## 수집 방식

브라우저에서 로그인 없이 열리는 Feishu 공개 문서는 게스트 세션 쿠키를 받은 뒤 서버 사이드 렌더링 HTML을 내려받을 수 있습니다. 이 프로젝트의 기본 방식은 그 HTML을 파싱해 Markdown으로 변환하는 것입니다.

Feishu는 `https://my.feishu.cn/wiki/...` 요청을 `https://{tenant}.feishu.cn/wiki/...` 같은 실제 공개 도메인으로 넘기거나, 쿼리 파라미터에 따라 이미지/표가 빠진 앱 셸 HTML을 돌려줄 수 있습니다. 수집기는 먼저 공개 URL을 해석한 뒤 `open_in_browser=true` 등 SSR 후보 URL을 비교해 이미지와 표 블록이 가장 많이 들어 있는 HTML을 사용합니다.

Playwright나 Chrome을 띄우지 않으므로 렌더링 대기 시간이 없고, Feishu OpenAPI 앱 권한도 필요하지 않습니다.

## 다시 수집하기

시작 문서만 저장:

```sh
python3 scripts/archive_feishu.py 'https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb'
```

링크된 하위 Wiki 페이지까지 저장:

```sh
python3 scripts/archive_feishu.py --recursive --depth -1 --max-pages 100 --no-raw 'https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb'
```

`--no-raw`를 빼면 원본 SSR HTML/API 응답이 `archive/raw/`에 함께 저장됩니다. 현재 저장소에는 Markdown 결과만 남기기 위해 raw 산출물을 생성하지 않았습니다.

이미지는 기본적으로 `archive/assets/`에 복사 또는 다운로드됩니다. 원격 이미지가 그대로 필요하면 `--no-assets`를 사용하세요.

수집 후에는 2-pass로 내부 링크를 정리합니다. 예를 들어 본문 안의 `https://my.feishu.cn/wiki/W14Kw1s1uieoKjkP8N0c1VVvn8d` 링크는 해당 URL을 `Source:`로 가진 `🍒v2.2.4 小智AI终端最新版本固件及源码下载.md`가 있으면 상대 링크로 바뀝니다. Feishu 원본 URL을 그대로 두고 싶으면 `--no-local-links`를 사용하세요.

깊이 기준:

```text
--depth 0   시작 문서만
--depth 1   시작 문서 + 직접 링크된 문서
--depth 2   직접 링크된 문서의 링크까지
--depth -1  --max-pages에 닿을 때까지 계속
```

출력 위치를 바꾸려면 `--out`을 사용합니다.

```sh
python3 scripts/archive_feishu.py --recursive --depth -1 --max-pages 100 --no-raw --out archive/markdown 'https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb'
```

## GitHub Wiki로 내보내기

소스 저장소에는 `archive/`를 커밋하지 않고, 로컬 산출물을 읽어 GitHub Wiki용 별도 디렉터리를 생성합니다. 이 저장소에서는 `.wiki-build/`가 그 출력 위치입니다.

```sh
python3 scripts/build_github_wiki.py
```

생성 결과:

```text
.wiki-build/
  Home.md
  Home-ko.md
  Home-zh.md
  _Sidebar.md
  images/
  ZH-*.md
  KO-*.md
```

이 출력물은 GitHub Wiki에 맞게 다음을 처리합니다.

- 페이지 파일명을 공백 없는 안전한 이름으로 변환
- 내부 Markdown 링크를 새 Wiki 페이지 이름으로 재작성
- `archive/assets/` 이미지를 `images/`로 복사하고 링크 수정
- 한국어/중국어 시작 페이지와 Wiki 사이드바 생성

실제 게시 순서는 보통 다음과 같습니다.

```sh
git clone https://github.com/YOUR-USER/xiaozhi-kr-doc.wiki.git
rsync -a --delete .wiki-build/ xiaozhi-kr-doc.wiki/
cd xiaozhi-kr-doc.wiki
git add .
git commit -m "Update wiki"
git push
```

GitHub Wiki는 저장소 본체와 별도의 Git 저장소입니다. 예를 들어 저장소 이름을 `xiaozhi-kr-doc`으로 만들면 Wiki 원격은 `xiaozhi-kr-doc.wiki.git`입니다. GitHub 공식 문서에 따르면 Wiki는 로컬에서 편집한 뒤 `YOUR-REPOSITORY.wiki.git`으로 푸시할 수 있고, `_Sidebar.md` 파일로 사이드바를 구성할 수 있습니다. 참고: [About wikis](https://docs.github.com/en/communities/documenting-your-project-with-wikis/about-wikis), [Adding or editing wiki pages](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages), [Creating a footer or sidebar for your wiki](https://docs.github.com/en/communities/documenting-your-project-with-wikis/creating-a-footer-or-sidebar-for-your-wiki)

Wiki URL은 로그인 페이지를 반환하지만 같은 토큰의 `docx` URL이 공개로 열리는 경우가 있습니다. 이때는 `Source:`에는 원래 Wiki URL을 남기고, 실제 HTML은 `--fetch-url`로 지정한 `docx` URL에서 가져올 수 있습니다. 다만 `docx`도 가상 스크롤을 쓰므로 하단 블록이 빠지면 브라우저에서 해당 위치까지 스크롤한 뒤 저장한 HTML을 함께 확인해야 합니다.

```sh
python3 scripts/archive_feishu.py --fetch-url 'https://smvsudqc87.feishu.cn/docx/AjtQdM0KpoVQ3YxJfaLco4KsnSe' --out archive/markdown 'https://my.feishu.cn/wiki/AjtQdM0KpoVQ3YxJfaLco4KsnSe'
```

## 로컬 HTML 변환

브라우저에서 저장한 Feishu HTML 파일이 있으면 네트워크 요청 없이 Markdown으로 변환할 수 있습니다. 첫 번째 인자는 문서의 원래 Feishu URL로, `Source:`에 그대로 기록됩니다.

```sh
python3 scripts/archive_feishu.py --html 'archive/markdown/_________________________________________________【最新】小智AI面包板带摄像头互动功能接线教程 - Feishu Docs.html' --out archive/markdown 'https://my.feishu.cn/wiki/GoGBdLVUooHARPxyTLhc9AjdnFh'
```

주의: Feishu 문서는 가상 스크롤을 사용하므로 브라우저의 "페이지 저장" HTML에는 저장 당시 렌더링된 블록만 들어갈 수 있습니다. 여러 위치에서 저장한 HTML이 있으면 각 파일에서 추출된 구간을 합쳐야 할 수 있습니다. 문서 전체가 필요하면 브라우저에서 끝까지 스크롤한 뒤 저장하거나, Feishu의 내보내기 기능으로 전체 HTML/Markdown/Word를 확보하는 편이 안전합니다.

## API 방식

기본 수집에는 필요하지 않지만, Feishu OpenAPI 권한이 있는 경우 API 모드도 사용할 수 있습니다.

필요 권한:

```text
wiki:wiki:readonly
docx:document:readonly
drive:drive:readonly
```

앱 ID/Secret로 tenant token을 자동 발급:

```sh
export FEISHU_APP_ID='cli_xxx'
export FEISHU_APP_SECRET='xxx'
python3 scripts/archive_feishu.py --source api 'https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb'
```

이미 발급된 토큰을 쓰는 경우:

```sh
export FEISHU_ACCESS_TOKEN='t-xxx-or-u-xxx'
python3 scripts/archive_feishu.py --source api 'https://my.feishu.cn/wiki/F5krwD16viZoF0kKkvDcrZNYnhb'
```

API 모드에서도 raw 응답을 남기고 싶지 않으면 `--no-raw`를 같이 사용하세요.
