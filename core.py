"""Local, fictional-data MVP for the Gyeongnam quote-fit demonstration."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "시연자료" / "가상_회사데이터.json"
DB_PATH = Path(os.environ.get("QUOTE_FIT_DB", ROOT / "견적핏.sqlite3"))
MODEL = "qwen3:0.6b"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
SCHEMA = {
    "type": "object", "required": ["action", "reason"],
    "properties": {"action": {"type": "string"}, "reason": {"type": "string"}},
    "additionalProperties": False,
}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_data() -> dict[str, Any]:
    with DATA_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if data.get("data_kind") != "fictional_demo":
        raise AppError("가상 시연 데이터가 확인되지 않아 실행을 중단했습니다.", 500)
    return data


def _data_fingerprint(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(str(path or DB_PATH), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS quotes (
      row_id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id INTEGER NOT NULL,
      version INTEGER NOT NULL, day INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
      request_json TEXT NOT NULL, quote_json TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(quote_id, version)
    );
    CREATE TABLE IF NOT EXISTS approvals (
      quote_id INTEGER NOT NULL, version INTEGER NOT NULL, option_id TEXT NOT NULL,
      status TEXT NOT NULL, agreed INTEGER NOT NULL DEFAULT 0,
      fingerprint TEXT NOT NULL, approved_at TEXT,
      PRIMARY KEY(quote_id, version),
      FOREIGN KEY(quote_id, version) REFERENCES quotes(quote_id, version)
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT OR IGNORE INTO settings(key,value) VALUES('day','0');
    INSERT OR IGNORE INTO settings(key,value) VALUES('next_quote_id','1');
    """)
    return db


