#!/usr/bin/env python3
"""FLUXIA Phase 1 MVP: static file server and financial-signal API."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "fluxia.db"
SEED = [
    ("4589", "アステラバイオ株式会社", "東証グロース", 420, -1180, -790, 18.2, 27.5, 310, "創薬プラットフォーム", "2026-09-08"),
    ("7692", "株式会社ネクストウェーブ", "東証スタンダード", 760, -1340, -1080, 24.1, 30.8, 820, "小売DX・店舗支援", "2026-09-08"),
    ("9331", "グリーンループ株式会社", "東証グロース", 930, -1200, -1010, 31.4, 35.9, 440, "資源循環ソリューション", "2026-09-07"),
    ("5124", "株式会社メディクラウド", "東証グロース", 1450, -1500, -1370, 41.3, 43.0, 260, "医療データSaaS", "2026-09-06"),
]


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


def initialize(path: Path = DB_PATH) -> None:
    with connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS financials (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
            cash_million REAL NOT NULL, operating_cf_million REAL NOT NULL,
            prior_operating_cf_million REAL NOT NULL, equity_ratio REAL NOT NULL,
            prior_equity_ratio REAL NOT NULL, interest_debt_million REAL NOT NULL,
            business TEXT NOT NULL, filing_date TEXT NOT NULL)""")
        db.executemany("INSERT OR IGNORE INTO financials VALUES (?,?,?,?,?,?,?,?,?,?,?)", SEED)


def _real_signals(path: Path = DB_PATH) -> list[dict]:
    """Build conservative signals from imported EDINET observations; never impute values."""
    with connect(path) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edinet_filings'").fetchone()
        if not exists:
            return []
        filings = db.execute("SELECT * FROM edinet_filings ORDER BY submitted_at DESC, doc_id DESC").fetchall()
        output = []
        seen_companies = set()
        for filing in filings:
            company_key = filing["edinet_code"] or filing["security_code"]
            if company_key in seen_companies:
                continue
            seen_companies.add(company_key)
            rows = db.execute("SELECT * FROM financial_observations WHERE doc_id=? ORDER BY period_end DESC", (filing["doc_id"],)).fetchall()
            preferred = [r for r in rows if r["scope"] == "consolidated"] or rows
            by_metric = {}
            for row in preferred:
                by_metric.setdefault(row["metric"], []).append(row)
            def latest(metric):
                values = by_metric.get(metric, [])
                return values[0] if values else None
            def distinct_periods(metric):
                seen, values = set(), []
                for row in by_metric.get(metric, []):
                    key = (row["period_start"], row["period_end"])
                    if key not in seen:
                        seen.add(key); values.append(row)
                return values
            cash = next((row for row in by_metric.get("cash", [])
                         if row["period_end"] == filing["period_end"]), None)
            operating = distinct_periods("operating_cf")
            current_cf = operating[0] if operating else None
            prior_cf = next((row for row in operating[1:] if _is_prior_period(current_cf, row)), None)
            cf_change = ((current_cf["value"] - prior_cf["value"]) / abs(prior_cf["value"]) * 100
                         if current_cf and prior_cf and prior_cf["value"] else None)
            duration_months = _period_months(current_cf["period_start"], current_cf["period_end"]) if current_cf else None
            burn = max(0.0, -current_cf["value"] / duration_months) if current_cf and duration_months else None
            runway = cash["value"] / burn if cash and burn else None
            ratio_pairs = _paired_ratios(by_metric.get("equity", []), by_metric.get("assets", []))
            current_pair = next((pair for pair in ratio_pairs if pair["period_end"] == filing["period_end"]), None)
            prior_pairs = [pair for pair in ratio_pairs
                           if current_pair and (pair["period_end"] or "") < (current_pair["period_end"] or "")]
            equity_ratio = current_pair["ratio"] if current_pair else None
            prior_equity_ratio = prior_pairs[0]["ratio"] if prior_pairs else None
            direct_debt = latest("interest_debt")
            debt_parts = [latest(x) for x in ("short_term_loans", "current_long_term_loans", "long_term_loans", "bonds")]
            debt = direct_debt["value"] if direct_debt else (sum(x["value"] for x in debt_parts if x) if any(debt_parts) else None)
            points, reasons, missing = 0, [], []
            if current_cf and not duration_months: missing.append("営業CFの対象期間")
            if runway is None: missing.append("現預金ランウェイ")
            elif runway < 6: points += 60; reasons.append("現預金ランウェイが6か月未満")
            elif runway < 12: points += 35; reasons.append("現預金ランウェイが12か月未満")
            if cf_change is None: missing.append("営業CF前年比")
            elif cf_change < -15: points += 25; reasons.append("営業CFが前年比15%以上悪化")
            if equity_ratio is None or prior_equity_ratio is None: missing.append("自己資本比率前年比")
            elif equity_ratio < prior_equity_ratio: points += 15; reasons.append("自己資本比率が前期比で低下")
            status = "undetermined" if missing else ("high" if points >= 60 else "mid" if points >= 35 else "low")
            selected_pairs = [pair for pair in [current_pair, prior_pairs[0] if prior_pairs else None] if pair]
            ratio_rows = [row for pair in selected_pairs for row in (pair["equity"], pair["assets"])]
            source_rows = [x for x in [cash, current_cf, prior_cf, *ratio_rows, direct_debt, *debt_parts] if x]
            is_pdf = filing["doc_type_code"] == "EARNINGS_PDF"
            output.append({"code": filing["security_code"], "name": filing["company_name"], "market": "PDF取込" if is_pdf else "EDINET",
              "business": "確認済み決算短信PDF" if is_pdf else "EDINET提出書類から取得", "filing_date": filing["period_end"], "doc_id": filing["doc_id"],
              "data_kind": "verified_pdf" if is_pdf else "actual", "analysis_status": status, "urgency": status, "score": None if missing else points,
              "cash_million": round(cash["value"] / 1_000_000, 1) if cash else None,
              "operating_cf_million": round(current_cf["value"] / 1_000_000, 1) if current_cf else None,
              "runway_months": round(runway, 1) if runway is not None else None,
              "cf_change_percent": round(cf_change, 1) if cf_change is not None else None,
              "equity_ratio": round(equity_ratio, 1) if equity_ratio is not None else None,
              "interest_debt_million": round(debt / 1_000_000, 1) if debt is not None else None,
              "reasons": reasons, "missing": missing,
              "sources": [{k: r[k] for k in ("metric", "value", "unit", "period_start", "period_end", "scope", "concept", "source_url", "acquired_at")} | {"doc_id": filing["doc_id"], "source_page": r["source_page"] if "source_page" in r.keys() else None} for r in source_rows]})
        return output


