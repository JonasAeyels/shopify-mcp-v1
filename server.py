#!/usr/bin/env python3
"""
Shopify MCP Server — Full Admin API access via FastMCP.
Provides tools for managing products, orders, customers, collections,
inventory, and fulfillments through the Shopify Admin REST API.

Token Management:
  - Uses client_credentials grant to auto-generate and refresh tokens
  - Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (recommended for OAuth apps)
  - Falls back to static SHOPIFY_ACCESS_TOKEN if client credentials not set
"""
import csv
import json
import os
import logging
import time
import asyncio
from typing import Optional, List, Dict, Any
from enum import Enum
import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOPIFY_STORE        = os.environ.get("SHOPIFY_STORE", "")           # e.g. "my-store"
SHOPIFY_TOKEN        = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")    # Static token (shpat_...)
SHOPIFY_CLIENT_ID    = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
API_VERSION          = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

# Refresh buffer: refresh token 30 minutes before expiry (only used with OAuth)
TOKEN_REFRESH_BUFFER = int(os.environ.get("TOKEN_REFRESH_BUFFER", "1800"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shopify_mcp")

PORT          = int(os.environ.get("PORT", "8000"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")

mcp = FastMCP("shopify_mcp", host="0.0.0.0", port=PORT, json_response=True)


# ---------------------------------------------------------------------------
# Token Manager — handles automatic token lifecycle
# ---------------------------------------------------------------------------

class TokenManager:
    """
    Manages Shopify Admin API access tokens.

    Two modes:
      1. Static token  — set SHOPIFY_ACCESS_TOKEN (recommended for Custom Apps)
      2. OAuth / client_credentials — set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
         Enables auto-refresh before expiry and retry on 401.
    """

    def __init__(
        self,
        store: str,
        client_id: str,
        client_secret: str,
        static_token: str = "",
        refresh_buffer: int = 1800,
    ):
        self._store         = store
        self._client_id     = client_id
        self._client_secret = client_secret
        self._static_token  = static_token
        self._refresh_buffer = refresh_buffer

        self._access_token: str   = ""
        self._expires_at: float   = 0.0
        self._lock = asyncio.Lock()

        self._use_client_credentials = bool(client_id and client_secret)

        if self._use_client_credentials:
            logger.info("Token mode: client_credentials (auto-refresh enabled)")
        elif static_token:
            logger.info("Token mode: static SHOPIFY_ACCESS_TOKEN (no auto-refresh)")
            self._access_token = static_token
            self._expires_at   = float("inf")
        else:
            logger.warning(
                "No credentials configured. Set SHOPIFY_ACCESS_TOKEN or "
                "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET."
            )

    @property
    def is_expired(self) -> bool:
        if not self._access_token:
            return True
        return time.time() >= (self._expires_at - self._refresh_buffer)

    async def get_token(self) -> str:
        if not self.is_expired:
            return self._access_token

        async with self._lock:
            if not self.is_expired:
                return self._access_token

            if self._use_client_credentials:
                await self._refresh_token()
            elif not self._access_token:
                raise RuntimeError(
                    "No valid token available. "
                    "Set SHOPIFY_ACCESS_TOKEN in your environment variables."
                )

        return self._access_token

    async def force_refresh(self) -> str:
        if not self._use_client_credentials:
            raise RuntimeError(
                "Cannot refresh — using a static token. "
                "Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET to enable auto-refresh."
            )
        async with self._lock:
            await self._refresh_token()
        return self._access_token

    async def _refresh_token(self) -> None:
        url = f"https://{self._store}.myshopify.com/admin/oauth/access_token"
        logger.info("Refreshing Shopify access token via client_credentials grant...")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

            if resp.status_code != 200:
                logger.error(f"Token refresh failed ({resp.status_code}): {resp.text[:500]}")
                raise RuntimeError(
                    f"Token refresh failed ({resp.status_code}). "
                    "Check SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET."
                )

            data               = resp.json()
            self._access_token = data["access_token"]
            expires_in         = data.get("expires_in", 86399)
            self._expires_at   = time.time() + expires_in

            scope         = data.get("scope", "")
            scope_preview = scope[:80] + "..." if len(scope) > 80 else scope
            logger.info(
                f"Token refreshed. Expires in {expires_in}s "
                f"({expires_in // 3600}h {(expires_in % 3600) // 60}m). "
                f"Scopes: {scope_preview}"
            )


# Global token manager
token_manager = TokenManager(
    store=SHOPIFY_STORE,
    client_id=SHOPIFY_CLIENT_ID,
    client_secret=SHOPIFY_CLIENT_SECRET,
    static_token=SHOPIFY_TOKEN,
    refresh_buffer=TOKEN_REFRESH_BUFFER,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}"


async def _headers() -> dict:
    token = await token_manager.get_token()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body:   Optional[dict] = None,
    _retried: bool = False,
) -> dict:
    """Central HTTP helper — every API call flows through here.
    Auto-retries once on 401 when using OAuth credentials.
    """
    if not SHOPIFY_STORE:
        raise RuntimeError(
            "Missing SHOPIFY_STORE environment variable. "
            "Set it before starting the server."
        )

    url     = f"{_base_url()}/{path}"
    headers = await _headers()

    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method, url,
            headers=headers,
            params=params,
            json=body,
            timeout=30.0,
        )

        if resp.status_code == 401 and not _retried and token_manager._use_client_credentials:
            logger.warning("Got 401 — refreshing token and retrying...")
            await token_manager.force_refresh()
            return await _request(method, path, params=params, body=body, _retried=True)

        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()


def _error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        host = e.request.url.host
        if not host.endswith("myshopify.com"):
            return f"{host} error {status} on {e.request.url.path}: {json.dumps(detail)[:300]}"
        messages = {
            401: "Authentication failed — check your SHOPIFY_ACCESS_TOKEN (should start with shpat_).",
            403: "Permission denied — your token may be missing required API scopes.",
            404: "Resource not found — double-check the ID.",
            422: f"Validation error: {json.dumps(detail)}",
            429: "Rate-limited — wait a moment and retry.",
        }
        return messages.get(status, f"Shopify API error {status}: {json.dumps(detail)}")
    if isinstance(e, httpx.TimeoutException):
        return "Request timed out — try again."
    if isinstance(e, RuntimeError):
        return str(e)
    return f"Unexpected error: {type(e).__name__}: {e}"


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ═══════════════════════════════════════════════════════════════════════════

class ListProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int]  = Field(default=50, ge=1, le=250, description="Max products to return (1-250)")
    status:         Optional[str]  = Field(default=None, description="Filter by status: active, archived, draft")
    product_type:   Optional[str]  = Field(default=None, description="Filter by product type")
    vendor:         Optional[str]  = Field(default=None, description="Filter by vendor name")
    collection_id:  Optional[int]  = Field(default=None, description="Filter by collection ID")
    since_id:       Optional[int]  = Field(default=None, description="Pagination: return products after this ID")
    fields:         Optional[str]  = Field(default=None, description="Comma-separated fields to include")


@mcp.tool(
    name="shopify_list_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_products(params: ListProductsInput) -> str:
    """List products from the Shopify store with optional filters."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for field in ["status", "product_type", "vendor", "collection_id", "since_id", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


class GetProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="The Shopify product ID")


@mcp.tool(
    name="shopify_get_product",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_product(params: GetProductInput) -> str:
    """Retrieve a single product by ID, including all variants and images."""
    try:
        data = await _request("GET", f"products/{params.product_id}.json")
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class CreateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title:        str                        = Field(..., min_length=1, description="Product title")
    body_html:    Optional[str]              = Field(default=None, description="HTML description")
    vendor:       Optional[str]              = Field(default=None)
    product_type: Optional[str]              = Field(default=None)
    tags:         Optional[str]              = Field(default=None, description="Comma-separated tags")
    status:       Optional[str]              = Field(default="draft", description="active, archived, or draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None, description="Variant objects with price, sku, etc.")
    options:      Optional[List[Dict[str, Any]]] = Field(default=None, description="Product options (Size, Color, etc.)")
    images:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Image objects with src URL")


@mcp.tool(
    name="shopify_create_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_product(params: CreateProductInput) -> str:
    """Create a new product in the Shopify store."""
    try:
        product: Dict[str, Any] = {"title": params.title}
        for field in ["body_html", "vendor", "product_type", "tags", "status", "variants", "options", "images"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("POST", "products.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class UpdateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id:   int            = Field(..., description="Product ID to update")
    title:        Optional[str]  = Field(default=None)
    body_html:    Optional[str]  = Field(default=None)
    vendor:       Optional[str]  = Field(default=None)
    product_type: Optional[str]  = Field(default=None)
    tags:         Optional[str]  = Field(default=None)
    status:       Optional[str]  = Field(default=None, description="active, archived, or draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None)


@mcp.tool(
    name="shopify_update_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_product(params: UpdateProductInput) -> str:
    """Update an existing product. Only provided fields are changed."""
    try:
        product: Dict[str, Any] = {}
        for field in ["title", "body_html", "vendor", "product_type", "tags", "status", "variants"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("PUT", f"products/{params.product_id}.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class DeleteProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="Product ID to delete")


@mcp.tool(
    name="shopify_delete_product",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_delete_product(params: DeleteProductInput) -> str:
    """Permanently delete a product. This cannot be undone."""
    try:
        await _request("DELETE", f"products/{params.product_id}.json")
        return f"Product {params.product_id} deleted."
    except Exception as e:
        return _error(e)


class ProductCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:       Optional[str] = Field(default=None, description="active, archived, or draft")
    vendor:       Optional[str] = Field(default=None)
    product_type: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_products(params: ProductCountInput) -> str:
    """Get the total count of products, optionally filtered."""
    try:
        p: Dict[str, Any] = {}
        for field in ["status", "vendor", "product_type"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "products/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════════════════

class ListOrdersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:               Optional[int] = Field(default=50, ge=1, le=250)
    status:              Optional[str] = Field(default="any", description="open, closed, cancelled, any")
    financial_status:    Optional[str] = Field(default=None, description="authorized, pending, paid, refunded, voided, any")
    fulfillment_status:  Optional[str] = Field(default=None, description="shipped, partial, unshipped, unfulfilled, any")
    since_id:            Optional[int] = Field(default=None)
    created_at_min:      Optional[str] = Field(default=None, description="ISO 8601 date, e.g. 2024-01-01T00:00:00Z")
    created_at_max:      Optional[str] = Field(default=None)
    fields:              Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_orders(params: ListOrdersInput) -> str:
    """List orders with optional filters for status, financial/fulfillment status, and date range."""
    try:
        p: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        for field in ["financial_status", "fulfillment_status", "since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data   = await _request("GET", "orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="The Shopify order ID")


@mcp.tool(
    name="shopify_get_order",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_order(params: GetOrderInput) -> str:
    """Retrieve a single order by ID with full details."""
    try:
        data = await _request("GET", f"orders/{params.order_id}.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class OrderCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:             Optional[str] = Field(default="any")
    financial_status:   Optional[str] = Field(default=None)
    fulfillment_status: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_orders(params: OrderCountInput) -> str:
    """Get total order count, optionally filtered."""
    try:
        p: Dict[str, Any] = {"status": params.status}
        for field in ["financial_status", "fulfillment_status"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "orders/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


class CloseOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="Order ID to close")


@mcp.tool(
    name="shopify_close_order",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_close_order(params: CloseOrderInput) -> str:
    """Close an order (marks it as completed)."""
    try:
        data = await _request("POST", f"orders/{params.order_id}/close.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class CancelOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int            = Field(..., description="Order ID to cancel")
    reason:   Optional[str]  = Field(default=None, description="customer, fraud, inventory, declined, other")
    email:    Optional[bool] = Field(default=True,  description="Send cancellation email to customer")
    restock:  Optional[bool] = Field(default=False, description="Restock line items")


@mcp.tool(
    name="shopify_cancel_order",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_cancel_order(params: CancelOrderInput) -> str:
    """Cancel an order. Optionally restock items and notify the customer."""
    try:
        body: Dict[str, Any] = {}
        for field in ["reason", "email", "restock"]:
            val = getattr(params, field)
            if val is not None:
                body[field] = val
        data = await _request("POST", f"orders/{params.order_id}/cancel.json", body=body)
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════════════════

class ListCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int] = Field(default=50, ge=1, le=250)
    since_id:       Optional[int] = Field(default=None)
    created_at_min: Optional[str] = Field(default=None, description="ISO 8601 date")
    created_at_max: Optional[str] = Field(default=None)
    fields:         Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_customers(params: ListCustomersInput) -> str:
    """List customers from the store."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for f in ["since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, f)
            if val is not None:
                p[f] = val
        data      = await _request("GET", "customers.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class SearchCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str           = Field(..., min_length=1, description="Search query (name, email, etc.)")
    limit: Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_search_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_search_customers(params: SearchCustomersInput) -> str:
    """Search customers by name, email, or other fields."""
    try:
        p         = {"query": params.query, "limit": params.limit}
        data      = await _request("GET", "customers/search.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class GetCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int = Field(..., description="Shopify customer ID")


@mcp.tool(
    name="shopify_get_customer",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer(params: GetCustomerInput) -> str:
    """Retrieve a single customer by ID."""
    try:
        data = await _request("GET", f"customers/{params.customer_id}.json")
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CreateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    first_name:         Optional[str]  = Field(default=None)
    last_name:          Optional[str]  = Field(default=None)
    email:              Optional[str]  = Field(default=None)
    phone:              Optional[str]  = Field(default=None)
    tags:               Optional[str]  = Field(default=None)
    note:               Optional[str]  = Field(default=None)
    addresses:          Optional[List[Dict[str, Any]]] = Field(default=None)
    send_email_invite:  Optional[bool] = Field(default=False)


@mcp.tool(
    name="shopify_create_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_customer(params: CreateCustomerInput) -> str:
    """Create a new customer."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note", "addresses", "send_email_invite"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("POST", "customers.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class UpdateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    customer_id: int           = Field(..., description="Customer ID to update")
    first_name:  Optional[str] = Field(default=None)
    last_name:   Optional[str] = Field(default=None)
    email:       Optional[str] = Field(default=None)
    phone:       Optional[str] = Field(default=None)
    tags:        Optional[str] = Field(default=None)
    note:        Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_update_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_customer(params: UpdateCustomerInput) -> str:
    """Update an existing customer. Only provided fields are changed."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("PUT", f"customers/{params.customer_id}.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CustomerOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int           = Field(..., description="Customer ID")
    limit:       Optional[int] = Field(default=50, ge=1, le=250)
    status:      Optional[str] = Field(default="any")


@mcp.tool(
    name="shopify_get_customer_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer_orders(params: CustomerOrdersInput) -> str:
    """Get all orders for a specific customer."""
    try:
        p      = {"limit": params.limit, "status": params.status}
        data   = await _request("GET", f"customers/{params.customer_id}/orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# COLLECTIONS (Custom + Smart)
# ═══════════════════════════════════════════════════════════════════════════

class ListCollectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit:           Optional[int] = Field(default=50, ge=1, le=250)
    since_id:        Optional[int] = Field(default=None)
    collection_type: Optional[str] = Field(default="custom", description="'custom' or 'smart'")


@mcp.tool(
    name="shopify_list_collections",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_collections(params: ListCollectionsInput) -> str:
    """List custom or smart collections."""
    try:
        endpoint = "custom_collections.json" if params.collection_type == "custom" else "smart_collections.json"
        p: Dict[str, Any] = {"limit": params.limit}
        if params.since_id:
            p["since_id"] = params.since_id
        data = await _request("GET", endpoint, params=p)
        key  = "custom_collections" if params.collection_type == "custom" else "smart_collections"
        collections = data.get(key, [])
        return _fmt({"count": len(collections), "collections": collections})
    except Exception as e:
        return _error(e)


class GetCollectionProductsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_id: int           = Field(..., description="Collection ID")
    limit:         Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_get_collection_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_collection_products(params: GetCollectionProductsInput) -> str:
    """Get all products in a specific collection."""
    try:
        p        = {"limit": params.limit, "collection_id": params.collection_id}
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# INVENTORY
# ═══════════════════════════════════════════════════════════════════════════

class ListInventoryLocationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_list_locations",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_locations(params: ListInventoryLocationsInput) -> str:
    """List all inventory locations for the store."""
    try:
        data      = await _request("GET", "locations.json")
        locations = data.get("locations", [])
        return _fmt({"count": len(locations), "locations": locations})
    except Exception as e:
        return _error(e)


class GetInventoryLevelsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id:         Optional[int] = Field(default=None, description="Filter by location ID")
    inventory_item_ids:  Optional[str] = Field(default=None, description="Comma-separated inventory item IDs")


@mcp.tool(
    name="shopify_get_inventory_levels",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_inventory_levels(params: GetInventoryLevelsInput) -> str:
    """Get inventory levels for specific locations or inventory items."""
    try:
        p: Dict[str, Any] = {}
        if params.location_id:
            p["location_ids"] = params.location_id
        if params.inventory_item_ids:
            p["inventory_item_ids"] = params.inventory_item_ids
        data   = await _request("GET", "inventory_levels.json", params=p)
        levels = data.get("inventory_levels", [])
        return _fmt({"count": len(levels), "inventory_levels": levels})
    except Exception as e:
        return _error(e)


class SetInventoryLevelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_item_id: int = Field(..., description="Inventory item ID")
    location_id:       int = Field(..., description="Location ID")
    available:         int = Field(..., description="Available quantity to set")


@mcp.tool(
    name="shopify_set_inventory_level",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_set_inventory_level(params: SetInventoryLevelInput) -> str:
    """Set the available inventory for an item at a location."""
    try:
        body = {
            "inventory_item_id": params.inventory_item_id,
            "location_id":       params.location_id,
            "available":         params.available,
        }
        data = await _request("POST", "inventory_levels/set.json", body=body)
        return _fmt(data.get("inventory_level", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# FULFILLMENTS
# ═══════════════════════════════════════════════════════════════════════════

class ListFulfillmentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int           = Field(..., description="Order ID")
    limit:    Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_list_fulfillments",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_fulfillments(params: ListFulfillmentsInput) -> str:
    """List fulfillments for a specific order."""
    try:
        p            = {"limit": params.limit}
        data         = await _request("GET", f"orders/{params.order_id}/fulfillments.json", params=p)
        fulfillments = data.get("fulfillments", [])
        return _fmt({"count": len(fulfillments), "fulfillments": fulfillments})
    except Exception as e:
        return _error(e)


class CreateFulfillmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id:         int                        = Field(..., description="Order ID to fulfill")
    location_id:      int                        = Field(..., description="Location ID fulfilling from")
    tracking_number:  Optional[str]              = Field(default=None)
    tracking_company: Optional[str]              = Field(default=None, description="e.g. UPS, FedEx, USPS")
    tracking_url:     Optional[str]              = Field(default=None)
    line_items:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Specific line items (omit for all)")
    notify_customer:  Optional[bool]             = Field(default=True, description="Send shipping notification email")


@mcp.tool(
    name="shopify_create_fulfillment",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_fulfillment(params: CreateFulfillmentInput) -> str:
    """Create a fulfillment for an order (ship items)."""
    try:
        fulfillment: Dict[str, Any] = {"location_id": params.location_id}
        for field in ["tracking_number", "tracking_company", "tracking_url", "line_items", "notify_customer"]:
            val = getattr(params, field)
            if val is not None:
                fulfillment[field] = val
        data = await _request(
            "POST",
            f"orders/{params.order_id}/fulfillments.json",
            body={"fulfillment": fulfillment},
        )
        return _fmt(data.get("fulfillment", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# SHOP INFO
# ═══════════════════════════════════════════════════════════════════════════

class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_get_shop",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_shop(params: EmptyInput) -> str:
    """Get store information: name, domain, plan, currency, timezone, etc."""
    try:
        data = await _request("GET", "shop.json")
        return _fmt(data.get("shop", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════

class ListWebhooksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: Optional[int] = Field(default=50, ge=1, le=250)
    topic: Optional[str] = Field(default=None, description="Filter by topic, e.g. orders/create")


@mcp.tool(
    name="shopify_list_webhooks",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_webhooks(params: ListWebhooksInput) -> str:
    """List configured webhooks."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        if params.topic:
            p["topic"] = params.topic
        data     = await _request("GET", "webhooks.json", params=p)
        webhooks = data.get("webhooks", [])
        return _fmt({"count": len(webhooks), "webhooks": webhooks})
    except Exception as e:
        return _error(e)


class CreateWebhookInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    topic:   str           = Field(..., description="Webhook topic, e.g. orders/create, products/update")
    address: str           = Field(..., description="URL to receive the webhook POST")
    format:  Optional[str] = Field(default="json", description="json or xml")


@mcp.tool(
    name="shopify_create_webhook",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_webhook(params: CreateWebhookInput) -> str:
    """Create a new webhook subscription."""
    try:
        webhook = {"topic": params.topic, "address": params.address, "format": params.format}
        data    = await _request("POST", "webhooks.json", body={"webhook": webhook})
        return _fmt(data.get("webhook", data))
    except Exception as e:
        return _error(e)

class AddToCollectionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id: int = Field(..., description="The Shopify product ID")
    collection_id: int = Field(..., description="The Shopify collection ID")

@mcp.tool(
    name="shopify_add_to_collection",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_add_to_collection(params: AddToCollectionInput) -> str:
    """Add a product to a manual collection via the Collects API."""
    try:
        data = await _request("POST", "collects.json", body={
            "collect": {
                "product_id": params.product_id,
                "collection_id": params.collection_id,
            }
        })
        return _fmt(data.get("collect", data))
    except Exception as e:
        return _error(e)


class AddProductImageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id: int = Field(..., description="The Shopify product ID")
    src: str = Field(..., description="Public URL of the image to add")
    alt: Optional[str] = Field(default=None, description="Alt text for the image")

@mcp.tool(
    name="shopify_add_product_image",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_add_product_image(params: AddProductImageInput) -> str:
    """Add an image to an existing product by URL."""
    try:
        image: Dict[str, Any] = {"src": params.src}
        if params.alt:
            image["alt"] = params.alt
        data = await _request("POST", f"products/{params.product_id}/images.json", body={"image": image})
        return _fmt(data.get("image", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# DAILY PROFIT & ROAS (Shopify + CJ Dropshipping + Google Ads)
# ═══════════════════════════════════════════════════════════════════════════

CJ_API_KEY              = os.environ.get("CJ_API_KEY", "")
CJ_BASE_URL             = "https://developers.cjdropshipping.com/api2.0/v1"
CJ_MAX_PAGES            = int(os.environ.get("CJ_MAX_PAGES", "20"))
PAYMENT_FEE_PERCENT     = float(os.environ.get("PAYMENT_FEE_PERCENT", "1.9"))
PAYMENT_FEE_FIXED       = float(os.environ.get("PAYMENT_FEE_FIXED", "0.25"))
DAILY_FIXED_COSTS       = float(os.environ.get("DAILY_FIXED_COSTS", "0"))
USD_TO_SHOP_RATE        = os.environ.get("USD_TO_SHOP_RATE", "")       # optional fixed rate, e.g. 0.92
GOOGLE_ADS_SPEND_CSV_URL = os.environ.get("GOOGLE_ADS_SPEND_CSV_URL", "")


class CJClient:
    """Minimal CJ Dropshipping API 2.0 client. Caches the access token (valid ~15 days)."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _get_token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            if not self._api_key:
                raise RuntimeError("Missing CJ_API_KEY environment variable (CJ dashboard → Authorization → API).")
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{CJ_BASE_URL}/authentication/getAccessToken",
                    json={"apiKey": self._api_key},
                    timeout=30.0,
                )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("result") or not data.get("data"):
                raise RuntimeError(f"CJ authentication failed: {data.get('message', data)}")
            self._token = data["data"]["accessToken"]
            # CJ tokens last ~15 days; refresh well before that.
            self._expires_at = time.time() + 7 * 24 * 3600
            return self._token

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        token = await self._get_token()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                f"{CJ_BASE_URL}/{path}",
                headers={"CJ-Access-Token": token},
                params=params,
                timeout=30.0,
            )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("result"):
            raise RuntimeError(f"CJ API error on {path}: {data.get('message', data)}")
        return data.get("data")


cj_client = CJClient(CJ_API_KEY)


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_key(value: Any) -> str:
    """Normalise an order number so '#1001', '1001' and 'store-1001' can be matched."""
    s = str(value or "").strip().lstrip("#")
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits or s


async def _shopify_get_all(path: str, params: dict, key: str) -> List[dict]:
    """GET a Shopify list endpoint and follow cursor pagination (Link header)."""
    if not SHOPIFY_STORE:
        raise RuntimeError("Missing SHOPIFY_STORE environment variable.")
    results: List[dict] = []
    url: Optional[str] = f"{_base_url()}/{path}"
    query: Optional[dict] = params
    async with httpx.AsyncClient() as client:
        while url:
            resp = await client.get(url, headers=await _headers(), params=query, timeout=30.0)
            resp.raise_for_status()
            results.extend(resp.json().get(key, []))
            next_link = resp.links.get("next", {}).get("url")
            url, query = next_link, None   # the next URL already carries page_info
    return results


async def _cj_costs_for(order_keys: set, oldest_date: str) -> Dict[str, dict]:
    """Page through recent CJ orders until all wanted orders are found or we pass oldest_date."""
    found: Dict[str, dict] = {}
    for page in range(1, CJ_MAX_PAGES + 1):
        data = await cj_client.get("shopping/order/list", {"pageNum": page, "pageSize": 50})
        rows = (data or {}).get("list") or []
        if not rows:
            break
        for row in rows:
            for candidate in (row.get("orderNum"), row.get("orderId")):
                k = _order_key(candidate)
                if k in order_keys and k not in found:
                    found[k] = row
        if order_keys <= set(found):
            break
        last_created = str(rows[-1].get("createDate") or "")[:10]
        if last_created and last_created < oldest_date:
            break
    return found


async def _usd_rate(currency: str, day: str) -> float:
    if currency == "USD":
        return 1.0
    if USD_TO_SHOP_RATE:
        return float(USD_TO_SHOP_RATE)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            f"https://api.frankfurter.dev/v1/{day}",
            params={"from": "USD", "to": currency},
            timeout=15.0,
        )
    resp.raise_for_status()
    return float(resp.json()["rates"][currency])


async def _google_ads_spend(day: str) -> Optional[float]:
    """Read spend for `day` from a published Google Sheet CSV (columns: date,cost)."""
    if not GOOGLE_ADS_SPEND_CSV_URL:
        return None
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(GOOGLE_ADS_SPEND_CSV_URL, timeout=30.0)
    resp.raise_for_status()
    total, seen = 0.0, False
    for line in resp.text.splitlines()[1:]:
        cells = next(csv.reader([line]), [])
        if len(cells) >= 2 and cells[0].strip()[:10] == day:
            total += _to_float(cells[1].strip().replace(",", "."))
            seen = True
    return total if seen else None


def _refunded_amount(order: dict) -> float:
    total = 0.0
    for refund in order.get("refunds") or []:
        for tx in refund.get("transactions") or []:
            if tx.get("kind") == "refund" and tx.get("status") == "success":
                total += _to_float(tx.get("amount"))
    return total


def calculate_profit(
    orders: List[dict],
    cj_orders: Dict[str, dict],
    usd_rate: float,
    ad_spend: float,
    fixed_costs: float,
    fee_percent: float,
    fee_fixed: float,
) -> dict:
    """Pure calculation: combine Shopify orders, CJ costs and ad spend into daily metrics."""
    rows: List[dict] = []
    for o in orders:
        if o.get("cancelled_at") or o.get("test"):
            continue
        gross    = _to_float(o.get("total_price"))
        tax      = _to_float(o.get("total_tax"))
        refunded = _refunded_amount(o)
        tax_ratio = tax / gross if gross else 0.0
        net_gross = max(gross - refunded, 0.0)                  # incl. VAT, after refunds
        net_ex_vat = net_gross * (1 - tax_ratio)
        fee = (gross * fee_percent / 100 + fee_fixed) if gross else 0.0

        key = _order_key(o.get("order_number") or o.get("name"))
        cj  = cj_orders.get(key) or cj_orders.get(_order_key(o.get("id")))
        cj_cancelled = bool(cj) and str(cj.get("orderStatus", "")).upper() == "CANCELLED"
        rows.append({
            "order": o.get("name"),
            "gross_incl_vat": gross,
            "vat": tax,
            "refunded": refunded,
            "net_revenue_ex_vat": net_ex_vat,
            "revenue_before_refunds_ex_vat": gross * (1 - tax_ratio),
            "payment_fee": fee,
            "cj_found": bool(cj),
            "cj_product": 0.0 if cj_cancelled or not cj else _to_float(cj.get("productAmount")) * usd_rate,
            "cj_shipping": 0.0 if cj_cancelled or not cj else _to_float(cj.get("postageAmount")) * usd_rate,
            "cj_total_reported": None if not cj else _to_float(cj.get("orderAmount")) * usd_rate,
            "cj_status": cj.get("orderStatus") if cj else None,
            "cj_cancelled": cj_cancelled,
        })

    # Prefer orderAmount when product/postage split is missing.
    for r in rows:
        if r["cj_found"] and not r["cj_cancelled"] and not (r["cj_product"] or r["cj_shipping"]):
            r["cj_product"] = r["cj_total_reported"] or 0.0
        r["cj_cost"] = r["cj_product"] + r["cj_shipping"]
        r["cj_estimated"] = False

    # Estimate CJ cost for orders not (yet) in CJ, using the cost ratio of matched orders
    # (based on revenue before refunds, since CJ charges for the full order).
    matched = [r for r in rows if r["cj_found"] and not r["cj_cancelled"]]
    match_rev = sum(r["revenue_before_refunds_ex_vat"] for r in matched)
    cost_ratio = (sum(r["cj_cost"] for r in matched) / match_rev) if match_rev else None
    for r in rows:
        r["cj_estimated_cost"] = 0.0
        if not r["cj_found"] and cost_ratio is not None:
            r["cj_estimated_cost"] = r["revenue_before_refunds_ex_vat"] * cost_ratio
            r["cj_cost"] = r["cj_estimated_cost"]
            r["cj_estimated"] = True
        r["profit_before_ads"] = r["net_revenue_ex_vat"] - r["cj_cost"] - r["payment_fee"]

    def total(field: str) -> float:
        return round(sum(r[field] for r in rows), 2)

    gross        = total("gross_incl_vat")
    refunded     = total("refunded")
    net_revenue  = total("net_revenue_ex_vat")
    cogs         = total("cj_cost")
    fees         = total("payment_fee")
    before_ads   = round(net_revenue - cogs - fees, 2)
    net_profit   = round(before_ads - ad_spend - fixed_costs, 2)
    revenue_after_refunds = round(gross - refunded, 2)

    def ratio(a: float, b: float) -> Optional[float]:
        return round(a / b, 2) if b else None

    missing = [r["order"] for r in rows if not r["cj_found"]]
    return {
        "orders": len(rows),
        "revenue_incl_vat": gross,
        "refunds": refunded,
        "vat": round(sum(r["vat"] for r in rows), 2),
        "net_revenue_ex_vat": net_revenue,
        "cj_product_cost": total("cj_product"),
        "cj_shipping_cost": total("cj_shipping"),
        "cj_estimated_cost": total("cj_estimated_cost"),
        "cj_cost_total": cogs,
        "payment_fees": fees,
        "profit_before_ads": before_ads,
        "ad_spend": round(ad_spend, 2),
        "fixed_costs": round(fixed_costs, 2),
        "net_profit": net_profit,
        "net_margin_pct": round(net_profit / net_revenue * 100, 1) if net_revenue else None,
        "aov_incl_vat": ratio(gross, len(rows)),
        # ROAS on revenue incl. VAT (what Google Ads reports as conversion value).
        "roas": ratio(revenue_after_refunds, ad_spend),
        "break_even_roas": ratio(revenue_after_refunds, before_ads) if before_ads > 0 else None,
        "poas": ratio(before_ads, ad_spend),
        "cost_per_order": ratio(ad_spend, len(rows)),
        "cj_orders_matched": len(rows) - len(missing),
        "cj_orders_estimated": [r["order"] for r in rows if r["cj_estimated"]],
        "cj_orders_missing_no_estimate": [r["order"] for r in rows if not r["cj_found"] and not r["cj_estimated"]],
        "order_details": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in rows
        ],
    }


class DailyProfitInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    date: Optional[str] = Field(
        default=None,
        description="Day to report, YYYY-MM-DD in the shop's timezone. Defaults to yesterday.",
    )
    ad_spend: Optional[float] = Field(
        default=None, ge=0,
        description="Google Ads spend for the day in shop currency. Overrides the Google Sheet value if given.",
    )
    fixed_costs: Optional[float] = Field(
        default=None, ge=0,
        description="Fixed costs to allocate to this day (apps, subscriptions). Defaults to DAILY_FIXED_COSTS.",
    )
    include_orders: bool = Field(default=False, description="Include a per-order breakdown")

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            from datetime import date as _date
            _date.fromisoformat(v)
        return v


@mcp.tool(
    name="daily_profit_report",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def daily_profit_report(params: DailyProfitInput) -> str:
    """Daily profit & ROAS report: Shopify revenue (excl. VAT, minus refunds), CJ Dropshipping
    product + shipping costs, payment fees, Google Ads spend and fixed costs.
    Returns net profit, margin, ROAS, break-even ROAS and POAS for one day."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    try:
        shop     = (await _request("GET", "shop.json")).get("shop", {})
        tz       = ZoneInfo(shop.get("iana_timezone") or "Europe/Brussels")
        currency = shop.get("currency") or "EUR"

        day = params.date or (datetime.now(tz).date() - timedelta(days=1)).isoformat()
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        end   = start + timedelta(days=1)

        orders = await _shopify_get_all("orders.json", {
            "status": "any",
            "limit": 250,
            "created_at_min": start.isoformat(),
            "created_at_max": (end - timedelta(seconds=1)).isoformat(),
            "fields": "id,name,order_number,total_price,total_tax,cancelled_at,test,refunds,currency",
        }, "orders")

        warnings: List[str] = []
        cj_orders: Dict[str, dict] = {}
        usd_rate = 1.0
        if orders:
            if CJ_API_KEY:
                keys = {_order_key(o.get("order_number") or o.get("name")) for o in orders}
                oldest = (start - timedelta(days=3)).date().isoformat()
                cj_orders = await _cj_costs_for(keys, oldest)
                usd_rate = await _usd_rate(currency, day)
            else:
                warnings.append("CJ_API_KEY not set — CJ costs are 0, profit is overstated.")

        ad_spend = params.ad_spend
        if ad_spend is None:
            ad_spend = await _google_ads_spend(day)
            if ad_spend is None:
                ad_spend = 0.0
                warnings.append(
                    "No Google Ads spend found for this day — pass ad_spend or set GOOGLE_ADS_SPEND_CSV_URL."
                )
        fixed = DAILY_FIXED_COSTS if params.fixed_costs is None else params.fixed_costs

        report = calculate_profit(
            orders, cj_orders, usd_rate, ad_spend, fixed, PAYMENT_FEE_PERCENT, PAYMENT_FEE_FIXED
        )
        if report["cj_orders_estimated"]:
            warnings.append(
                f"{len(report['cj_orders_estimated'])} order(s) not found in CJ yet — CJ cost estimated "
                "from the cost ratio of matched orders."
            )
        if report["cj_orders_missing_no_estimate"]:
            warnings.append(
                f"{len(report['cj_orders_missing_no_estimate'])} order(s) have no CJ cost and could not be estimated."
            )
        if not params.include_orders:
            report.pop("order_details")

        return _fmt({
            "date": day,
            "currency": currency,
            "usd_to_shop_rate": round(usd_rate, 4),
            **report,
            "warnings": warnings,
        })
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
