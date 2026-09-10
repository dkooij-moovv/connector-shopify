from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from ..graphql.order import _search_date, orders_bulk_query


class TestShopifyOrderDriftWatermark(TransactionCase):
    """The drift cron must resume from where it got to, on updated_at.

    Two bugs this guards against, both silent:

    * Filtering the drift window on ``created_at`` means an order placed
      months ago and fulfilled today never comes back round, so its
      outstanding quantity stops decreasing as goods ship.
    * A fixed "last two days" window loses everything that changed while the
      cron was not running - a long weekend is enough.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["shopify.instance"].create({
            "name": "Drift Shop",
            "shop_url": "drift-shop.myshopify.com",
            "access_token": "test-token",
        })

    def _queued_since(self):
        """Run the cron and return the date it asked Shopify for."""
        before = self.env["queue.job"].search([]).ids
        self.env["shopify.instance"]._cron_reconcile_orders()
        job = self.env["queue.job"].search([("id", "not in", before)], limit=1)
        self.assertTrue(job, "the drift cron queued nothing")
        return job.args[0]

    def test_query_filters_on_the_requested_date_field(self):
        self.assertIn("created_at:>=2026-09-01", orders_bulk_query("2026-09-01"))
        self.assertIn(
            "updated_at:>=2026-09-01",
            orders_bulk_query("2026-09-01", date_field="updated_at"),
        )
        with self.assertRaises(ValueError):
            orders_bulk_query("2026-09-01", date_field="processed_at")

    def test_a_datetime_bound_keeps_its_time(self):
        """A catch-up resumes from the minute, not from the start of the day."""
        self.assertEqual(
            _search_date(datetime(2026, 9, 9, 18, 32, 5)), "2026-09-09T18:32:05Z"
        )
        self.assertEqual(_search_date("2026-09-09"), "2026-09-09")

    def test_first_run_falls_back_to_a_two_day_window(self):
        self.instance.write({"state": "connected", "order_drift_watermark": False})
        since = fields.Datetime.from_string(self._queued_since())
        self.assertLess(abs((fields.Datetime.now() - since).days - 2), 1)

    def test_a_missed_week_is_caught_up_not_lost(self):
        watermark = fields.Datetime.now() - timedelta(days=7)
        self.instance.write({"state": "connected", "order_drift_watermark": watermark})
        since = fields.Datetime.from_string(self._queued_since())
        self.assertLess(
            abs((fields.Datetime.now() - since).days - 7), 1,
            "a run missed for a week must reach back a week, not two days",
        )

    def test_the_cron_does_not_advance_the_watermark_itself(self):
        """Only the job may advance it, and only once the window is queued.

        Advancing in the cron would move the window past records the fetch
        never reached, and those changes would never come back.
        """
        before = fields.Datetime.now() - timedelta(days=3)
        self.instance.write({"state": "connected", "order_drift_watermark": before})
        self._queued_since()
        self.assertEqual(
            self.instance.order_drift_watermark, before,
            "the cron advanced the watermark before the fetch had run",
        )

    def test_a_failed_fetch_leaves_the_watermark_alone(self):
        """Prefer processing twice over losing a change."""
        before = fields.Datetime.now() - timedelta(days=3)
        self.instance.write({"state": "connected", "order_drift_watermark": before})

        def explodes(self, *args, **kwargs):
            raise RuntimeError("Shopify unreachable")

        with patch.object(type(self.instance), "_shopify_client", explodes):
            with self.assertRaises(RuntimeError):
                self.instance._job_fetch_orders_bulk(
                    "2026-09-01 00:00:00", False,
                    date_field="updated_at",
                    watermark=fields.Datetime.to_string(fields.Datetime.now()),
                )
        self.assertEqual(self.instance.order_drift_watermark, before)

    def test_the_overlap_is_applied_and_is_small(self):
        """Ten minutes: enough for clock skew, not a whole day of re-reading."""
        watermark = fields.Datetime.now() - timedelta(days=3)
        self.instance.write({"state": "connected", "order_drift_watermark": watermark})
        since = fields.Datetime.from_string(self._queued_since())
        self.assertEqual(watermark - since, timedelta(minutes=10))


class TestShopifyOutstandingDemand(TransactionCase):
    """What Shopify still owes is not the same as what it never shipped.

    unfulfilledQuantity keeps counting a line the customer has been refunded
    for; currentQuantity is what survives refunds and cancellations. Open
    demand is the smaller of the two.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["shopify.instance"].create({
            "name": "Demand Shop",
            "shop_url": "demand-shop.myshopify.com",
            "access_token": "test-token",
            "import_open_quantity_only": True,
        })
        cls.sync = cls.env["shopify.order"]

    def _demand(self, quantity, unfulfilled):
        return self.sync._line_demand(
            self.instance, {"quantity": quantity, "unfulfilled_quantity": unfulfilled}
        )

    def test_nothing_shipped_yet_is_fully_owed(self):
        self.assertEqual(self._demand(3, 3), 3)

    def test_a_partial_shipment_leaves_the_remainder(self):
        self.assertEqual(self._demand(3, 1), 1)

    def test_a_refunded_line_is_owed_nothing(self):
        """currentQuantity 0 with unfulfilledQuantity 1: refunded, never shipped."""
        self.assertEqual(self._demand(0, 1), 0)

    def test_a_missing_unfulfilled_quantity_falls_back_to_ordered(self):
        """Draft orders carry no fulfilment data; absent is not the same as 0."""
        self.assertEqual(self._demand(2, None), 2)

    def test_the_flag_off_keeps_the_ordered_quantity(self):
        self.instance.import_open_quantity_only = False
        self.assertEqual(self._demand(3, 1), 3)