def _period_months(start_text: str | None, end_text: str | None) -> float | None:
    """Return the filing period in months, respecting quarter/half/full-year durations."""
    if not start_text or not end_text:
        return None
    try:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        return None
    if end < start:
        return None
    exclusive_end = end + timedelta(days=1)
    calendar_months = (exclusive_end.year - start.year) * 12 + exclusive_end.month - start.month
    if exclusive_end.day == start.day and calendar_months > 0:
        return float(calendar_months)
    approximate = (end - start).days + 1
    months = approximate / (365.2425 / 12)
    return round(months, 2) if months > 0 else None


def _is_prior_period(current: sqlite3.Row | None, candidate: sqlite3.Row) -> bool:
    if not current:
        return False
    current_months = _period_months(current["period_start"], current["period_end"])
    candidate_months = _period_months(candidate["period_start"], candidate["period_end"])
    try:
        current_end = date.fromisoformat(current["period_end"])
        candidate_end = date.fromisoformat(candidate["period_end"])
    except (TypeError, ValueError):
        return False
    return (current_months == candidate_months
            and candidate_end.year == current_end.year - 1
            and (candidate_end.month, candidate_end.day) == (current_end.month, current_end.day))


def _paired_ratios(equity_rows: list[sqlite3.Row], asset_rows: list[sqlite3.Row]) -> list[dict]:
    """Pair equity/assets only where period and consolidation scope are identical."""
    def keyed(rows):
        result = {}
        for row in rows:
            key = (row["period_start"], row["period_end"], row["scope"])
            result.setdefault(key, row)
        return result
    equities, assets = keyed(equity_rows), keyed(asset_rows)
    pairs = []
    for key in sorted(equities.keys() & assets.keys(), key=lambda item: item[1] or "", reverse=True):
        equity, asset = equities[key], assets[key]
        if asset["value"]:
            pairs.append({"period_start": key[0], "period_end": key[1], "scope": key[2],
                          "ratio": equity["value"] / asset["value"] * 100,
                          "equity": equity, "assets": asset})
    return pairs


def score(row: sqlite3.Row) -> dict:
    burn = max(0.0, -row["operating_cf_million"] / 12)
    runway = None if burn == 0 else row["cash_million"] / burn
    cf_change = ((row["operating_cf_million"] - row["prior_operating_cf_million"])
                 / abs(row["prior_operating_cf_million"]) * 100) if row["prior_operating_cf_million"] else None
    points, reasons = 0, []
    if runway is not None and runway < 6:
        points += 60; reasons.append("現預金ランウェイが6か月未満")
    elif runway is not None and runway < 12:
        points += 35; reasons.append("現預金ランウェイが12か月未満")
    if cf_change is not None and cf_change < -15:
        points += 25; reasons.append("営業CFが前年比15%以上悪化")
    if row["equity_ratio"] < row["prior_equity_ratio"]:
        points += 15; reasons.append("自己資本比率が前期比で低下")
    urgency = "high" if points >= 60 else "mid" if points >= 35 else "low"
    return {"code": row["code"], "name": row["name"], "market": row["market"],
            "business": row["business"], "filing_date": row["filing_date"],
            "cash_million": row["cash_million"], "operating_cf_million": row["operating_cf_million"],
            "runway_months": round(runway, 1) if runway is not None else None,
            "cf_change_percent": round(cf_change, 1) if cf_change is not None else None,
            "equity_ratio": row["equity_ratio"], "interest_debt_million": row["interest_debt_million"],
            "score": points, "urgency": urgency, "reasons": reasons}


