import tempfile
import unittest
from pathlib import Path

import server


class SignalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.db"
        server.initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_scores_and_sorts_signals(self):
        items = server.signals(self.db)
        self.assertEqual(4, len(items))
        self.assertEqual("4589", items[0]["code"])
        self.assertEqual("high", items[0]["urgency"])
        self.assertIn("現預金ランウェイが6か月未満", items[0]["reasons"])

    def test_filters_by_urgency_and_query(self):
        self.assertTrue(all(x["urgency"] == "mid" for x in server.signals(self.db, urgency="mid")))
        self.assertEqual(["5124"], [x["code"] for x in server.signals(self.db, query="医療")])


if __name__ == "__main__":
    unittest.main()
