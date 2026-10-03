#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

quote_python="python3"
if [[ -n "${QUOTE_FIT_PYTHON:-}" ]]; then
  quote_python="$QUOTE_FIT_PYTHON"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python3" ]]; then
  quote_python="$VIRTUAL_ENV/bin/python3"
else
  quote_bundled="${HOME:-}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  if [[ -x "$quote_bundled" ]]; then quote_python="$quote_bundled"; fi
fi
if [[ "$quote_python" != */* ]]; then
  quote_resolved="$(command -v "$quote_python" || true)"
  if [[ -n "$quote_resolved" ]]; then quote_python="$quote_resolved"; fi
fi

if [[ ! -x "$quote_python" ]]; then
  printf 'Python 실행 파일을 찾을 수 없습니다: %s\n' "$quote_python" >&2
  exit 1
fi

quote_python_check() {
  PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10 이상이 필요합니다"; import pypdf'
}

quote_port_free() {
  PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c '
import socket, sys
probe = socket.socket()
probe.settimeout(0.25)
active = probe.connect_ex(("127.0.0.1", 8765)) == 0
probe.close()
if active:
    sys.exit(1)
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", 8765))
except OSError:
    sys.exit(1)
finally:
    s.close()
'
}

quote_assets_check() {
  PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c 'import pathlib; import core; from pypdf import PdfReader; core.load_data(); root=pathlib.Path("."); [((root/path).read_text(encoding="utf-8")) for path in ("시연자료/01_견적요청.txt","시연자료/02_발주변경요청.txt")]; pdf=PdfReader(root/"output/pdf/견적핏_가상견적요청.pdf"); assert pdf.pages and any(page.extract_text() for page in pdf.pages)'
}

quote_ollama_status() {
  PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c 'import json,urllib.request; r=urllib.request.urlopen("http://127.0.0.1:11434/api/tags",timeout=1); m=json.loads(r.read(262144)).get("models",[]); sys_ok=any(x.get("name")=="qwen3:0.6b" or x.get("model")=="qwen3:0.6b" for x in m); raise SystemExit(0 if sys_ok else 1)' 2>/dev/null
}

if [[ "${1:-}" == "--점검" ]]; then
  if [[ "$#" -ne 1 ]]; then printf '사용법: ./실행.command --점검\n' >&2; exit 2; fi
  printf 'Python: %s\n' "$quote_python"
  quote_check_failed=0
  if quote_python_check 2>/dev/null; then printf 'Python 3.10+ 및 pypdf: 확인\n'; else printf 'Python/pypdf: 준비 필요 (requirements.txt 설치 확인)\n'; quote_check_failed=1; fi
  if quote_assets_check 2>/dev/null; then printf '가상 회사표·요청서·PDF: 읽기 확인\n'; else printf '시연 자료: 누락 또는 읽기 오류\n'; quote_check_failed=1; fi
  if quote_port_free 2>/dev/null; then printf '127.0.0.1:8765: 사용 가능\n'; else printf '127.0.0.1:8765: 사용 중\n'; quote_check_failed=1; fi
  if quote_ollama_status; then printf 'Ollama qwen3:0.6b: 연결됨\n'; else
    if PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:11434/api/tags",timeout=1)' >/dev/null 2>&1; then
      printf 'Ollama: 연결됨, qwen3:0.6b 모델 준비 필요\n'
    else
      printf 'Ollama: 연결되지 않음 (규칙 모드는 가능, AI 판단은 불가)\n'
    fi
  fi
  exit "$quote_check_failed"
fi

if [[ "$#" -gt 1 || ( "$#" -eq 1 && "${1:-}" != "--새시연" ) ]]; then
  printf '사용법: ./실행.command [--새시연|--점검]\n' >&2
  exit 2
fi
if ! quote_python_check 2>/dev/null; then
  printf 'Python 3.10 이상과 pypdf가 필요합니다. requirements.txt 설치 지침을 확인하세요.\n' >&2
  exit 1
fi
if ! quote_assets_check 2>/dev/null; then
  printf '시연용 회사표, 요청서 또는 PDF를 읽을 수 없습니다. 파일 경로와 내용을 확인하세요.\n' >&2
  exit 1
fi
if ! quote_port_free 2>/dev/null; then
  printf '127.0.0.1:8765 포트가 이미 사용 중입니다. 기존 앱을 확인하세요.\n' >&2
  exit 1
fi

if [[ "${1:-}" == "--새시연" ]]; then
  mkdir -p data
  quote_stamp="$(date +%Y%m%d-%H%M%S)"
  quote_candidate="$PWD/data/demo-$quote_stamp.sqlite3"
  quote_suffix=1
  while [[ -e "$quote_candidate" || -e "$quote_candidate-wal" || -e "$quote_candidate-shm" ]]; do
    quote_candidate="$PWD/data/demo-$quote_stamp-$quote_suffix.sqlite3"
    quote_suffix=$((quote_suffix + 1))
  done
  export QUOTE_FIT_DB="$quote_candidate"
  printf '새 시연 저장소: %s\n이전 기록은 그대로 보존됩니다.\n' "$QUOTE_FIT_DB"
fi

if ! quote_ollama_status; then
  if PYTHONDONTWRITEBYTECODE=1 "$quote_python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:11434/api/tags",timeout=1)' >/dev/null 2>&1; then
    printf 'Ollama는 연결됐지만 qwen3:0.6b가 없습니다. ollama pull qwen3:0.6b 실행 후 AI 판단을 사용할 수 있습니다.\n'
  elif command -v ollama >/dev/null 2>&1; then
    ollama serve >/dev/null 2>&1 &
    quote_ready=0
    for quote_attempt in {1..15}; do
      if quote_ollama_status; then quote_ready=1; break; fi
      sleep 1
    done
    if [[ "$quote_ready" -eq 1 ]]; then
      printf '로컬 AI 서버와 qwen3:0.6b 모델을 확인했습니다.\n'
    else
      printf 'Ollama가 15초 안에 준비되지 않았습니다. 규칙 모드는 가능하며 AI 판단은 연결 후 사용할 수 있습니다.\n'
    fi
  else
    printf 'Ollama가 없어 규칙 모드만 사용할 수 있습니다. README.md의 AI 연결 절차를 확인하세요.\n'
  fi
fi

printf '견적핏: http://127.0.0.1:8765\n종료: Control+C\n'
exec "$quote_python" server.py
