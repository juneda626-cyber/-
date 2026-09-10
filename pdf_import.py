"""Extract reviewable candidates from text-based Japanese earnings PDFs."""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from edinet import create_tables
from server import DB_PATH, _real_signals, connect

UPLOAD_ROOT = DB_PATH.parent / "uploads"
MAX_PDF_BYTES = 20 * 1024 * 1024
METRICS = {
    "cash": ("現金及び預金", "現金及び現金同等物の期末残高"),
    "operating_cf": ("営業活動によるキャッシュ・フロー", "営業活動によるキャッシュフロー"),
    "equity": ("自己資本", "純資産合計"),
    "assets": ("総資産", "資産合計"),
    "interest_debt": ("有利子負債",),
}
UNIT_MULTIPLIERS = {"円": 1, "千円": 1_000, "百万円": 1_000_000, "億円": 100_000_000}


def extract_pdf(path: Path) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("pypdfがありません。`py -m pip install -r requirements.txt`を実行してください。") from error
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("暗号化されたPDFには対応していません。")
    pages = [(page.extract_text() or "") for page in reader.pages]
    if not any(text.strip() for text in pages):
        raise ValueError("文字を抽出できません。画像だけのPDF（OCRが必要なPDF）は非対応です。")
    return extract_candidates(pages)


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _number(line: str) -> float | None:
    values = re.findall(r"[△▲\-−]?\s*[0-9０-９][0-9０-９,，]*", line)
    if not values:
        return None
    raw = values[-1].translate(str.maketrans("０１２３４５６７８９，−", "0123456789,-"))
    negative = raw.lstrip().startswith(("△", "▲", "-"))
    digits = re.sub(r"[^0-9]", "", raw)
    return (-1 if negative else 1) * float(digits) if digits else None


def extract_candidates(pages: list[str]) -> dict:
    all_text = "\n".join(pages)
    unit = _first(r"(?:単位\s*[:：]\s*|（)(円|千円|百万円|億円)", all_text)
    result = {
        "company_name": _first(r"会社名\s*[:：]?\s*([^\n]+)", all_text),
        "security_code": _first(r"コード番号\s*[:：]?\s*([0-9]{4})", all_text),
        "fiscal_period": _first(r"([0-9０-９]{4}年\s*[0-9０-９]{1,2}月期)", all_text),
        "scope": "consolidated" if "連結決算" in all_text or "連結財務" in all_text else
                 "standalone" if "非連結" in all_text or "個別財務" in all_text else None,
        "period_start": None, "period_end": None, "unit": unit,
        "metrics": {key: {"value": None, "page": None, "unit": unit} for key in METRICS},
    }
    for page_number, text in enumerate(pages, 1):
        for line in text.splitlines():
            normalized = re.sub(r"\s+", "", line)
            for metric, labels in METRICS.items():
                candidate = result["metrics"][metric]
                if candidate["value"] is None and any(label in normalized for label in labels):
                    candidate.update(value=_number(line), page=page_number)
    return result


def stage_pdf(pdf: bytes) -> tuple[str, Path]:
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("PDFファイルではありません。")
    if len(pdf) > MAX_PDF_BYTES:
        raise ValueError("PDFは20MB以下にしてください。")
    token = hashlib.sha256(pdf).hexdigest()
    pending = UPLOAD_ROOT / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{token}.pdf"
    path.write_bytes(pdf)
    return token, path


def save_confirmed(payload: dict, path: Path = DB_PATH) -> dict:
    token = str(payload.get("token", ""))
    pending = UPLOAD_ROOT / "pending" / f"{token}.pdf"
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not pending.exists():
        raise ValueError("アップロード済みPDFを確認できません。再度アップロードしてください。")
    code = str(payload.get("security_code", "")).strip()
    name = str(payload.get("company_name", "")).strip()
    if not re.fullmatch(r"[0-9]{4}", code) or not name:
        raise ValueError("企業名と4桁の証券コードは必須です。")
    scope = payload.get("scope")
    if scope not in ("consolidated", "standalone"):
        raise ValueError("連結・単体区分を選択してください。")
    unit = payload.get("unit")
    if unit not in UNIT_MULTIPLIERS:
        raise ValueError("単位を選択してください。")
    doc_id = f"PDF-{token[:16]}"
    acquired = datetime.now(timezone.utc).isoformat()
    source_url = str(payload.get("source_url") or f"/api/uploads/{doc_id}/pdf")
    filing = {"doc_id": doc_id, "edinet_code": f"UPLOAD:{code}", "security_code": code,
              "company_name": name, "doc_type_code": "EARNINGS_PDF",
              "period_start": payload.get("period_start") or None, "period_end": payload.get("period_end") or None,
              "submitted_at": acquired, "source_url": source_url, "imported_at": acquired}
    rows = []
    for metric, candidate in (payload.get("metrics") or {}).items():
        if metric not in METRICS or candidate.get("value") in (None, ""):
            continue
        value = float(candidate["value"]) * UNIT_MULTIPLIERS[unit]
        rows.append({"metric": metric, "value": value, "unit": "JPY",
                     "period_start": filing["period_start"] if metric == "operating_cf" else None,
                     "period_end": filing["period_end"], "context_id": f"confirmed-page-{candidate.get('page') or 'unknown'}",
                     "scope": scope, "concept": f"ConfirmedFromPDF:{metric}",
                     "source_url": source_url, "acquired_at": acquired,
                     "source_page": int(candidate["page"]) if candidate.get("page") else None})
    create_tables(path)
    final_dir = UPLOAD_ROOT / "confirmed"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / f"{doc_id}.pdf"
    shutil.move(pending, final_path)
    with connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS uploaded_documents
          (doc_id TEXT PRIMARY KEY, file_path TEXT NOT NULL, original_name TEXT, sha256 TEXT NOT NULL)""")
        db.execute("INSERT OR REPLACE INTO edinet_filings VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(filing.values()))
        db.execute("DELETE FROM financial_observations WHERE doc_id=?", (doc_id,))
        db.executemany("""INSERT INTO financial_observations
          (doc_id,metric,value,unit,period_start,period_end,context_id,scope,concept,source_url,acquired_at,source_page)
          VALUES (:doc_id,:metric,:value,:unit,:period_start,:period_end,:context_id,:scope,:concept,:source_url,:acquired_at,:source_page)""",
          [dict(row, doc_id=doc_id) for row in rows])
        db.execute("INSERT OR REPLACE INTO uploaded_documents VALUES (?,?,?,?)",
                   (doc_id, str(final_path), payload.get("original_name"), token))
    result = next((item for item in _real_signals(path) if item["doc_id"] == doc_id), None)
    return {"doc_id": doc_id, "saved_metrics": len(rows), "analysis": result}