def signals(path: Path = DB_PATH, urgency: str | None = None, query: str = "") -> list[dict]:
    with connect(path) as db:
        result = [dict(score(row), data_kind="sample", analysis_status=score(row)["urgency"], missing=[], sources=[]) for row in db.execute("SELECT * FROM financials")]
    result = _real_signals(path) + result
    if urgency:
        result = [item for item in result if item["urgency"] == urgency]
    if query:
        q = query.casefold()
        result = [item for item in result if q in (item["name"] + item["code"] + item["business"]).casefold()]
    return sorted(result, key=lambda item: (item["data_kind"] == "sample", -(item["score"] or -1), item["runway_months"] or 9999))


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            actual_count = len(_real_signals())
            return self.send_json({"status": "ok", "mode": "mixed" if actual_count else "sample",
                                   "actual_companies": actual_count})
        if parsed.path == "/api/methodology":
            return self.send_json({"purpose": "調査・確認対象の優先順位付け（増資の必要性を断定しない）",
              "rules": [{"condition": "ランウェイ < 6か月", "points": 60},
                        {"condition": "6か月以上12か月未満", "points": 35},
                        {"condition": "営業CFが前年比15%以上悪化", "points": 25},
                        {"condition": "自己資本比率が前期比低下", "points": 15}],
              "thresholds": {"high": 60, "mid": 35},
              "missing_data": "必要値が不足する場合はゼロ補完せず判定不可"})
        if parsed.path == "/api/signals":
            params = parse_qs(parsed.query)
            urgency = params.get("urgency", [None])[0]
            if urgency not in (None, "high", "mid", "low", "undetermined"):
                return self.send_json({"error": "urgency must be high, mid, low, or undetermined"}, HTTPStatus.BAD_REQUEST)
            items = signals(urgency=urgency, query=params.get("q", [""])[0])
            return self.send_json({"data": items, "count": len(items), "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "sample"})
        if parsed.path.startswith("/api/companies/"):
            code = parsed.path.rsplit("/", 1)[-1]
            items = [item for item in signals() if item["code"] == code]
            return self.send_json(items[0] if items else {"error": "company not found"}, HTTPStatus.OK if items else HTTPStatus.NOT_FOUND)
        if parsed.path.startswith("/api/uploads/") and parsed.path.endswith("/pdf"):
            doc_id = parsed.path.split("/")[3]
            with connect() as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uploaded_documents'").fetchone()
                row = db.execute("SELECT file_path FROM uploaded_documents WHERE doc_id=?", (doc_id,)).fetchone() if exists else None
            if not row or not Path(row["file_path"]).is_file():
                return self.send_json({"error": "PDF not found"}, HTTPStatus.NOT_FOUND)
            data = Path(row["file_path"]).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        return super().do_GET()

    def do_POST(self) -> None:
        from pdf_import import MAX_PDF_BYTES, extract_pdf, save_confirmed, stage_pdf
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_PDF_BYTES + 100_000:
                raise ValueError("アップロードは20MB以下にしてください。")
            body = self.rfile.read(length)
            if parsed.path == "/api/pdf/extract":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise ValueError("multipart/form-dataでPDFを送信してください。")
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
                part = next((item for item in message.iter_attachments() if item.get_param("name", header="content-disposition") == "pdf"), None)
                if not part:
                    raise ValueError("PDFファイルがありません。")
                pdf = part.get_payload(decode=True)
                token, path = stage_pdf(pdf)
                candidates = extract_pdf(path)
                return self.send_json({"token": token, "original_name": part.get_filename(), "candidates": candidates})
            if parsed.path == "/api/pdf/confirm":
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError("application/jsonで確定値を送信してください。")
                return self.send_json(save_confirmed(json.loads(body)))
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def translate_path(self, path: str) -> str:
        relative = Path(super().translate_path(path)).relative_to(Path.cwd())
        return str(ROOT / relative)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--init-only", action="store_true")
    args = parser.parse_args(); initialize()
    if args.init_only:
        print(f"Initialized {DB_PATH}"); return
    print(f"FLUXIA MVP: http://localhost:{args.port} (sample data mode)")
    try:
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nFLUXIAを停止しました。")


if __name__ == "__main__":
    main()