class TestShopifyDraftOrderPagination(TransactionCase):
    """A catch-up can span pages. Nothing may be skipped, and a failure
    halfway must not move the window past records nobody processed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["shopify.instance"].create({
            "name": "Paging Shop",
            "shop_url": "paging-shop.myshopify.com",
            "access_token": "test-token",
            "state": "connected",
        })

    def _pages(self, aantal, valt_om_op=None):
        """A fake client handing out `aantal` pages of one draft order each."""
        bezocht = []

        class Client:
            def execute(_self, document, variables=None):
                cursor = (variables or {}).get("after")
                bladzijde = len(bezocht)
                bezocht.append(cursor)
                if valt_om_op is not None and bladzijde == valt_om_op:
                    raise RuntimeError("Shopify fell over mid-pagination")
                return {"draftOrders": {
                    "nodes": [{
                        "id": f"gid://shopify/DraftOrder/{bladzijde}",
                        "name": f"#D{bladzijde}",
                        "status": "OPEN",
                        "createdAt": "2026-09-01T10:00:00Z",
                        "currencyCode": "EUR",
                        "lineItems": {"nodes": []},
                    }],
                    "pageInfo": {
                        "hasNextPage": bladzijde + 1 < aantal,
                        "endCursor": f"cursor-{bladzijde}",
                    },
                }}

        return Client(), bezocht

    def test_every_page_is_walked_and_nothing_skipped(self):
        client, bezocht = self._pages(4)
        with patch.object(type(self.instance), "_shopify_client", lambda _s: client):
            aantal = self.instance._queue_draft_orders("2026-09-01 00:00:00")
        self.assertEqual(aantal, 4, "one draft order per page should be queued")
        self.assertEqual(
            bezocht, [None, "cursor-0", "cursor-1", "cursor-2"],
            "each request must carry the previous page's cursor",
        )

    def test_a_failure_on_a_later_page_does_not_advance_the_watermark(self):
        """Page three dies. The watermark stays where it was, so the next run
        reads the whole window again - duplicates are free, gaps are not."""
        before = fields.Datetime.now() - timedelta(days=5)
        self.instance.order_drift_watermark = before
        client, _ = self._pages(5, valt_om_op=2)

        with patch.object(type(self.instance), "_shopify_client", lambda _s: client), \
             patch("odoo.addons.shopify_connector.models.order_sync.ShopifyBulkRunner") as runner:
            runner.return_value.run.return_value = []
            with self.assertRaises(RuntimeError):
                self.instance._job_fetch_orders_bulk(
                    "2026-09-01 00:00:00", False,
                    date_field="updated_at",
                    watermark=fields.Datetime.to_string(fields.Datetime.now()),
                )
        self.assertEqual(
            self.instance.order_drift_watermark, before,
            "a failure mid-pagination must leave the watermark untouched",
        )

    def test_a_missing_cursor_is_refused_rather_than_looping(self):
        """hasNextPage without endCursor would spin forever on page one."""
        class Broken:
            def execute(_self, document, variables=None):
                return {"draftOrders": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                }}

        with patch.object(type(self.instance), "_shopify_client", lambda _s: Broken()):
            with self.assertRaises(Exception):
                self.instance._queue_draft_orders("2026-09-01 00:00:00")

    def test_a_datetime_window_reaches_the_draft_query_too(self):
        """The bulk query keeps the time; the draft query must not drop it."""
        gezien = {}

        class Client:
            def execute(_self, document, variables=None):
                gezien["query"] = (variables or {}).get("query")
                return {"draftOrders": {"nodes": [], "pageInfo": {"hasNextPage": False}}}

        with patch.object(type(self.instance), "_shopify_client", lambda _s: Client()):
            self.instance._queue_draft_orders(
                "2026-09-09 18:32:05", False, date_field="updated_at")
        self.assertIn("updated_at:>=2026-09-09T18:32:05Z", gezien["query"])
