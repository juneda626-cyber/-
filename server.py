#!/usr/bin/env python3
"""FLUXIA Phase 1 MVP: static file server and financial-signal API."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
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


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
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
        filings = db.execute("SELECT * FROM edinet_filings ORDER BY submitted_at DESC").fetchall()
        output = []
        for filing in filings:
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
            cash, operating = latest("cash"), distinct_periods("operating_cf")
            equity, assets = distinct_periods("equity"), distinct_periods("assets")
            current_cf = operating[0] if operating else None
            prior_cf = operating[1] if len(operating) > 1 else None
            cf_change = ((current_cf["value"] - prior_cf["value"]) / abs(prior_cf["value"]) * 100
                         if current_cf and prior_cf and prior_cf["value"] else None)
            burn = max(0.0, -current_cf["value"] / 12) if current_cf else None
            runway = cash["value"] / burn if cash and burn else None
            equity_ratio = equity[0]["value"] / assets[0]["value"] * 100 if equity and assets and assets[0]["value"] else None
            prior_equity_ratio = (equity[1]["value"] / assets[1]["value"] * 100
                                  if len(equity) > 1 and len(assets) > 1 and assets[1]["value"] else None)
            debt_parts = [latest(x) for x in ("short_term_loans", "current_long_term_loans", "long_term_loans", "bonds")]
            debt = sum(x["value"] for x in debt_parts if x) if any(debt_parts) else None
            points, reasons, missing = 0, [], []
            if runway is None: missing.append("現預金ランウェイ")
            elif runway < 6: points += 60; reasons.append("現預金ランウェイが6か月未満")
            elif runway < 12: points += 35; reasons.append("現預金ランウェイが12か月未満")
            if cf_change is None: missing.append("営業CF前年比")
            elif cf_change < -15: points += 25; reasons.append("営業CFが前年比15%以上悪化")
            if equity_ratio is None or prior_equity_ratio is None: missing.append("自己資本比率前年比")
            elif equity_ratio < prior_equity_ratio: points += 15; reasons.append("自己資本比率が前期比で低下")
            status = "undetermined" if missing else ("high" if points >= 60 else "mid" if points >= 35 else "low")
            source_rows = [x for x in [cash, current_cf, prior_cf, *(equity[:2]), *(assets[:2]), *debt_parts] if x]
            output.append({"code": filing["security_code"], "name": filing["company_name"], "market": "EDINET",
              "business": "EDINET提出書類から取得", "filing_date": filing["period_end"], "doc_id": filing["doc_id"],
              "data_kind": "actual", "analysis_status": status, "urgency": status, "score": None if missing else points,
              "cash_million": round(cash["value"] / 1_000_000, 1) if cash else None,
              "operating_cf_million": round(current_cf["value"] / 1_000_000, 1) if current_cf else None,
              "runway_months": round(runway, 1) if runway is not None else None,
              "cf_change_percent": round(cf_change, 1) if cf_change is not None else None,
              "equity_ratio": round(equity_ratio, 1) if equity_ratio is not None else None,
              "interest_debt_million": round(debt / 1_000_000, 1) if debt is not None else None,
              "reasons": reasons, "missing": missing,
              "sources": [{k: r[k] for k in ("metric", "value", "unit", "period_start", "period_end", "scope", "concept", "source_url", "acquired_at")} | {"doc_id": filing["doc_id"]} for r in source_rows]})
        return output


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
    return sorted(result, key=lambda item: (item["data_kind"] != "actual", -(item["score"] or -1), item["runway_months"] or 9999))


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
        return super().do_GET()

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
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
