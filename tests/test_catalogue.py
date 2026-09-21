import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from orion.catalogue import normalize, expiry_date
from orion.core import IST, resolve
from orion.subscriptions import Subscriptions


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nse.csv"
        self.now = datetime(2026, 9, 22, 10, tzinfo=IST)
        self.profile = {
            "verified_on": "2026-09-22",
            "source": "TEST ONLY",
            "products": {
                "NIFTY": {
                    "expiry_time_ist": "15:30:00",
                    "verified_lot_sizes": [65],
                    "price_units": "",
                    "delivery_units": "",
                    "premium_rupees_per_quantity_point": "1",
                }
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, token, day=22, strike=22500):
        # Encode the date using the same offset as Kotak's search implementation.
        from datetime import timezone

        expiry = int(datetime(2026, 9, day, 8, 30, tzinfo=timezone.utc).timestamp()) - 315511200
        return {
            "pSymbolName": "NIFTY",
            "pOptionType": "CE",
            "pExchSeg": "nse_fo",
            "pExpiryDate": str(expiry),
            "lLotSize": "65",
            "iLotSize": "65",
            "dStrikePrice;": str(strike * 100),
            "dTickSize ": "5",
            "lPrecision": "2",
            "pPriceUnits": "",
            "pDeliveryUnits": "",
            "pSymbol": str(token),
            "pTrdSymbol": f"TEST-{token}",
        }

    def build(self, rows):
        with self.path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        Path(str(self.path) + ".receipt.json").write_text(
            json.dumps({"as_of": "2026-09-22", "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        )
        return normalize({"nse_fo": self.path}, self.profile, self.now)

    def test_full_catalogue_explicit_and_missing_expiry(self):
        rows = [self.row(i, strike=22000 + i * 50) for i in range(101)]
        rows += [self.row(999, day=29, strike=22500)]
        master = self.build(rows)
        self.assertEqual(len(master["contracts"]), 102)
        signal = {"product": "NIFTY", "strike": "22500", "option_type": "CE", "expiry": None}
        self.assertEqual(resolve(signal, master, self.now)["expiry"], "2026-09-22")
        signal["expiry"] = "2026-09-29"
        self.assertEqual(resolve(signal, master, self.now)["token"], "999")
        signal["expiry"] = "2026-09-30"
        with self.assertRaisesRegex(ValueError, "No exact contract"):
            resolve(signal, master, self.now)
        self.assertEqual(master["contracts"][0]["tick_size"], "0.05")

    def test_duplicates_and_unknown_economics_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.build([self.row(1), self.row(2)])
        self.profile["products"]["NIFTY"]["verified_lot_sizes"] = [30]
        with self.assertRaisesRegex(ValueError, "Unverified lot"):
            self.build([self.row(1)])

    def test_stale_profile_and_receipt_fail(self):
        self.build([self.row(1)])
        receipt = Path(str(self.path) + ".receipt.json")
        receipt.write_text("{}")
        with self.assertRaisesRegex(ValueError, "receipt"):
            normalize({"nse_fo": self.path}, self.profile, self.now)
        self.profile["verified_on"] = "2026-09-21"
        with self.assertRaisesRegex(ValueError, "verification"):
            normalize({"nse_fo": self.path}, self.profile, self.now)

    def test_segment_specific_dates(self):
        self.assertEqual(expiry_date("1477578600", "nse_fo").isoformat(), "2026-10-27")
        self.assertEqual(expiry_date("1795823999", "mcx_fo").isoformat(), "2026-11-27")

    def test_expired_today_not_selected_after_cutoff(self):
        self.now = self.now.replace(hour=16)
        master = self.build([self.row(1), self.row(2, day=29)])
        self.assertEqual([c["token"] for c in master["contracts"]], ["2"])

    def test_nonfinite_multiplier_fails(self):
        self.profile["products"]["NIFTY"]["premium_rupees_per_quantity_point"] = "NaN"
        with self.assertRaises(ValueError):
            self.build([self.row(1)])


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_shared_contract_and_restart(self):
        class WS:
            def __init__(self):
                self.calls = []

            async def subscribe_scrips(self, tokens):
                self.calls.append(("add", tokens))

            async def unsubscribe_scrips(self, tokens):
                self.calls.append(("remove", tokens))

        ws = WS()
        subs = Subscriptions(ws, lambda s, t: (s, t))
        positions = {
            "a": {"status": "OPEN", "contract": {"segment": "nse_fo", "token": "1"}},
            "b": {"status": "PENDING", "contract": {"segment": "nse_fo", "token": "1"}},
        }
        await subs.sync(positions)  # Restores persisted open trades without loading whole catalogue.
        await subs.sync(positions)
        self.assertEqual(len(ws.calls), 1)
        positions["a"]["status"] = "CLOSED"
        await subs.sync(positions)
        self.assertEqual(len(ws.calls), 1)
        positions["b"]["status"] = "CANCELLED"
        self.assertEqual(await subs.sync(positions), {("nse_fo", "1")})
        self.assertEqual(ws.calls[-1], ("remove", [("nse_fo", "1")]))

    async def test_failed_subscription_not_marked_successful(self):
        class WS:
            async def subscribe_scrips(self, tokens):
                raise RuntimeError("offline")

            async def unsubscribe_scrips(self, tokens):
                pass

        subs = Subscriptions(WS(), lambda s, t: (s, t))
        with self.assertRaises(RuntimeError):
            await subs.sync({"a": {"status": "PENDING", "contract": {"segment": "nse_fo", "token": "1"}}})
        self.assertFalse(subs.current)

    async def test_limit_fails_before_changing_subscriptions(self):
        class WS:
            async def subscribe_scrips(self, tokens):
                raise AssertionError("No subscription should be attempted")

            async def unsubscribe_scrips(self, tokens):
                raise AssertionError("No subscription should be removed")

        subs = Subscriptions(WS(), lambda s, t: (s, t), limit=1)
        positions = {
            str(i): {"status": "OPEN", "contract": {"segment": "mcx_fo", "token": str(i)}} for i in range(2)
        }
        with self.assertRaisesRegex(ValueError, "limit exceeded"):
            await subs.sync(positions)
