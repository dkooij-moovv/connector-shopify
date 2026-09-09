from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

from ..graphql.order import orders_bulk_query


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

    def test_first_run_falls_back_to_a_two_day_window(self):
        self.instance.write({"state": "connected", "order_drift_watermark": False})
        expected = fields.Date.context_today(self.instance) - timedelta(days=2)
        self.assertEqual(self._queued_since(), fields.Date.to_string(expected))

    def test_a_missed_week_is_caught_up_not_lost(self):
        self.instance.write({
            "state": "connected",
            "order_drift_watermark": fields.Datetime.now() - timedelta(days=7),
        })
        since = fields.Date.from_string(self._queued_since())
        self.assertEqual(
            (fields.Date.context_today(self.instance) - since).days, 8,
            "a run missed for a week must reach back a week, not two days",
        )

    def test_the_watermark_advances_after_a_run(self):
        self.instance.write({
            "state": "connected",
            "order_drift_watermark": fields.Datetime.now() - timedelta(days=3),
        })
        self._queued_since()
        self.assertGreater(
            self.instance.order_drift_watermark,
            fields.Datetime.now() - timedelta(minutes=5),
        )
