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
 <xbrli:context id="CurrentYearDuration"><xbrli:entity/><xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="Prior1YearDuration"><xbrli:entity/><xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <jppfs:CashAndDeposits contextRef="CurrentYearInstant" unitRef="JPY">120000000</jppfs:CashAndDeposits>
 <jppfs:NetCashProvidedByUsedInOperatingActivities contextRef="CurrentYearDuration" unitRef="JPY">-240000000</jppfs:NetCashProvidedByUsedInOperatingActivities>
 <jppfs:NetCashProvidedByUsedInOperatingActivities contextRef="Prior1YearDuration" unitRef="JPY">-100000000</jppfs:NetCashProvidedByUsedInOperatingActivities>
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
            archive.writestr("XBRL/PublicDoc/report.xbrl", XBRL)
        doc = {"docID": "S100TEST", "edinetCode": "E36405", "secCode": "93270",
               "filerName": "株式会社イー・ロジット", "docTypeCode": "120",
               "periodStart": "2025-04-01", "periodEnd": "2026-03-31",
               "submitDateTime": "2026-06-30 15:00"}
        filing, rows = edinet.parse_xbrl(stream.getvalue(), doc, "2026-09-10T00:00:00+00:00")
        edinet.save(filing, rows, self.db)
        actual = server._real_signals(self.db)[0]
        self.assertEqual("9327", actual["code"])
        self.assertEqual(6.0, actual["runway_months"])
        self.assertEqual("undetermined", actual["analysis_status"])
        self.assertIn("自己資本比率前年比", actual["missing"])
        self.assertIsNone(actual["interest_debt_million"])
        self.assertEqual("S100TEST", actual["sources"][0]["doc_id"])

    def test_api_key_is_required(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EDINET_API_KEY"):
                edinet.EdinetClient()


if __name__ == "__main__":
    unittest.main()
