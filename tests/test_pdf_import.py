import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pdf_import
import server


class PdfImportTest(unittest.TestCase):
    def test_extracts_candidates_and_keeps_missing_fields_empty(self):
        pages = ["2025年3月期 連結決算短信\n会社名 株式会社テスト物流\nコード番号 1234\n（単位：百万円）",
                 "総資産 1,500\n自己資本 420\n営業活動によるキャッシュ・フロー △120"]
        result = pdf_import.extract_candidates(pages)
        self.assertEqual("株式会社テスト物流", result["company_name"])
        self.assertEqual("1234", result["security_code"])
        self.assertEqual("百万円", result["unit"])
        self.assertEqual(-120, result["metrics"]["operating_cf"]["value"])
        self.assertEqual(2, result["metrics"]["operating_cf"]["page"])
        self.assertIsNone(result["metrics"]["cash"]["value"])

    def test_saves_confirmed_values_pdf_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            db_path = Path(directory) / "fluxia.db"
            with mock.patch.object(pdf_import, "UPLOAD_ROOT", root):
                token, _ = pdf_import.stage_pdf(b"%PDF-1.4\ntest")
                payload = {"token": token, "original_name": "results.pdf", "company_name": "株式会社テスト物流",
                           "security_code": "1234", "scope": "consolidated", "unit": "百万円",
                           "period_start": "2024-04-01", "period_end": "2025-03-31",
                           "source_url": "https://example.test/ir/results.pdf",
                           "metrics": {"cash": {"value": 100, "page": 5},
                                       "operating_cf": {"value": -240, "page": 8},
                                       "equity": {"value": 300, "page": 5},
                                       "assets": {"value": 1000, "page": 5},
                                       "interest_debt": {"value": None, "page": None}}}
                saved = pdf_import.save_confirmed(payload, db_path)
                self.assertEqual(4, saved["saved_metrics"])
                self.assertEqual("verified_pdf", saved["analysis"]["data_kind"])
                self.assertEqual("undetermined", saved["analysis"]["analysis_status"])
                self.assertIsNone(saved["analysis"]["interest_debt_million"])
                cash_source = next(x for x in saved["analysis"]["sources"] if x["metric"] == "cash")
                self.assertEqual(5, cash_source["source_page"])
                self.assertEqual("https://example.test/ir/results.pdf", cash_source["source_url"])
                self.assertTrue((root / "confirmed" / f"{saved['doc_id']}.pdf").is_file())


if __name__ == "__main__":
    unittest.main()
