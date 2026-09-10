import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import edinet
import server


XBRL = b'''<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:jppfs="http://example.test/jppfs">
 <xbrli:context id="CurrentYearInstant"><xbrli:entity/><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="Prior1YearInstant"><xbrli:entity/><xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="CurrentYearDuration"><xbrli:entity/><xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="Prior1YearDuration"><xbrli:entity/><xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2024-09-30</xbrli:endDate></xbrli:period></xbrli:context>
 <jppfs:CashAndDeposits contextRef="CurrentYearInstant" unitRef="JPY">120000000</jppfs:CashAndDeposits>
 <jppfs:NetCashProvidedByUsedInOperatingActivities contextRef="CurrentYearDuration" unitRef="JPY">-240000000</jppfs:NetCashProvidedByUsedInOperatingActivities>
 <jppfs:NetCashProvidedByUsedInOperatingActivities contextRef="Prior1YearDuration" unitRef="JPY">-100000000</jppfs:NetCashProvidedByUsedInOperatingActivities>
 <jppfs:NetAssets contextRef="CurrentYearInstant" unitRef="JPY">300000000</jppfs:NetAssets>
 <jppfs:NetAssets contextRef="Prior1YearInstant" unitRef="JPY">250000000</jppfs:NetAssets>
 <jppfs:Assets contextRef="Prior1YearInstant" unitRef="JPY">1000000000</jppfs:Assets>
</xbrli:xbrl>'''


class EdinetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_parse_save_and_mark_incomplete_analysis(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("XBRL/AuditDoc/audit.xbrl", b"<audit />")
            archive.writestr("XBRL/PublicDoc/report.xbrl", XBRL)
        doc = {"docID": "S100TEST", "edinetCode": "E36405", "secCode": "93270",
               "filerName": "株式会社イー・ロジット", "docTypeCode": "120",
               "periodStart": "2025-04-01", "periodEnd": "2026-03-31",
               "submitDateTime": "2026-06-30 15:00"}
        filing, rows = edinet.parse_xbrl(stream.getvalue(), doc, "2026-09-10T00:00:00+00:00")
        edinet.save(filing, rows, self.db)
        actual = server._real_signals(self.db)[0]
        self.assertEqual("9327", actual["code"])
        self.assertEqual(3.0, actual["runway_months"])
        self.assertEqual("undetermined", actual["analysis_status"])
        self.assertIn("自己資本比率前年比", actual["missing"])
        self.assertIsNone(actual["equity_ratio"])
        self.assertIsNone(actual["interest_debt_million"])
        self.assertEqual("S100TEST", actual["sources"][0]["doc_id"])

    def test_company_list_uses_only_latest_filing(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("XBRL/PublicDoc/report.xbrl", XBRL)
        base = {"edinetCode": "E36405", "secCode": "93270", "filerName": "株式会社イー・ロジット",
                "docTypeCode": "120", "periodStart": "2025-04-01", "periodEnd": "2026-03-31"}
        older = dict(base, docID="S100OLD", submitDateTime="2026-06-29 15:00")
        newer = dict(base, docID="S100NEW", submitDateTime="2026-06-30 15:00")
        for doc in (older, newer):
            filing, rows = edinet.parse_xbrl(stream.getvalue(), doc, "2026-09-10T00:00:00+00:00")
            edinet.save(filing, rows, self.db)
        actual = server._real_signals(self.db)
        self.assertEqual(1, len(actual))
        self.assertEqual("S100NEW", actual[0]["doc_id"])

    def test_period_months_distinguishes_reporting_lengths(self):
        self.assertEqual(3.0, server._period_months("2025-04-01", "2025-06-30"))
        self.assertEqual(6.0, server._period_months("2025-04-01", "2025-09-30"))
        self.assertEqual(12.0, server._period_months("2025-04-01", "2026-03-31"))
        self.assertIsNone(server._period_months(None, "2026-03-31"))

    def test_equity_and_assets_must_have_same_period_and_scope(self):
        equity = {"period_start": None, "period_end": "2026-03-31", "scope": "consolidated", "value": 300}
        wrong_period = {"period_start": None, "period_end": "2025-03-31", "scope": "consolidated", "value": 1000}
        wrong_scope = {"period_start": None, "period_end": "2026-03-31", "scope": "standalone", "value": 1000}
        self.assertEqual([], server._paired_ratios([equity], [wrong_period, wrong_scope]))

    def test_api_key_is_required(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EDINET_API_KEY"):
                edinet.EdinetClient()


if __name__ == "__main__":
    unittest.main()
