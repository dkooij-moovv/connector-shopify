"""Guard the shipped GraphQL documents against fields Shopify has removed.

Every entry below was rejected by the live Shopify Admin GraphQL API at the
version this module targets, so no document may request it again.
"""

from odoo.addons.shopify_connector.graphql import (
    COMPANIES_QUERY,
    INVENTORY_LEVEL_QUERY,
    INVENTORY_LEVELS_BULK_QUERY,
    ORDER_BY_ID_QUERY,
)
from odoo.tests.common import TransactionCase


class TestShopifyGraphqlApiVersion(TransactionCase):
    def test_order_query_requests_the_supported_order_number_field(self):
        assert "orderNumber" not in ORDER_BY_ID_QUERY
        assert "\n    number\n" in ORDER_BY_ID_QUERY

    def test_inventory_documents_drop_the_removed_level_active_flag(self):
        for document in (INVENTORY_LEVEL_QUERY, INVENTORY_LEVELS_BULK_QUERY):
            assert "isActive" not in document
        # ``inventoryLevel`` no longer accepts the argument either.
        assert "includeInactive" not in INVENTORY_LEVEL_QUERY

    def test_company_role_assignments_use_the_renamed_contact_field(self):
        assert "companyContact {" in COMPANIES_QUERY
