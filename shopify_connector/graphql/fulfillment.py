"""Shopify Admin GraphQL documents for fulfillment synchronization."""

FULFILLMENT_ORDER_FIELDS = """
    id
    status
    requestStatus
    assignedLocation {
      location { id name }
    }
    supportedActions { action }
    fulfillmentHolds {
      id
      reason
      reasonNotes
      heldByRequestingApp
    }
    lineItems(first: 250) {
      nodes {
        id
        totalQuantity
        remainingQuantity
        lineItem { id }
      }
    }
"""

ORDER_FULFILLMENT_ORDERS_QUERY = (
    "query ShopifyOrderFulfillmentOrders($id: ID!) {"
    " order(id: $id) { id fulfillmentOrders(first: 250) { nodes {"
    + FULFILLMENT_ORDER_FIELDS
    + "} } } }"
)

FULFILLMENT_BY_ID_QUERY = """
query ShopifyFulfillment($id: ID!) {
  fulfillment(id: $id) {
    id
    name
    status
    createdAt
    updatedAt
    order { id }
    location { id }
    fulfillmentOrders(first: 250) {
      nodes { id }
    }
    trackingInfo {
      company
      number
      url
    }
    fulfillmentLineItems(first: 250) {
      nodes {
        id
        quantity
        lineItem { id }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation ShopifyFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment {
      id
      name
      status
      createdAt
      updatedAt
      location { id }
      trackingInfo {
        company
        number
        url
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

FULFILLMENT_ORDER_MOVE_MUTATION = """
mutation ShopifyFulfillmentOrderMove($id: ID!, $newLocationId: ID!) {
  fulfillmentOrderMove(id: $id, newLocationId: $newLocationId) {
    movedFulfillmentOrder {
      id
      status
      requestStatus
    }
    originalFulfillmentOrder {
      id
      status
      requestStatus
    }
    remainingFulfillmentOrder {
      id
      status
      requestStatus
    }
    userErrors {
      field
      message
    }
  }
}
"""

FULFILLMENT_ORDER_RELEASE_HOLD_MUTATION = """
mutation ShopifyFulfillmentOrderReleaseHold($id: ID!, $holdIds: [ID!]) {
  fulfillmentOrderReleaseHold(id: $id, holdIds: $holdIds) {
    fulfillmentOrder {
      id
      status
      requestStatus
    }
    userErrors {
      field
      message
    }
  }
}
"""

FULFILLMENT_CANCEL_MUTATION = """
mutation ShopifyFulfillmentCancel($id: ID!) {
  fulfillmentCancel(id: $id) {
    fulfillment {
      id
      status
    }
    userErrors {
      field
      message
    }
  }
}
"""


def fulfillments_bulk_query(date_from=None, date_to=None) -> str:
    """Bulk query for the fulfillments (shipment records) of each order.

    Shopify has no top-level fulfillments query, so they are reached through
    orders. Two API constraints shape this document and neither can be worked
    around, so do not "improve" it without re-testing against the live API:

    * ``Order.fulfillments`` is a LIST, not a connection, and Shopify rejects
      a bulk query with "connection field within a list field". So
      ``fulfillmentLineItems`` cannot be nested here.
    * Routing around that via ``fulfillmentOrders { fulfillments { ... } }``
      is all connections, but then ``fulfillmentLineItems`` sits at depth 3
      and Shopify rejects "nesting depth greater than 2".

    Line-level shipped quantities are therefore not fetched here. They are
    already derivable: shopify.fulfillment.order.line carries total_quantity
    and remaining_quantity, and shipped = total - remaining.

    Because the list is inlined rather than paginated, ``first`` caps how many
    fulfillments an order can report. 50 is far above any realistic split
    shipment; orders that exceed it would need the per-order query.
    """
    from .order import _search_date

    terms = []
    if date_from:
        terms.append(f"created_at:>={_search_date(date_from)}")
    if date_to:
        terms.append(f"created_at:<={_search_date(date_to)}")
    search = " ".join(terms)
    root = f'orders(query: "{search}")' if search else "orders"
    return (
        "{ "
        + root
        + """ { edges { node {
        id
        fulfillments(first: 50) {
          id
          name
          createdAt
          updatedAt
          status
          displayStatus
          deliveredAt
          estimatedDeliveryAt
          trackingInfo { number company url }
          location { id }
        }
      } } } }"""
    )