def get_day(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT value FROM settings WHERE key='day'").fetchone()[0])


def _log(action: str, detail: str, status: str = "ok") -> dict[str, str]:
    return {"type": "tool", "action": action, "status": status, "detail": detail, "at": now()}


def _decision_reason(action: str, facts: dict[str, Any]) -> str:
    if action == "ask_missing":
        return f"필수 사양 확인 필요: {', '.join(facts.get('missing', []))}"
    if action == "lookup":
        return "필수 사양 확인 완료; 최신 승인 원가·재고·공정 조회"
    if action == "requery_all":
        return f"{facts.get('snapshot', '최신')} 자료로 변경·만료 조건 재조회"
    if action == "calculate":
        return "확인된 입력을 결정론적 계산 도구에 전달"
    if action == "try_alternative":
        return (f"표준 납기 {facts.get('standard_delivery')}일이 희망 {facts.get('wanted_delivery')}일을 넘겨 "
                "허용 대안을 재검증")
    if action == "handoff":
        return "자동 검증을 중단하고 담당자 확인 요청"
    return "허용된 다음 단계를 선택"


def decide(mode: str, allowed: set[str], facts: dict[str, Any], fallback: str,
           logs: list[dict[str, Any]]) -> str:
    """Ask local Ollama for a constrained next action; reject unsafe/unknown output."""
    if mode != "ai":
        logs.append({"type": "model_decision", "provider": "rules", "model": None,
                     "success": False, "action": fallback, "reason": "규칙 모드 선택", "fallback": True, "at": now()})
        return fallback
    proposed_action, proposed_reason = None, None
    payload = {"model": MODEL, "stream": False, "think": False, "format": SCHEMA,
               "options": {"temperature": 0, "seed": 42, "num_predict": 160},
               "messages": [{"role": "system", "content": (
                   "You are a quote workflow agent. Your goal is to prepare a validated option that meets the customer's requested delivery deadline. "
                   "Choose exactly one safe next action from the allowed list. Evaluate all confirmed allowed alternatives before handing off; "
                   "if reason_for_replan is confirmed_candidate_not_yet_evaluated and try_alternative is allowed, choose try_alternative. "
                   "Quantity changes require requery_all before recalculation. "
                   "Never invent values, prices, approvals, or external actions. Keep the reason short and write it in Korean. Return JSON with action and reason.")},
                 {"role": "user", "content": json.dumps({"allowed_actions": sorted(allowed), "facts": facts}, ensure_ascii=False)}]}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as response:
            result = json.loads(response.read(1024 * 1024))
        content = result.get("message", {}).get("content", "")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("model output must be an object")
        action, reason = parsed.get("action"), parsed.get("reason")
        proposed_action = str(action)[:100]
        proposed_reason = reason[:300] if isinstance(reason, str) else None
        if set(parsed) != {"action", "reason"} or not isinstance(action, str) or not isinstance(reason, str) or not reason.strip():
            raise ValueError("model output violates action/reason schema")
        reason = reason[:300]
        proposed_action, proposed_reason = str(action)[:100], reason
        if action not in allowed:
            raise ValueError("model action outside allowlist")
        if action == "calculate" and (facts.get("expired") or facts.get("changed_items") or facts.get("reason") == "same_condition_revalidation"):
            raise ValueError("calculate conflicts with required revalidation")
        logs.append({"type": "model_decision", "provider": "ollama-local", "model": MODEL,
                     "success": True, "action": action, "reason": _decision_reason(action, facts),
                     "reason_source": "validated_facts", "model_reason_unverified": reason,
                     "fallback": False, "at": now()})
        return action
    except Exception as exc:
        logs.append({"type": "model_decision", "provider": "ollama-local", "model": MODEL,
                     "success": False, "action": fallback, "reason": "모델 호출/출력 검증 실패",
                     "error": type(exc).__name__, "rejected_action": proposed_action,
                     "rejected_model_reason_unverified": proposed_reason, "fallback": True, "at": now()})
        return fallback


def _extract_file(file: dict[str, Any]) -> str:
    if not isinstance(file, dict):
        raise AppError("첨부 파일 정보가 올바르지 않습니다.")
    name = file.get("name", "")
    if not isinstance(name, str) or not name or len(name) > 255:
        raise AppError("첨부 파일 이름이 올바르지 않습니다.")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AppError("첨부 파일 이름에 올바르지 않은 유니코드 문자가 있습니다.") from exc
    encoded = file.get("base64", "")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise AppError("첨부 파일은 5MB 이하만 처리할 수 있습니다.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise AppError("첨부 파일의 Base64 형식이 올바르지 않습니다.") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise AppError("첨부 파일은 5MB 이하만 처리할 수 있습니다.")
    if name.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            from io import BytesIO
            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages)
        except ImportError as exc:
            raise AppError("PDF 읽기 모듈 pypdf가 없습니다. TXT 붙여넣기를 이용하세요.", 501) from exc
        except Exception as exc:
            raise AppError("PDF를 읽지 못했습니다. 파일이 손상되지 않았는지 확인하세요.") from exc
        if not text.strip():
            raise AppError("텍스트를 추출할 수 있는 PDF만 지원합니다. 스캔 PDF/OCR은 지원하지 않습니다.", 422)
        return text
    if name.lower().endswith((".txt", ".eml")):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError("TXT/메일은 UTF-8 인코딩만 지원합니다.") from exc
    raise AppError("PDF, TXT, EML 파일만 처리할 수 있습니다.")


def _parse(text: str, kind: str, confirmed_request: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(text, str):
        raise AppError("입력은 UTF-8 기준 5MiB 이하의 텍스트여야 합니다.")
    try:
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise AppError("입력은 UTF-8 기준 5MiB 이하의 텍스트여야 합니다.")
    except UnicodeEncodeError as exc:
        raise AppError("입력에 올바르지 않은 유니코드 문자가 있습니다.") from exc
    # This parser deliberately accepts only the supplied synthetic format.
    codes = {f"P{i:02d}" for i in range(1, 11)}
    items: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(P\d{2})\s*\|\s*([^|]+)\|\s*(\d+)\s*\|\s*([^|]*)\|\s*([^|]+)\|\s*([^|]+)\s*$", line)
        if m:
            code, name, qty, material, drawing, revision = m.groups()
            if code not in codes or code in items:
                raise AppError(f"{code} 품목 코드가 중복되었거나 지원되지 않습니다.")
            try:
                quantity = int(qty)
            except ValueError as exc:
                raise AppError(f"{code} 수량이 올바른 정수가 아닙니다.", 422) from exc
            if quantity <= 0:
                raise AppError(f"{code} 수량은 1개 이상이어야 합니다.", 422)
            items[code] = {"item_code": code, "item_name": name.strip(), "quantity": quantity,
                           "source_material": material.strip(), "material": material.strip(),
                           "drawing_id": drawing.strip(), "revision": revision.strip()}
    if kind == "order":
        allowed_lines = (ROOT / "시연자료" / "02_발주변경요청.txt").read_text(encoding="utf-8").splitlines()
        # ponytail: this demo accepts one order format; use a reviewed parser for broader customer documents.
        if [line.strip() for line in text.splitlines() if line.strip()] != [line.strip() for line in allowed_lines if line.strip()]:
            raise AppError("지원하는 가상 발주 변경 형식과 다릅니다. 재질·도면·납기 등 다른 변경은 담당자 검토가 필요합니다.", 422)
        base = json.loads(json.dumps(confirmed_request or {}))
        if not base.get("items"):
            raise AppError("기존 견적 요청 정보가 없어 변경분을 해석할 수 없습니다.", 409)
        recognized = False
        for line in text.splitlines():
            if not re.search(r"\bP\d{2}\b", line) or not re.search(r"수량|\d+\s*개", line):
                continue
            change = re.fullmatch(r"\s*P01\s+[^\s]+\s+수량을\s+10개에서\s+20개로\s+변경합니다[.。]?\s*", line)
            if not change:
                raise AppError("지원 형식과 다른 품목 변경이 있어 자동 재검증을 중단했습니다.", 422)
            if base["items"]["P01"]["quantity"] != 10:
                raise AppError("P01 이전 수량이 10개가 아니어서 변경을 확인할 수 없습니다.", 409)
            base["items"]["P01"]["quantity"] = 20
            recognized = True
        # Only the explicit synthetic P01 transition is currently supported.
        if not recognized:
            raise AppError("변경 요청에서 P01 수량을 확인하지 못했습니다.")
        base["wanted_delivery_days"] = 5
        base["source"] = "synthetic_order_change"
        return base
    if len(items) != 10:
        raise AppError("지원하는 가상 견적은 P01~P10 총 10개 품목이어야 합니다.", 422)
    headers = [[part.strip() for part in line.split("|")] for line in text.splitlines() if "품목코드" in line and "|" in line]
    if headers != [["품목코드", "품명", "수량(개)", "재질", "도면번호", "개정"]]:
        raise AppError("지원하는 표의 수량 단위는 개입니다. 다른 단위나 표 형식은 담당자 검토가 필요합니다.", 422)
    deadlines = re.findall(r"희망 납기\s*[:：]?\s*[^\n]*?(\d+)\s*영업일", text)
    if deadlines != ["5"]:
        raise AppError("희망 납기 5영업일을 확인할 수 없습니다.", 422)
    return {"items": items, "wanted_delivery_days": 5, "source": "synthetic_rfq"}


def _snapshot(data: dict[str, Any], day: int) -> dict[str, Any]:
    eligible = [s for s in data["snapshots"] if s["observed_at_simulation_business_day"] <= day]
    if not eligible:
        raise AppError("해당 시뮬레이션 시점의 조회 자료가 없습니다.", 409)
    snapshot = eligible[-1]
    age = day - snapshot["observed_at_simulation_business_day"]
    if age >= snapshot["valid_for_business_days"]:
        raise AppError("최신 원가·재고 자료가 만료되어 계산할 수 없습니다.", 409)
    return snapshot


def _calculate(request: dict[str, Any], snapshot: dict[str, Any], day: int,
               alternative: bool = False, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_data()
    records = {x["item_code"]: x for x in snapshot["records"]}
    numerator = data["rules"]["price_markup_numerator"]
    denominator = data["rules"]["price_markup_denominator"]
    lines, missing, unknown, max_delivery, total_cost = [], [], [], 0, 0
    if not snapshot.get("shared_inspection_shipping_capacity_confirmed"):
        return {"missing": [], "unknown": sorted(request["items"]), "lines": []}
    for code, item in request["items"].items():
        rec = records.get(code)
        if not item.get("material"):
            missing.append(code); continue
        if not rec or (item["drawing_id"], item["revision"], item["material"]) != (rec["drawing_id"], rec["revision"], rec["material"]):
            unknown.append(code); continue
        qty = item["quantity"]
        if qty not in rec["supported_quantities"]:
            unknown.append(code); continue
        shortage = max(0, qty * rec["blank_count_per_unit"] - rec["available_blank_count"])
        cost = rec["approved_unit_cost_krw"] * qty
        if alternative and code == "P01":
            alt = next((a for a in data["alternatives"] if a["snapshot_id"] == snapshot["snapshot_id"] and a["item_code"] == code and a["supported_quantity"] == qty), None)
            if not alt or not alt["same_specification_confirmed"] or not alt["capacity_confirmed"]:
                unknown.append(code); continue
            cost += alt["rush_extra_total_krw"] + alt["outsourcing_extra_total_krw"]
            delivery = max(alt["material_ready_elapsed_days"], alt["production_ready_elapsed_days"]) + alt["machining_elapsed_days"] + alt["inspection_shipping_elapsed_days"]
            assumptions = ["P01 허용 긴급 소재·외주 대안", "소재·외주 추가비는 작업 총액"]
            if qty * rec["blank_count_per_unit"] > alt["available_blank_count"]:
                unknown.append(code); continue
        else:
            if shortage:
                material_ready = rec["normal_material_ready_elapsed_days"]
                if material_ready is None:
                    unknown.append(code); continue
            else:
                material_ready = 0
            delivery = max(material_ready, rec["production_ready_elapsed_days"]) + rec["machining_elapsed_days"] + rec["inspection_shipping_elapsed_days"]
            assumptions = ["승인 표준 원가를 전체 수량에 적용"]
        if not rec["capacity_confirmed"]:
            unknown.append(code); continue
        if cost * numerator % denominator or cost % qty:
            unknown.append(code); continue
        max_delivery = max(max_delivery, delivery)
        total_cost += cost
        lines.append({"item_code": code, "item_name": item["item_name"], "quantity": qty,
                      "unit_cost_krw": cost // qty,
                      "base_unit_cost_krw": rec["approved_unit_cost_krw"],
                      "extra_total_krw": cost - rec["approved_unit_cost_krw"] * qty,
                      "total_krw": (cost * numerator // denominator), "cost_total_krw": cost,
                      "delivery_days": delivery, "assumptions": assumptions})
    if missing or unknown:
        return {"missing": missing, "unknown": unknown, "lines": lines}
    if total_cost * numerator % denominator:
        return {"missing": [], "unknown": sorted(request["items"]), "lines": []}
    return {"missing": [], "unknown": [], "lines": lines, "cost_krw": total_cost,
            "total_krw": total_cost * numerator // denominator, "delivery_days": max_delivery,
            "meets_requested": max_delivery <= request["wanted_delivery_days"]}


def _fingerprint(quote: dict[str, Any]) -> str:
    keys = ("snapshot_id", "data_fingerprint", "request", "options", "changes") if "data_fingerprint" in quote else (
        "snapshot_id", "request", "options", "changes")
    return hashlib.sha256(json.dumps({k: quote.get(k) for k in keys},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _save(db: sqlite3.Connection, quote_id: int | None, version: int, day: int,
          snapshot_id: str, request: dict[str, Any], quote: dict[str, Any], fault: str | None = None,
          expected_latest_version: int | None = None, expected_data_fingerprint: str | None = None) -> int:
    if fault == "save":
        raise sqlite3.OperationalError("injected demo save failure")
    try:
        db.execute("BEGIN IMMEDIATE")
        if get_day(db) != day:
            raise AppError("저장 중 시연 시점이 바뀌었습니다. 최신 상태를 불러와 다시 검증하세요.", 409)
        data = load_data()
        if expected_data_fingerprint and _data_fingerprint(data) != expected_data_fingerprint:
            raise AppError("저장 중 회사 기준 자료가 바뀌었습니다. 다시 조회해 주세요.", 409)
        fresh = _snapshot(data, day)
        if fresh["snapshot_id"] != snapshot_id:
            raise AppError("저장 중 조회 기준 자료가 바뀌었습니다. 다시 검증하세요.", 409)
        if quote_id is None:
            quote_id = int(db.execute("SELECT value FROM settings WHERE key='next_quote_id'").fetchone()[0])
            db.execute("UPDATE settings SET value=? WHERE key='next_quote_id'", (str(quote_id + 1),))
        else:
            latest = db.execute("SELECT MAX(version) FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()[0]
            if latest != expected_latest_version:
                raise AppError("저장 중 최신 견적이 바뀌었습니다. 최신 버전을 불러와 다시 검증하세요.", 409)
            if version != latest + 1:
                raise AppError("새 견적 버전이 순서와 맞지 않습니다. 다시 검증하세요.", 409)
        quote["id"], quote["version"], quote["day"], quote["snapshot_id"] = quote_id, version, day, snapshot_id
        quote["request"] = request
        quote["source_text"], quote["source_name"] = request.get("source_text", ""), request.get("source_name")
        db.execute("INSERT INTO quotes(quote_id,version,day,snapshot_id,request_json,quote_json,created_at) VALUES(?,?,?,?,?,?,?)",
                   (quote_id, version, day, snapshot_id, json.dumps(request, ensure_ascii=False),
                    json.dumps(quote, ensure_ascii=False), now()))
        db.execute("INSERT INTO approvals(quote_id,version,option_id,status,fingerprint) VALUES(?,?,?,'pending',?)",
                   (quote_id, version, "", _fingerprint(quote)))
        if version > 1:
            db.execute("UPDATE approvals SET status='stale' WHERE quote_id=? AND status='approved'", (quote_id,))
        stored = db.execute("SELECT quote_json FROM quotes WHERE quote_id=? AND version=?", (quote_id, version)).fetchone()
        approval = db.execute("SELECT status,fingerprint FROM approvals WHERE quote_id=? AND version=?",
                              (quote_id, version)).fetchone()
        if not stored or json.loads(stored["quote_json"]) != quote or not approval or approval["status"] != "pending":
            raise sqlite3.IntegrityError("saved quote/approval readback mismatch")
        quote["logs"].append(_log("save_and_readback", f"V{version} 견적과 승인 작업 DB 재조회 확인"))
        db.execute("UPDATE quotes SET quote_json=? WHERE quote_id=? AND version=?",
                   (json.dumps(quote, ensure_ascii=False), quote_id, version))
        verified = db.execute("SELECT quote_json FROM quotes WHERE quote_id=? AND version=?", (quote_id, version)).fetchone()
        if not verified or json.loads(verified["quote_json"]) != quote:
            raise sqlite3.IntegrityError("saved quote final readback mismatch")
        db.commit()
        return quote_id
    except Exception:
        db.rollback()
        raise


def _latest(db: sqlite3.Connection, quote_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM quotes WHERE quote_id=? ORDER BY version DESC LIMIT 1", (quote_id,)).fetchone()
    if not row:
        raise AppError("견적을 찾을 수 없습니다.", 404)
    return row


def run(payload: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("요청은 JSON 객체여야 합니다.")
    mode = payload.get("mode", "ai")
    if mode not in ("ai", "rules"):
        raise AppError("mode는 ai 또는 rules여야 합니다.")
    kind = payload.get("kind")
    if kind not in ("rfq", "order", "revalidate"):
        raise AppError("kind는 rfq, order 또는 revalidate여야 합니다.")
    day = payload.get("day")
    if isinstance(day, bool) or not isinstance(day, int) or day not in (0, 4, 7):
        raise AppError("시연 실행 시점 day는 0, 4 또는 7만 허용합니다.")
    text = payload.get("text", "")
    if payload.get("file") is not None:
        if kind == "revalidate":
            raise AppError("기존 조건 재검증에는 첨부 파일을 사용할 수 없습니다.")
        text = _extract_file(payload["file"])
    source_file = payload.get("file")
    source_name = source_file.get("name") if isinstance(source_file, dict) else None
    db = connect(db_path)
    logs: list[dict[str, Any]] = []
    source_text = text
    try:
        if get_day(db) != day:
            raise AppError("화면의 시뮬레이션 날짜가 현재 날짜와 다릅니다. 먼저 시계를 맞춰 주세요.", 409)
        data = load_data()
        data_fingerprint = _data_fingerprint(data)
        if kind == "rfq":
            request = _parse(text, kind)
            request["source_text"], request["source_name"] = text, source_name
            request["source_history"] = [{"kind": "rfq", "text": text, "name": source_name}]
            confirmations = payload.get("confirmations", {})
            if not isinstance(confirmations, dict):
                raise AppError("확인값 형식이 올바르지 않습니다.")
            for code, value in confirmations.items():
                if not isinstance(code, str) or code not in request["items"] or not isinstance(value, str) or value not in {"AL6061"}:
                    raise AppError("확인값은 요청 품목의 허용된 재질이어야 합니다.")
                item = request["items"][code]
                if item["source_material"]:
                    raise AppError(f"{code} 재질은 원문에 있어 확인값을 적용할 수 없습니다.", 422)
                item["material"] = value
            logs.append(_log("parse_rfq", "가상 RFQ P01~P10 필드 확인"))
            if re.search(r"(가격\s*규칙\s*변경|승인\s*생략|무시하고|system prompt|ignore previous)", text, re.I):
                logs.append(_log("input_guard", "문서 내 작업 규칙 지시는 데이터로 취급하고 무시", "warning"))
            missing = [c for c, item in request["items"].items() if not item["material"]]
            allowed = {"ask_missing"} if missing else {"lookup"}
            action = decide(mode, allowed, {"missing": missing, "kind": kind}, "ask_missing" if missing else "lookup", logs)
            if action == "ask_missing" or missing:
                title = "필수 사양 확인 필요"
                if mode == "ai" and not _ai_used(logs):
                    title = "규칙 경로 사용 — AI 판단 미검증 · " + title
                return {"status": "needs_input", "title": title, "message": f"{', '.join(missing)} 재질을 확인하면 견적을 계산합니다.",
                        "items": list(request["items"].values()), "missing": missing, "quote": None,
                        "logs": logs, "mode": "ai" if mode == "ai" else "rules", "ai_used": _ai_used(logs),
                        "planner_mode": _planner_mode(logs), "source_text": source_text}
            snap = _snapshot(data, day)
            if payload.get("fault") == "inventory":
                logs.append(_log("lookup_snapshot", "가상 재고 조회 장애 주입", "error"))
                title = "재고 조회 실패"
                if mode == "ai" and not _ai_used(logs): title = "규칙 경로 사용 — AI 판단 미검증 · " + title
                return {"status": "error", "title": title,
                        "message": "최신 재고 자료를 확인하지 못해 견적을 저장하지 않았습니다.",
                        "items": list(request["items"].values()), "missing": [], "quote": None,
                        "logs": logs, "mode": mode, "ai_used": _ai_used(logs),
                        "planner_mode": _planner_mode(logs), "source_text": source_text}
            logs.append(_log("lookup_snapshot", f"{snap['snapshot_id']} 전체 품목 원가·재고·공정 조회"))
            result = _calculate(request, snap, day, data=data)
            logs.append(_log("calculate", "정수 원가×5/4 및 병목 납기 계산"))
            if result.get("unknown"):
                return {"status": "needs_review", "title": "확인할 조건이 있습니다", "message": "지원되지 않는 사양 또는 수량이 있습니다.",
                        "items": list(request["items"].values()), "missing": [], "issues": result["unknown"], "quote": None, "logs": logs, "mode": mode,
                        "ai_used": _ai_used(logs), "planner_mode": _planner_mode(logs), "source_text": source_text}
            option = _option("standard", "표준 조건", result)
            quote = {"options": [option], "changes": [], "approval": {"status": "pending", "option_id": ""}, "logs": logs,
                     "valid_until_day": day + data["rules"]["quote_valid_business_days"], "data_kind": "fictional_demo",
                     "data_fingerprint": data_fingerprint, "ai_used": _ai_used(logs), "planner_mode": _planner_mode(logs)}
            quote_id = _save(db, None, 1, day, snap["snapshot_id"], request, quote, payload.get("fault"),
                             expected_data_fingerprint=data_fingerprint)
            quote["logs"] = logs
            return _run_result("approval_pending", "견적 초안 저장", "가격·납기 근거를 저장했습니다. 담당자 검토가 필요합니다.",
                    list(request["items"].values()), [], quote, logs, mode, source_text)
        quote_id = payload.get("quote_id")
        if isinstance(quote_id, bool) or not isinstance(quote_id, int) or not 1 <= quote_id <= 9_223_372_036_854_775_807:
            raise AppError("발주 변경에는 기존 quote_id가 필요합니다.")
        old = _latest(db, quote_id)
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1 or expected_version != old["version"]:
            raise AppError("기준으로 선택한 견적이 최신 버전이 아닙니다. 최신 버전을 다시 불러오세요.", 409)
        old_quote, old_req = json.loads(old["quote_json"]), json.loads(old["request_json"])
        if old_quote.get("data_kind") != "fictional_demo":
            raise AppError("가상 시연 견적만 처리할 수 있습니다.", 409)
        if kind == "revalidate":
            if old_quote.get("data_kind") != "fictional_demo":
                raise AppError("가상 시연 견적만 처리할 수 있습니다.", 409)
            new_request = json.loads(json.dumps(old_req))
            if not new_request.get("source_history"):
                history = []
                for source_row in db.execute("SELECT request_json FROM quotes WHERE quote_id=? ORDER BY version", (quote_id,)):
                    saved_request = json.loads(source_row["request_json"])
                    history.append({"kind": "order_change" if saved_request.get("source") == "synthetic_order_change" else "rfq",
                                    "text": saved_request.get("source_text", ""), "name": saved_request.get("source_name")})
                new_request["source_history"] = history
            source_text = old_req.get("source_text", "")
            source_name = old_req.get("source_name")
            text = source_text
        else:
            new_request = _parse(text, "order", old_req)
            new_request["source_text"], new_request["source_name"] = text, source_name
            history = list(old_req.get("source_history") or [
                {"kind": "rfq", "text": old_req.get("source_text", ""), "name": old_req.get("source_name")}])
            history.append({"kind": "order_change", "text": text, "name": source_name})
            new_request["source_history"] = history
        if re.search(r"(가격\s*규칙\s*변경|승인\s*생략|무시하고|system prompt|ignore previous)", text, re.I):
            logs.append(_log("input_guard", "문서 내 작업 규칙 지시는 데이터로 취급하고 무시", "warning"))
        changes = [{"item_code": code, "before": old_req["items"][code]["quantity"], "after": item["quantity"]}
                   for code, item in new_request["items"].items() if old_req["items"][code]["quantity"] != item["quantity"]]
        if not changes and kind != "revalidate":
            raise AppError("확인 가능한 수량 변경이 없습니다.", 422)
        snap = _snapshot(data, day)
        if payload.get("fault") == "inventory":
            logs.append(_log("lookup_snapshot", "가상 재고 조회 장애 주입", "error"))
            title = "재고 조회 실패"
            if mode == "ai" and not _ai_used(logs): title = "규칙 경로 사용 — AI 판단 미검증 · " + title
            return {"status": "error", "title": title,
                    "message": "최신 재고 자료를 확인하지 못해 변경 견적을 저장하지 않았습니다.",
                    "items": list(new_request["items"].values()), "missing": [], "quote": None,
                    "logs": logs, "mode": mode, "ai_used": _ai_used(logs),
                    "planner_mode": _planner_mode(logs), "source_text": source_text}
        expired = day - old["day"] >= data["rules"]["quote_expired_at_or_after_elapsed_days"]
        logs.append(_log("compare_request", f"변경 항목 {len(changes)}개; 이전 버전 {'만료' if expired else '유효'}"))
        allowed_actions = {"requery_all"} if kind == "revalidate" else {"requery_all", "calculate"}
        fallback_action = "requery_all" if kind == "revalidate" or expired or changes else "calculate"
        action = decide(mode, allowed_actions, {"expired": expired, "day": day,
                         "changed_items": changes, "snapshot": snap["snapshot_id"], "reason": "same_condition_revalidation" if kind == "revalidate" else None},
                        fallback_action, logs)
        if action == "requery_all" or expired or snap["snapshot_id"] != old["snapshot_id"] or kind == "revalidate":
            detail = "저장된 요청·확인값으로 최신 원가·재고·공정 조회" if kind == "revalidate" else "변경·만료 영향 품목 및 공유 여력 재조회"
            logs.append(_log("lookup_snapshot", f"{snap['snapshot_id']} {detail}"))
        standard = _calculate(new_request, snap, day, data=data)
        logs.append(_log("calculate_standard", "최신 원가·재고·공정 기준 표준 경로 재계산"))
        if standard.get("unknown") or standard.get("missing"):
            return {"status": "needs_review", "title": "변경 견적 보류", "message": "확인되지 않은 조건이 있어 저장하지 않았습니다.",
                    "items": list(new_request["items"].values()), "missing": [], "issues": standard.get("missing", []) + standard.get("unknown", []),
                    "quote": None, "logs": logs, "mode": mode, "ai_used": _ai_used(logs),
                    "planner_mode": _planner_mode(logs), "source_text": source_text}
        options = [_option("standard", "표준 조건", standard)]
        if not standard["meets_requested"]:
            candidate = next((a for a in data["alternatives"] if a["snapshot_id"] == snap["snapshot_id"] and a["item_code"] == "P01" and a["supported_quantity"] == new_request["items"]["P01"]["quantity"]), None)
            if candidate:
                choice = decide(mode, {"try_alternative", "handoff"}, {"standard_delivery": standard["delivery_days"],
                                  "wanted_delivery": new_request["wanted_delivery_days"],
                                  "goal": "meet_requested_delivery_if_confirmed_candidate_passes_revalidation",
                                  "allowed_alternatives": [candidate["alternative_id"]],
                                  "candidate_evaluated": False}, "try_alternative", logs)
                if choice == "handoff":
                    # Handoff is a rejected proposal here, not a successful AI step.
                    logs[-1].update(success=False, fallback=True, rejected_action="handoff",
                                    rejection="confirmed_candidate_not_yet_evaluated")
                    logs.append(_log("evaluate_plan", "목표 미달 경로이며 허용 대안이 아직 검증되지 않아 1회 재계획", "replan"))
                    choice = decide(mode, {"try_alternative"}, {"goal": "meet_requested_delivery_if_candidate_passes_revalidation",
                                    "standard_delivery": standard["delivery_days"], "wanted_delivery": new_request["wanted_delivery_days"],
                                    "previous_action": "handoff", "reason_for_replan": "confirmed_candidate_not_yet_evaluated",
                                    "alternative": candidate["alternative_id"]}, "try_alternative", logs)
                if choice == "try_alternative":
                    alt = _calculate(new_request, snap, day, alternative=True, data=data)
                    logs.append(_log("calculate_alternative", "허용된 긴급 소재·외주 대안을 재계산하고 납기 재검증"))
                    if not alt.get("unknown") and alt["meets_requested"]:
                        options.append(_option("rush", "긴급 소재·외주 대안", alt))
                    else:
                        logs.append(_log("verify_alternative", "대안이 조건을 충족하지 못함", "blocked"))
        quote = {"options": options, "changes": changes,
                 "approval": {"status": "pending", "option_id": ""}, "logs": logs,
                 "valid_until_day": day + data["rules"]["quote_valid_business_days"], "data_kind": "fictional_demo",
                 "data_fingerprint": _data_fingerprint(data),
                 "ai_used": _ai_used(logs), "planner_mode": _planner_mode(logs)}
        version = old["version"] + 1
        new_id = _save(db, quote_id, version, day, snap["snapshot_id"], new_request, quote, payload.get("fault"),
                       expected_latest_version=old["version"], expected_data_fingerprint=quote["data_fingerprint"])
        quote["logs"] = logs
        return _run_result("approval_pending", "변경 견적 저장", "대안별 가격·납기를 검증했습니다. 고객 합의와 담당자 승인이 필요합니다.",
                list(new_request["items"].values()), [], quote, logs, mode, source_text)
    except sqlite3.OperationalError as exc:
        if "injected demo save failure" in str(exc):
            title = "저장 실패"
            if mode == "ai" and not _ai_used(logs): title = "규칙 경로 사용 — AI 판단 미검증 · " + title
            return {"status": "error", "title": title, "message": "DB 저장에 실패해 완료로 처리하지 않았습니다.",
                    "items": [], "missing": [], "quote": None, "logs": logs + [_log("save", "transaction rollback", "error")], "mode": mode,
                    "ai_used": _ai_used(logs), "planner_mode": _planner_mode(logs), "source_text": source_text}
        raise AppError("저장소 오류가 발생했습니다. 완료 상태로 처리하지 않았습니다.", 500) from exc
    except sqlite3.Error as exc:
        raise AppError("견적 또는 승인 저장 검증에 실패했습니다. 완료 상태로 처리하지 않았습니다.", 500) from exc
    finally:
        db.close()


def _option(option_id: str, label: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": option_id, "label": label, "total_krw": result["total_krw"], "cost_krw": result["cost_krw"],
            "delivery_days": result["delivery_days"], "meets_requested": result["meets_requested"],
            "lines": result["lines"], "assumptions": sorted({a for line in result["lines"] for a in line["assumptions"]})}


def _ai_used(logs: list[dict[str, Any]]) -> bool:
    decisions = [x for x in logs if x.get("type") == "model_decision"]
    return bool(decisions) and all(x.get("success") and not x.get("fallback") for x in decisions)


def _planner_mode(logs: list[dict[str, Any]]) -> str:
    decisions = [x for x in logs if x.get("type") == "model_decision"]
    if not decisions or not any(x.get("provider") == "ollama-local" for x in decisions):
        return "rules" if decisions else "none"
    successes = sum(bool(x.get("success") and not x.get("fallback")) for x in decisions)
    if successes == len(decisions):
        return "ai"
    return "mixed" if successes else "rules_fallback"


def _run_result(status: str, title: str, message: str, items: list[dict[str, Any]],
                missing: list[str], quote: dict[str, Any], logs: list[dict[str, Any]],
                mode: str, source_text: str) -> dict[str, Any]:
    ai_used = _ai_used(logs)
    if mode == "ai" and not ai_used:
        title = "규칙 경로 사용 — AI 판단 미검증 · " + title
    quote["ai_used"], quote["planner_mode"] = ai_used, _planner_mode(logs)
    return {"status": status, "title": title, "message": message, "items": items, "missing": missing,
            "quote": quote, "logs": logs, "mode": mode, "ai_used": ai_used,
            "planner_mode": _planner_mode(logs), "source_text": source_text}


def _view(row: sqlite3.Row, db: sqlite3.Connection, current_day: int,
          current_data_fingerprint: str | None = None) -> dict[str, Any]:
    quote = json.loads(row["quote_json"])
    approval = db.execute("SELECT * FROM approvals WHERE quote_id=? AND version=?", (row["quote_id"], row["version"])).fetchone()
    status = approval["status"] if approval else "pending"
    latest_version = db.execute("SELECT MAX(version) FROM quotes WHERE quote_id=?", (row["quote_id"],)).fetchone()[0]
    if row["version"] < latest_version:
        status = "invalidated"
    elif (quote.get("data_fingerprint") and current_data_fingerprint
          and quote["data_fingerprint"] != current_data_fingerprint):
        status = "stale"
    elif current_day >= quote["valid_until_day"]:
        status = "stale" if status in ("approved", "stale") else "expired"
    quote["approval"] = {"status": status, "option_id": approval["option_id"] if approval else ""}
    return quote


def state(db_path: Path | str | None = None) -> dict[str, Any]:
    db = connect(db_path)
    try:
        day = get_day(db)
        source_fingerprint = _data_fingerprint(load_data())
        quotes = [_view(row, db, day, source_fingerprint) for row in db.execute("SELECT * FROM quotes ORDER BY quote_id,version")]
        text_rfq = (ROOT / "시연자료" / "01_견적요청.txt").read_text(encoding="utf-8")
        text_order = (ROOT / "시연자료" / "02_발주변경요청.txt").read_text(encoding="utf-8")
        return {"quotes": quotes, "day": day, "ai": {"available": _ollama_available(), "model": MODEL},
                "samples": {"rfq": text_rfq, "order": text_order}, "data_kind": "fictional_demo"}
    finally:
        db.close()


def _ollama_available() -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1) as res:
            models = json.loads(res.read(256 * 1024)).get("models", [])
        return any(m.get("name") == MODEL or m.get("model") == MODEL for m in models)
    except Exception:
        return False


def approve(payload: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("승인 요청은 JSON 객체여야 합니다.")
    db = connect(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        quote_id, option_id, day = payload.get("quote_id"), payload.get("option_id"), payload.get("day")
        if (not isinstance(quote_id, int) or isinstance(quote_id, bool)
                or not 1 <= quote_id <= 9_223_372_036_854_775_807 or option_id not in ("standard", "rush")):
            raise AppError("견적 또는 선택한 조건이 올바르지 않습니다.")
        current_day = get_day(db)
        if isinstance(day, bool) or not isinstance(day, int) or day != current_day:
            raise AppError("화면의 시뮬레이션 날짜가 현재 날짜와 다릅니다. 새로고침하세요.", 409)
        row = _latest(db, quote_id)
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1 or expected_version != row["version"]:
            raise AppError("새 견적 버전이 있습니다. 최신 버전을 확인하세요.", 409)
        quote = json.loads(row["quote_json"])
        request = json.loads(row["request_json"])
        data = load_data()
        snap = _snapshot(data, current_day)
        if quote.get("data_fingerprint") and quote["data_fingerprint"] != _data_fingerprint(data):
            raise AppError("견적 기준 회사 자료가 바뀌었습니다. 재검증이 필요합니다.", 409)
        if current_day >= quote["valid_until_day"] or snap["snapshot_id"] != quote["snapshot_id"]:
            raise AppError("견적 또는 조회 자료가 만료되었습니다. 재검증이 필요합니다.", 409)
        option = next((o for o in quote["options"] if o["id"] == option_id), None)
        if not option:
            raise AppError("해당 대안은 이 견적 버전에 없습니다.", 409)
        recomputed = _calculate(request, snap, current_day, alternative=option_id == "rush", data=data)
        if recomputed.get("unknown") or _option(option_id, option["label"], recomputed) != option:
            raise AppError("저장 견적과 재계산 결과가 다릅니다. 재검증이 필요합니다.", 409)
        agreed = payload.get("customer_delivery_agreed") is True
        if not option["meets_requested"] and not agreed:
            raise AppError("희망 납기보다 늦은 조건은 고객 납기 변경 합의 후 승인할 수 있습니다.", 409)
        approval = db.execute("SELECT * FROM approvals WHERE quote_id=? AND version=?", (quote_id, row["version"])).fetchone()
        if not approval or approval["fingerprint"] != _fingerprint(quote):
            raise AppError("승인 대상의 저장 검증 정보가 일치하지 않습니다.", 409)
        if approval["status"] != "pending":
            raise AppError("이 견적 버전은 이미 승인되었거나 무효화되었습니다. 새 버전을 검토하세요.", 409)
        with db:
            db.execute("UPDATE approvals SET option_id=?,status='approved',agreed=?,approved_at=? WHERE quote_id=? AND version=?",
                       (option_id, int(agreed), now(), quote_id, row["version"]))
        return {"status": "approved", "quote": _view(_latest(db, quote_id), db, current_day), "message": "내부 승인을 기록했습니다. 외부 발송은 실행하지 않았습니다."}
    finally:
        db.close()


def advance_clock(payload: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("시뮬레이션 시계 요청은 JSON 객체여야 합니다.")
    day = payload.get("day")
    if isinstance(day, bool) or not isinstance(day, int) or day not in (0, 4, 7):
        raise AppError("day는 0, 4 또는 7이어야 합니다.")
    db = connect(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        current = get_day(db)
        if day < current:
            raise AppError("시뮬레이션 시계는 되돌릴 수 없습니다.", 409)
        with db:
            db.execute("UPDATE settings SET value=? WHERE key='day'", (str(day),))
            for row in db.execute("SELECT * FROM quotes"):
                quote = json.loads(row["quote_json"])
                if day >= quote["valid_until_day"]:
                    db.execute("UPDATE approvals SET status='stale' WHERE quote_id=? AND version=? AND status='approved'",
                               (row["quote_id"], row["version"]))
        return {"status": "ok", "day": day}
    finally:
        db.close()


def export_quote(quote_id: int, version: int | None = None, db_path: Path | str | None = None) -> dict[str, Any]:
    db = connect(db_path)
    try:
        if isinstance(quote_id, bool) or not isinstance(quote_id, int) or not 1 <= quote_id <= 9_223_372_036_854_775_807:
            raise AppError("quote_id 형식이 올바르지 않습니다.")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise AppError("version은 1 이상의 정수여야 합니다.")
        if version is not None and version > 9_223_372_036_854_775_807:
            raise AppError("version 값이 너무 큽니다.")
        if version is None:
            rows = db.execute("SELECT * FROM quotes WHERE quote_id=? ORDER BY version", (quote_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM quotes WHERE quote_id=? AND version=?", (quote_id, version)).fetchall()
        if not rows:
            raise AppError("견적을 찾을 수 없습니다.", 404)
        day = get_day(db)
        return {"data_kind": "fictional_demo", "history": [_view(row, db, day) for row in rows]}
    finally:
        db.close()
