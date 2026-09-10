#!/usr/bin/env python3
"""Import one issuer's official EDINET XBRL filing with field-level provenance."""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from server import DB_PATH, _real_signals, connect, initialize

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
TARGETS = {"e-logit": {"name": "株式会社イー・ロジット", "code": "9327", "edinet_code": "E36405"}}
DOC_TYPES = {"120", "130", "140", "150", "160"}
CONCEPTS = {
    "cash": {"CashAndDeposits", "CashAndCashEquivalents"},
    "operating_cf": {"NetCashProvidedByUsedInOperatingActivities"},
    "equity": {"Equity", "NetAssets", "TotalStockholdersEquity"},
    "assets": {"Assets"},
    "short_term_loans": {"ShortTermLoansPayable"},
    "current_long_term_loans": {"CurrentPortionOfLongTermLoansPayable"},
    "long_term_loans": {"LongTermLoansPayable"},
    "bonds": {"BondsPayable"},
}


def create_tables(path: Path = DB_PATH) -> None:
    initialize(path)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS edinet_filings (
          doc_id TEXT PRIMARY KEY, edinet_code TEXT NOT NULL, security_code TEXT,
          company_name TEXT NOT NULL, doc_type_code TEXT NOT NULL,
          period_start TEXT, period_end TEXT, submitted_at TEXT, source_url TEXT NOT NULL,
          imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS financial_observations (
          id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, metric TEXT NOT NULL,
          value REAL NOT NULL, unit TEXT NOT NULL, period_start TEXT, period_end TEXT,
          context_id TEXT NOT NULL, scope TEXT NOT NULL, concept TEXT NOT NULL,
          source_url TEXT NOT NULL, acquired_at TEXT NOT NULL,
          UNIQUE(doc_id, metric, context_id, concept),
          FOREIGN KEY(doc_id) REFERENCES edinet_filings(doc_id));
        """)


class EdinetClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("EDINET_API_KEY")
        if not self.api_key:
            raise RuntimeError("EDINET_API_KEY が未設定です。READMEの手順で環境変数を設定してください。")

    def _get(self, path: str, params: dict[str, str]) -> bytes:
        params["Subscription-Key"] = self.api_key
        url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "FLUXIA-MVP/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    def documents(self, day: date) -> list[dict]:
        payload = json.loads(self._get("documents.json", {"date": day.isoformat(), "type": "2"}))
        return payload.get("results") or []

    def xbrl(self, doc_id: str) -> bytes:
        return self._get(f"documents/{doc_id}", {"type": "1"})


def find_latest(client: EdinetClient, edinet_code: str, until: date, days: int) -> dict:
    for offset in range(days):
        candidates = [d for d in client.documents(until - timedelta(days=offset))
                      if d.get("edinetCode") == edinet_code and d.get("docTypeCode") in DOC_TYPES
                      and d.get("withdrawalStatus") != "1"]
        if candidates:
            return max(candidates, key=lambda d: d.get("submitDateTime") or "")
    raise LookupError(f"過去{days}日以内に対象のXBRL開示を確認できませんでした。")


def _contexts(root: ET.Element) -> dict[str, dict]:
    result = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "context":
            continue
        values = {x.tag.rsplit("}", 1)[-1]: (x.text or "").strip() for x in node.iter()}
        xml = ET.tostring(node, encoding="unicode")
        result[node.attrib["id"]] = {
            "start": values.get("startDate"), "end": values.get("endDate") or values.get("instant"),
            "scope": "standalone" if "NonConsolidatedMember" in xml else "consolidated",
            "dimensional": "explicitMember" in xml and "NonConsolidatedMember" not in xml,
        }
    return result


def parse_xbrl(archive: bytes, doc: dict, acquired_at: str) -> tuple[dict, list[dict]]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = [n for n in zf.namelist()
                 if n.lower().endswith(".xbrl") and "PublicDoc" in Path(n).parts]
        if not names:
            raise ValueError("取得ZIPのXBRL/PublicDocに本体XBRLがありません。")
        root = ET.fromstring(zf.read(sorted(names)[0]))
    contexts = _contexts(root)
    source_url = f"{BASE_URL}/documents/{doc['docID']}?type=1"
    rows = []
    for node in root.iter():
        concept = node.tag.rsplit("}", 1)[-1]
        metric = next((key for key, names in CONCEPTS.items() if concept in names), None)
        context_id = node.attrib.get("contextRef")
        if (not metric or not context_id or context_id not in contexts
                or contexts[context_id]["dimensional"] or node.text is None):
            continue
        try:
            value = float(node.text.replace(",", ""))
        except ValueError:
            continue
        context = contexts[context_id]
        rows.append({"metric": metric, "value": value, "unit": node.attrib.get("unitRef", "unknown"),
                     "period_start": context["start"], "period_end": context["end"],
                     "context_id": context_id, "scope": context["scope"], "concept": concept,
                     "source_url": source_url, "acquired_at": acquired_at})
    filing = {"doc_id": doc["docID"], "edinet_code": doc["edinetCode"],
              "security_code": (doc.get("secCode") or "")[:4], "company_name": doc.get("filerName") or "",
              "doc_type_code": doc["docTypeCode"], "period_start": doc.get("periodStart"),
              "period_end": doc.get("periodEnd"), "submitted_at": doc.get("submitDateTime"),
              "source_url": source_url, "imported_at": acquired_at}
    return filing, rows


def save(filing: dict, rows: list[dict], path: Path = DB_PATH) -> None:
    create_tables(path)
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO edinet_filings VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(filing.values()))
        db.execute("DELETE FROM financial_observations WHERE doc_id = ?", (filing["doc_id"],))
        db.executemany("""INSERT INTO financial_observations
          (doc_id,metric,value,unit,period_start,period_end,context_id,scope,concept,source_url,acquired_at)
          VALUES (:doc_id,:metric,:value,:unit,:period_start,:period_end,:context_id,:scope,:concept,:source_url,:acquired_at)""",
          [dict(row, doc_id=filing["doc_id"]) for row in rows])


def sync(target: str, until: date, days: int, path: Path = DB_PATH) -> dict:
    company = TARGETS[target]; client = EdinetClient()
    doc = find_latest(client, company["edinet_code"], until, days)
    acquired = datetime.now(timezone.utc).isoformat()
    filing, rows = parse_xbrl(client.xbrl(doc["docID"]), doc, acquired)
    save(filing, rows, path)
    actual = next((item for item in _real_signals(path) if item["doc_id"] == filing["doc_id"]), None)
    return {"filing": filing, "observations": len(rows), "analysis": actual}


def main() -> None:
    parser = argparse.ArgumentParser(description="EDINETから実データを取得してSQLiteへ保存")
    parser.add_argument("--target", choices=TARGETS, default="e-logit")
    parser.add_argument("--until", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    try:
        result = sync(args.target, args.until, args.days)
    except (RuntimeError, LookupError, OSError, ValueError) as error:
        parser.exit(1, f"取得できませんでした: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
