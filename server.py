"""Loopback-only HTTP API and static-file server for QuoteFit."""
from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core

ROOT = Path(__file__).resolve().parent
# Transport limit includes JSON metadata and Base64 expansion; decoded text and files
# are independently limited to 5 MiB in core.py.
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}


class Handler(BaseHTTPRequestHandler):
    server_version = "QuoteFitLocal/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep request bodies and submitted customer text out of terminal logs.
        print("%s - %s" % (self.client_address[0], fmt % args))

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return bool(re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?", host, re.I))

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        return parsed.scheme == "http" and host in ("127.0.0.1", "localhost") and parsed.netloc == self.headers.get("Host")

    def _body(self) -> dict:
        length = self.headers.get("Content-Length")
        if not length or len(length) > 12 or not length.isdecimal():
            raise core.AppError("요청 본문 길이가 필요합니다.")
        size = int(length)
        if size > MAX_JSON_BODY_BYTES:
            raise core.AppError("요청은 8MB 이하만 처리할 수 있습니다.", 413)
        if self.headers.get_content_type() != "application/json":
            raise core.AppError("Content-Type은 application/json이어야 합니다.", 415)
        try:
            value = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError) as exc:
            raise core.AppError("JSON 요청 본문을 읽을 수 없습니다.") from exc
        if not isinstance(value, dict):
            raise core.AppError("JSON 객체를 보내야 합니다.")
        return value

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"error": "허용되지 않은 Host입니다."}, 403)
            return False
        if not self._origin_ok():
            self._json({"error": "동일 출처 요청만 허용합니다."}, 403)
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/state":
                self._json(core.state())
                return
            if parsed.path == "/api/export":
                values = parse_qs(parsed.query).get("quote_id", [])
                if len(values) != 1 or len(values[0]) > 19 or not values[0].isdecimal():
                    raise core.AppError("quote_id가 필요합니다.")
                versions = parse_qs(parsed.query).get("version", [])
                if len(versions) > 1 or (versions and (len(versions[0]) > 19 or not versions[0].isdecimal())):
                    raise core.AppError("version 형식이 올바르지 않습니다.")
                self._json(core.export_quote(int(values[0]), int(versions[0]) if versions else None))
                return
            relative = STATIC.get(parsed.path)
            if relative:
                path = ROOT / relative
                if not path.is_file():
                    self._json({"error": "화면 파일이 아직 준비되지 않았습니다."}, 404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "경로를 찾을 수 없습니다."}, 404)
        except core.AppError as exc:
            self._json({"error": str(exc)}, exc.status)
        except sqlite3.Error:
            self._json({"error": "로컬 저장소를 읽지 못했습니다."}, 500)
        except (OSError, ValueError) as exc:
            self._json({"error": "요청을 처리하지 못했습니다."}, 500)

    def do_POST(self) -> None:
        if not self._guard():
            return
        try:
            data = self._body()
            if self.path == "/api/run":
                self._json(core.run(data))
            elif self.path == "/api/approve":
                self._json(core.approve(data))
            elif self.path == "/api/clock":
                self._json(core.advance_clock(data))
            else:
                self._json({"error": "경로를 찾을 수 없습니다."}, 404)
        except core.AppError as exc:
            self._json({"error": str(exc)}, exc.status)
        except sqlite3.Error:
            self._json({"error": "로컬 저장소를 갱신하지 못했습니다."}, 500)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "JSON 요청 본문을 읽을 수 없습니다."}, 400)
        except (OSError, ValueError) as exc:
            self._json({"error": "요청을 처리하지 못했습니다."}, 500)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("견적핏 로컬 시연 서버: http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
