"""Monarch Money MCP Server - Main server implementation."""

import os
import sys
import logging
import asyncio
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, date
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from mcp.server.auth.provider import AccessTokenT
from mcp.server.fastmcp import FastMCP
import mcp.types as types
from monarchmoney import MonarchMoney, RequireMFAException
from monarchmoney.monarchmoney import MonarchMoneyEndpoints

# The library defaults to api.monarchmoney.com which Cloudflare blocks for non-browser clients.
# api.monarch.com is the correct endpoint used by the Monarch web app.
MonarchMoneyEndpoints.BASE_URL = "https://api.monarch.com"

from pydantic import BaseModel, Field
from monarch_mcp_server.secure_session import secure_session

# Configure logging - must use stderr to avoid corrupting MCP stdio transport on stdout
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize FastMCP server
mcp = FastMCP("Monarch Money MCP Server")


def run_async(coro):
    """Run async function in a new thread with its own event loop."""

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with ThreadPoolExecutor() as executor:
        future = executor.submit(_run)
        return future.result()


class MonarchConfig(BaseModel):
    """Configuration for Monarch Money connection."""

    email: Optional[str] = Field(default=None, description="Monarch Money email")
    password: Optional[str] = Field(default=None, description="Monarch Money password")
    session_file: str = Field(
        default="monarch_session.json", description="Session file path"
    )



async def get_monarch_client() -> MonarchMoney:
    """Get or create MonarchMoney client instance using secure session storage."""
    # Try to get authenticated client from secure session
    client = secure_session.get_authenticated_client()

    if client is not None:
        logger.info("✅ Using authenticated client from secure keyring storage")
        return client

    # If no secure session, try environment credentials
    email = os.getenv("MONARCH_EMAIL")
    password = os.getenv("MONARCH_PASSWORD")

    if email and password:
        try:
            client = MonarchMoney()
            await client.login(email, password)
            logger.info(
                "Successfully logged into Monarch Money with environment credentials"
            )

            # Save the session securely
            secure_session.save_authenticated_session(client)

            return client
        except Exception as e:
            logger.error(f"Failed to login to Monarch Money: {e}")
            raise

    raise RuntimeError("🔐 Authentication needed! Run: python login_setup.py")


@mcp.tool()
def setup_authentication() -> str:
    """Get instructions for setting up secure authentication with Monarch Money."""
    return """🔐 Monarch Money - One-Time Setup

1️⃣ Open Terminal and run:
   python login_setup.py

2️⃣ Enter your Monarch Money credentials when prompted
   • Email and password
   • 2FA code if you have MFA enabled

3️⃣ Session will be saved automatically and last for weeks

4️⃣ Start using Monarch tools in Claude Desktop:
   • get_accounts - View all accounts
   • get_transactions - Recent transactions
   • get_budgets - Budget information

✅ Session persists across Claude restarts
✅ No need to re-authenticate frequently
✅ All credentials stay secure in terminal"""


@mcp.tool()
def check_auth_status() -> str:
    """Check if already authenticated with Monarch Money."""
    try:
        # Check if we have a token in the keyring
        token = secure_session.load_token()
        if token:
            status = "✅ Authentication token found in secure keyring storage\n"
        else:
            status = "❌ No authentication token found in keyring\n"

        email = os.getenv("MONARCH_EMAIL")
        if email:
            status += f"📧 Environment email: {email}\n"

        status += (
            "\n💡 Try get_accounts to test connection or run login_setup.py if needed."
        )

        return status
    except Exception as e:
        return f"Error checking auth status: {str(e)}"


@mcp.tool()
def debug_session_loading() -> str:
    """Debug keyring session loading issues."""
    try:
        # Check keyring access
        token = secure_session.load_token()
        if token:
            return f"✅ Token found in keyring (length: {len(token)})"
        else:
            return "❌ No token found in keyring. Run login_setup.py to authenticate."
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        return f"❌ Keyring access failed:\nError: {str(e)}\nType: {type(e)}\nTraceback:\n{error_details}"


@mcp.tool()
def get_accounts() -> str:
    """Get all financial accounts from Monarch Money."""
    try:

        async def _get_accounts():
            client = await get_monarch_client()
            return await client.get_accounts()

        accounts = run_async(_get_accounts())

        # Format accounts for display
        account_list = []
        for account in accounts.get("accounts", []):
            account_info = {
                "id": account.get("id"),
                "name": account.get("displayName") or account.get("name"),
                "type": (account.get("type") or {}).get("name"),
                "balance": account.get("currentBalance"),
                "institution": (account.get("institution") or {}).get("name"),
                "is_active": account.get("isActive")
                if "isActive" in account
                else not account.get("deactivatedAt"),
            }
            account_list.append(account_info)

        return json.dumps(account_list, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get accounts: {e}")
        return f"Error getting accounts: {str(e)}"


@mcp.tool()
def get_transactions(
    limit: int = 100,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    account_ids: Optional[List[str]] = None,
    category_ids: Optional[List[str]] = None,
    tag_ids: Optional[List[str]] = None,
    has_notes: Optional[bool] = None,
    is_recurring: Optional[bool] = None,
    hidden_from_reports: Optional[bool] = None,
) -> str:
    """
    Get transactions from Monarch Money.

    Args:
        limit: Number of transactions to retrieve (default: 100)
        offset: Number of transactions to skip (default: 0)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        search: Search string to filter transactions by merchant name, description, etc.
        account_ids: List of account IDs to filter by
        category_ids: List of category IDs to filter by
        tag_ids: List of tag IDs to filter by
        has_notes: Filter for transactions with/without notes
        is_recurring: Filter for recurring transactions
        hidden_from_reports: Filter for transactions hidden from reports
    """
    try:

        async def _get_transactions():
            client = await get_monarch_client()

            kwargs: dict = {}
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            if search:
                kwargs["search"] = search
            if account_ids:
                kwargs["account_ids"] = account_ids
            if category_ids:
                kwargs["category_ids"] = category_ids
            if tag_ids:
                kwargs["tag_ids"] = tag_ids
            if has_notes is not None:
                kwargs["has_notes"] = has_notes
            if is_recurring is not None:
                kwargs["is_recurring"] = is_recurring
            if hidden_from_reports is not None:
                kwargs["hidden_from_reports"] = hidden_from_reports

            return await client.get_transactions(limit=limit, offset=offset, **kwargs)

        transactions = run_async(_get_transactions())

        # Format transactions for display
        transaction_list = []
        for txn in transactions.get("allTransactions", {}).get("results", []):
            transaction_info = {
                "id": txn.get("id"),
                "date": txn.get("date"),
                "amount": txn.get("amount"),
                "description": txn.get("description"),
                "category": txn.get("category", {}).get("name")
                if txn.get("category")
                else None,
                "account": txn.get("account", {}).get("displayName"),
                "merchant": txn.get("merchant", {}).get("name")
                if txn.get("merchant")
                else None,
                "is_pending": txn.get("isPending", False),
            }
            transaction_list.append(transaction_info)

        return json.dumps(transaction_list, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transactions: {e}")
        return f"Error getting transactions: {str(e)}"


@mcp.tool()
def get_budgets() -> str:
    """Get budget information from Monarch Money."""
    try:

        async def _get_budgets():
            client = await get_monarch_client()
            return await client.get_budgets()

        budgets = run_async(_get_budgets())

        # Format budgets for display
        budget_list = []
        for budget in budgets.get("budgets", []):
            budget_info = {
                "id": budget.get("id"),
                "name": budget.get("name"),
                "amount": budget.get("amount"),
                "spent": budget.get("spent"),
                "remaining": budget.get("remaining"),
                "category": budget.get("category", {}).get("name"),
                "period": budget.get("period"),
            }
            budget_list.append(budget_info)

        return json.dumps(budget_list, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get budgets: {e}")
        return f"Error getting budgets: {str(e)}"


@mcp.tool()
def get_cashflow(
    start_date: Optional[str] = None, end_date: Optional[str] = None
) -> str:
    """
    Get cashflow analysis from Monarch Money.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    try:

        async def _get_cashflow():
            client = await get_monarch_client()

            filters = {}
            if start_date:
                filters["start_date"] = start_date
            if end_date:
                filters["end_date"] = end_date

            return await client.get_cashflow(**filters)

        cashflow = run_async(_get_cashflow())

        return json.dumps(cashflow, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get cashflow: {e}")
        return f"Error getting cashflow: {str(e)}"


@mcp.tool()
def get_account_holdings(account_id: str) -> str:
    """
    Get investment holdings for a specific account.

    Args:
        account_id: The ID of the investment account
    """
    try:

        async def _get_holdings():
            client = await get_monarch_client()
            return await client.get_account_holdings(account_id)

        holdings = run_async(_get_holdings())

        return json.dumps(holdings, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get account holdings: {e}")
        return f"Error getting account holdings: {str(e)}"


@mcp.tool()
def create_transaction(
    account_id: str,
    amount: float,
    description: str,
    date: str,
    category_id: Optional[str] = None,
    merchant_name: Optional[str] = None,
) -> str:
    """
    Create a new transaction in Monarch Money.

    Args:
        account_id: The account ID to add the transaction to
        amount: Transaction amount (positive for income, negative for expenses)
        description: Transaction description
        date: Transaction date in YYYY-MM-DD format
        category_id: Optional category ID
        merchant_name: Optional merchant name
    """
    try:

        async def _create_transaction():
            client = await get_monarch_client()

            transaction_data = {
                "account_id": account_id,
                "amount": amount,
                "description": description,
                "date": date,
            }

            if category_id:
                transaction_data["category_id"] = category_id
            if merchant_name:
                transaction_data["merchant_name"] = merchant_name

            return await client.create_transaction(**transaction_data)

        result = run_async(_create_transaction())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to create transaction: {e}")
        return f"Error creating transaction: {str(e)}"


@mcp.tool()
def update_transaction(
    transaction_id: str,
    amount: Optional[float] = None,
    category_id: Optional[str] = None,
    merchant_name: Optional[str] = None,
    notes: Optional[str] = None,
    date: Optional[str] = None,
    hide_from_reports: Optional[bool] = None,
    needs_review: Optional[bool] = None,
) -> str:
    """
    Update an existing transaction in Monarch Money.

    Args:
        transaction_id: The ID of the transaction to update
        amount: New transaction amount
        category_id: New category ID
        merchant_name: New merchant name
        notes: Notes to attach to the transaction
        date: New transaction date in YYYY-MM-DD format
        hide_from_reports: Whether to hide from reports
        needs_review: Whether the transaction needs review
    """
    try:

        async def _update_transaction():
            client = await get_monarch_client()

            update_data = {"transaction_id": transaction_id}

            if amount is not None:
                update_data["amount"] = amount
            if category_id is not None:
                update_data["category_id"] = category_id
            if merchant_name is not None:
                update_data["merchant_name"] = merchant_name
            if notes is not None:
                update_data["notes"] = notes
            if date is not None:
                update_data["date"] = date
            if hide_from_reports is not None:
                update_data["hide_from_reports"] = hide_from_reports
            if needs_review is not None:
                update_data["needs_review"] = needs_review

            return await client.update_transaction(**update_data)

        result = run_async(_update_transaction())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to update transaction: {e}")
        return f"Error updating transaction: {str(e)}"


@mcp.tool()
def refresh_accounts() -> str:
    """Request account data refresh from financial institutions."""
    try:

        async def _refresh_accounts():
            client = await get_monarch_client()
            return await client.request_accounts_refresh()

        result = run_async(_refresh_accounts())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to refresh accounts: {e}")
        return f"Error refreshing accounts: {str(e)}"


# ============================================================================
# NEW TOOLS - Account & Institution Data
# ============================================================================


@mcp.tool()
def get_account_history(
    account_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Get daily balance history for a specific account.

    Args:
        account_id: The unique identifier for the account
        start_date: Start date (YYYY-MM-DD). Defaults to 30 days ago
        end_date: End date (YYYY-MM-DD). Defaults to today
    """
    try:

        async def _get_account_history():
            client = await get_monarch_client()
            kwargs = {}
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            return await client.get_account_history(account_id, **kwargs)

        result = run_async(_get_account_history())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get account history: {e}")
        return f"Error getting account history: {str(e)}"


@mcp.tool()
def get_account_type_options() -> str:
    """
    Get all available account types and subtypes in Monarch Money.
    Useful for account creation and understanding account categorization.
    """
    try:

        async def _get_account_type_options():
            client = await get_monarch_client()
            return await client.get_account_type_options()

        result = run_async(_get_account_type_options())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get account type options: {e}")
        return f"Error getting account type options: {str(e)}"


@mcp.tool()
def get_institutions() -> str:
    """
    Get all financial institutions linked to your Monarch Money account.
    Returns institution details including connection status and last sync time.
    """
    try:

        async def _get_institutions():
            client = await get_monarch_client()
            return await client.get_institutions()

        result = run_async(_get_institutions())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get institutions: {e}")
        return f"Error getting institutions: {str(e)}"


@mcp.tool()
def get_subscription_details() -> str:
    """
    Get Monarch Money subscription status including plan type and expiration.
    """
    try:

        async def _get_subscription_details():
            client = await get_monarch_client()
            return await client.get_subscription_details()

        result = run_async(_get_subscription_details())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get subscription details: {e}")
        return f"Error getting subscription details: {str(e)}"


@mcp.tool()
def is_accounts_refresh_complete() -> str:
    """
    Check if a running account refresh operation is complete.
    Use this after calling refresh_accounts to poll for completion status.
    """
    try:

        async def _is_accounts_refresh_complete():
            client = await get_monarch_client()
            return await client.is_accounts_refresh_complete()

        result = run_async(_is_accounts_refresh_complete())

        return json.dumps({"refresh_complete": result}, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to check refresh status: {e}")
        return f"Error checking refresh status: {str(e)}"


# ============================================================================
# NEW TOOLS - Transaction Management
# ============================================================================


@mcp.tool()
def get_transaction_details(transaction_id: str) -> str:
    """
    Get comprehensive details for a single transaction including all metadata.

    Args:
        transaction_id: The unique identifier for the transaction
    """
    try:

        async def _get_transaction_details():
            client = await get_monarch_client()
            return await client.get_transaction_details(transaction_id)

        result = run_async(_get_transaction_details())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction details: {e}")
        return f"Error getting transaction details: {str(e)}"


@mcp.tool()
def get_transaction_splits(transaction_id: str) -> str:
    """
    Get split information for a transaction divided across multiple categories.

    Args:
        transaction_id: The unique identifier for the transaction
    """
    try:

        async def _get_transaction_splits():
            client = await get_monarch_client()
            return await client.get_transaction_splits(transaction_id)

        result = run_async(_get_transaction_splits())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction splits: {e}")
        return f"Error getting transaction splits: {str(e)}"


@mcp.tool()
def update_transaction_splits(transaction_id: str, splits: str) -> str:
    """
    Split a transaction across multiple categories or modify existing splits.

    Args:
        transaction_id: The transaction to split
        splits: JSON array of split objects. Each object should have:
                - category_id (string): Category ID for this split
                - amount (number): Amount for this split (positive value)
                - merchant_name (string, optional): Merchant name
                - notes (string, optional): Notes for this split

    Example splits: '[{"category_id": "cat123", "amount": 50.00}, {"category_id": "cat456", "amount": 25.00}]'

    Note: Sum of split amounts must equal the original transaction amount.
    """
    try:
        # Parse the splits JSON
        split_data = json.loads(splits)

        async def _update_transaction_splits():
            client = await get_monarch_client()
            return await client.update_transaction_splits(transaction_id, split_data)

        result = run_async(_update_transaction_splits())

        return json.dumps(result, indent=2, default=str)
    except json.JSONDecodeError as e:
        return f"Error parsing splits JSON: {str(e)}. Please provide valid JSON array."
    except Exception as e:
        logger.error(f"Failed to update transaction splits: {e}")
        return f"Error updating transaction splits: {str(e)}"


@mcp.tool()
def get_transactions_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Get aggregated transaction summary data (totals by category, merchant, etc.).

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    """
    try:

        async def _get_transactions_summary():
            client = await get_monarch_client()
            kwargs = {}
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            return await client.get_transactions_summary(**kwargs)

        result = run_async(_get_transactions_summary())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transactions summary: {e}")
        return f"Error getting transactions summary: {str(e)}"


@mcp.tool()
def get_recurring_transactions() -> str:
    """
    Get all recurring/scheduled transactions with frequency, next occurrence, and merchant details.
    Useful for tracking subscriptions and upcoming bills.
    """
    try:

        async def _get_recurring_transactions():
            client = await get_monarch_client()
            return await client.get_recurring_transactions()

        result = run_async(_get_recurring_transactions())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get recurring transactions: {e}")
        return f"Error getting recurring transactions: {str(e)}"


# ============================================================================
# NEW TOOLS - Categories & Tags
# ============================================================================


@mcp.tool()
def get_transaction_categories() -> str:
    """
    Get all transaction categories configured in the account.
    Returns category IDs, names, icons, and whether they are system or custom categories.
    """
    try:

        async def _get_transaction_categories():
            client = await get_monarch_client()
            return await client.get_transaction_categories()

        result = run_async(_get_transaction_categories())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction categories: {e}")
        return f"Error getting transaction categories: {str(e)}"


@mcp.tool()
def get_transaction_category_groups() -> str:
    """
    Get all category groups (parent groupings for categories).
    Returns group IDs, names, and associated category information.
    """
    try:

        async def _get_transaction_category_groups():
            client = await get_monarch_client()
            return await client.get_transaction_category_groups()

        result = run_async(_get_transaction_category_groups())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction category groups: {e}")
        return f"Error getting transaction category groups: {str(e)}"


@mcp.tool()
def create_category_group(
    name: str,
    group_type: str = "expense",
    group_level_budgeting: bool = False,
) -> str:
    """
    Create a new category group (parent grouping for categories).

    Args:
        name: Group name (e.g. "Subscriptions")
        group_type: "expense", "income", or "transfer"
        group_level_budgeting: If true, budget is set at the group level instead of per-category
    """
    try:

        async def _create_category_group():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                mutation Common_CreateCategoryGroup($input: CreateCategoryGroupInput!) {
                    createCategoryGroup(input: $input) {
                        categoryGroup {
                            id
                            name
                            order
                            type
                            color
                            groupLevelBudgetingEnabled
                            budgetVariability
                            rolloverPeriod {
                                id
                                startMonth
                                endMonth
                                startingBalance
                                __typename
                            }
                            __typename
                        }
                        __typename
                    }
                }
            """)

            from datetime import datetime
            variables = {
                "input": {
                    "name": name,
                    "type": group_type,
                    "groupLevelBudgetingEnabled": group_level_budgeting,
                    "rolloverEnabled": False,
                    "rolloverStartMonth": datetime.today().replace(day=1).strftime("%Y-%m-%d"),
                    "rolloverType": "monthly",
                }
            }

            return await client.gql_call(
                operation="Common_CreateCategoryGroup",
                graphql_query=query,
                variables=variables,
            )

        result = run_async(_create_category_group())
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to create category group: {e}")
        return f"Error creating category group: {str(e)}"


@mcp.tool()
def delete_category_group(
    group_id: str,
    move_to_group_id: Optional[str] = None,
) -> str:
    """
    Delete a category group. Optionally move its categories to another group.

    Args:
        group_id: The ID of the group to delete
        move_to_group_id: If provided, move categories from the deleted group to this group.
                          If not provided, categories in the group will be moved to "Other".
    """
    try:

        async def _delete_category_group():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                mutation Common_DeleteCategoryGroup($id: UUID!, $moveToGroupId: UUID) {
                    deleteCategoryGroup(id: $id, moveToGroupId: $moveToGroupId) {
                        deleted
                        errors {
                            fieldErrors {
                                field
                                messages
                                __typename
                            }
                            message
                            code
                            __typename
                        }
                        __typename
                    }
                }
            """)

            variables = {"id": group_id}
            if move_to_group_id:
                variables["moveToGroupId"] = move_to_group_id

            return await client.gql_call(
                operation="Common_DeleteCategoryGroup",
                graphql_query=query,
                variables=variables,
            )

        result = run_async(_delete_category_group())
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to delete category group: {e}")
        return f"Error deleting category group: {str(e)}"


@mcp.tool()
def create_transaction_rule(
    original_statement_contains: Optional[str] = None,
    merchant_name_exactly: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    is_expense: bool = True,
    account_ids: Optional[List[str]] = None,
    set_category_id: Optional[str] = None,
    set_merchant_name: Optional[str] = None,
    hide_from_reports: Optional[bool] = None,
    add_tag_ids: Optional[List[str]] = None,
    set_review_status: Optional[str] = None,
    apply_to_existing: bool = True,
) -> str:
    """
    Create a Monarch Money transaction rule to auto-categorize future transactions.

    Use 'original_statement_contains' to match on the raw bank statement text (e.g. "WHOLEFDS" for Whole Foods).
    Use 'merchant_name_exactly' to match on the Monarch merchant name (e.g. "whole foods market").

    Args:
        original_statement_contains: Match if original bank statement contains this text (case-insensitive)
        merchant_name_exactly: Match if Monarch merchant name exactly equals this text
        amount_min: Minimum transaction amount (debit/expense by default). If only amount_min: "greater than". If both amount_min and amount_max: "between".
        amount_max: Maximum transaction amount. If only amount_max: "less than".
        is_expense: Whether amount criteria applies to expenses/debits (default True) or credits (False)
        account_ids: List of account IDs to restrict rule to specific accounts
        set_category_id: Category ID to assign to matched transactions
        set_merchant_name: Rename the merchant to this name (must be an existing Monarch merchant name)
        hide_from_reports: Whether to hide matched transactions from reports
        add_tag_ids: List of tag IDs to add to matched transactions (e.g. ["181126664071419049"] for Tax tag)
        set_review_status: Set review status: "needs_review" or "reviewed"
        apply_to_existing: Whether to apply the rule to existing transactions (default True)
    """
    try:
        assert original_statement_contains or merchant_name_exactly, \
            "Must provide either original_statement_contains or merchant_name_exactly"
        assert set_category_id or set_merchant_name or hide_from_reports is not None or add_tag_ids or set_review_status, \
            "Must provide at least one action"

        async def _create_rule():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                mutation Common_CreateTransactionRuleMutationV2($input: CreateTransactionRuleInput!) {
                    createTransactionRuleV2(input: $input) {
                        errors {
                            fieldErrors {
                                field
                                messages
                                __typename
                            }
                            message
                            code
                            __typename
                        }
                        __typename
                    }
                }
            """)

            input_data: dict = {
                "merchantCriteriaUseOriginalStatement": False,
                "applyToExistingTransactions": apply_to_existing,
                "actionSetBusinessEntityIsUnassigned": False,
                "categoryIds": None,
                "accountIds": account_ids or None,
                "merchantCriteria": None,
                "merchantNameCriteria": None,
                "amountCriteria": None,
                "addTagsAction": None,
                "splitTransactionsAction": None,
                "linkGoalAction": None,
                "linkSavingsGoalAction": None,
                "reviewStatusAction": set_review_status,
                "actionSetBusinessEntity": None,
            }

            if original_statement_contains:
                input_data["originalStatementCriteria"] = [
                    {"operator": "contains", "value": original_statement_contains}
                ]
            else:
                input_data["originalStatementCriteria"] = None

            if merchant_name_exactly:
                input_data["merchantCriteria"] = [
                    {"operator": "eq", "value": merchant_name_exactly.lower()}
                ]

            if amount_min is not None or amount_max is not None:
                if amount_min is not None and amount_max is not None:
                    input_data["amountCriteria"] = {
                        "operator": "between",
                        "isExpense": is_expense,
                        "value": None,
                        "valueRange": {"lower": amount_min, "upper": amount_max},
                    }
                elif amount_min is not None:
                    input_data["amountCriteria"] = {
                        "operator": "gt",
                        "isExpense": is_expense,
                        "value": amount_min,
                        "valueRange": None,
                    }
                else:
                    input_data["amountCriteria"] = {
                        "operator": "lt",
                        "isExpense": is_expense,
                        "value": amount_max,
                        "valueRange": None,
                    }

            if set_category_id:
                input_data["setCategoryAction"] = set_category_id
            else:
                input_data["setCategoryAction"] = None

            if set_merchant_name:
                input_data["setMerchantAction"] = set_merchant_name
            else:
                input_data["setMerchantAction"] = None

            if hide_from_reports is not None:
                input_data["setHideFromReportsAction"] = hide_from_reports

            if add_tag_ids:
                input_data["addTagsAction"] = add_tag_ids

            return await client.gql_call(
                operation="Common_CreateTransactionRuleMutationV2",
                graphql_query=query,
                variables={"input": input_data},
            )

        result = run_async(_create_rule())
        errors = result.get("createTransactionRuleV2", {}).get("errors")
        if errors:
            return f"Error creating rule: {errors}"
        return "Rule created successfully"
    except Exception as e:
        logger.error(f"Failed to create transaction rule: {e}")
        return f"Error creating transaction rule: {str(e)}"


@mcp.tool()
def create_transaction_category(
    name: str,
    group_id: Optional[str] = None,
    icon: Optional[str] = None,
) -> str:
    """
    Create a new custom category for transactions.

    Args:
        name: Category name (max 50 chars, must be unique)
        group_id: Optional parent category group ID
        icon: Optional icon identifier
    """
    try:

        async def _create_transaction_category():
            client = await get_monarch_client()
            if not group_id:
                raise ValueError("group_id is required to create a transaction category")
            kwargs = {
                "group_id": group_id,
                "transaction_category_name": name,
            }
            if icon:
                kwargs["icon"] = icon
            return await client.create_transaction_category(**kwargs)

        result = run_async(_create_transaction_category())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to create transaction category: {e}")
        return f"Error creating transaction category: {str(e)}"


@mcp.tool()
def update_transaction_category(
    category_id: str,
    name: Optional[str] = None,
    icon: Optional[str] = None,
) -> str:
    """
    Update an existing transaction category's name or icon.

    Args:
        category_id: The category ID to update (from get_transaction_categories)
        name: New category name (optional)
        icon: New emoji icon (optional, e.g. "🏛️" or "💰")
    """
    try:
        async def _update_category():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            mutation = gql_parse("""
                mutation Web_UpdateCategory($input: UpdateCategoryInput!) {
                    updateCategory(input: $input) {
                        errors {
                            fieldErrors { field messages __typename }
                            message
                            code
                            __typename
                        }
                        category {
                            id
                            name
                            icon
                            __typename
                        }
                        __typename
                    }
                }
            """)
            input_data: dict = {"id": category_id}
            if name is not None:
                input_data["name"] = name
            if icon is not None:
                input_data["icon"] = icon
            return await client.gql_call(
                operation="Web_UpdateCategory",
                graphql_query=mutation,
                variables={"input": input_data},
            )

        result = run_async(_update_category())
        errors = result.get("updateCategory", {}).get("errors")
        if errors:
            return f"Error updating category: {errors}"
        cat = result.get("updateCategory", {}).get("category", {})
        return f"Category updated: {cat.get('icon', '')} {cat.get('name', '')} (id: {cat.get('id', '')})"
    except Exception as e:
        logger.error(f"Failed to update transaction category: {e}")
        return f"Error updating transaction category: {str(e)}"


@mcp.tool()
def get_transaction_rules() -> str:
    """
    Get all transaction rules configured in the account.
    Returns rules with their conditions (original statement / merchant name) and actions (category, rename, hide).
    """
    try:
        async def _get_rules():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                query GetTransactionRules {
                    transactionRules {
                        id
                        order
                        merchantCriteriaUseOriginalStatement
                        merchantCriteria { operator value __typename }
                        merchantNameCriteria { operator value __typename }
                        originalStatementCriteria { operator value __typename }
                        setCategoryAction { id name icon __typename }
                        setMerchantAction { id name __typename }
                        setHideFromReportsAction
                        recentApplicationCount
                        lastAppliedAt
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="GetTransactionRules",
                graphql_query=query,
                variables={},
            )

        result = run_async(_get_rules())
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction rules: {e}")
        return f"Error getting transaction rules: {str(e)}"


@mcp.tool()
def delete_transaction_rule(rule_id: str) -> str:
    """
    Delete a Monarch Money transaction rule by ID.

    The rule ID can be obtained from get_transaction_rules.
    Deleting a rule does NOT affect existing transactions — only future auto-categorization.

    Args:
        rule_id: The rule ID to delete (from get_transaction_rules)
    """
    try:
        async def _delete_rule():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            mutation = gql_parse("""
                mutation Common_DeleteTransactionRule($id: ID!) {
                    deleteTransactionRule(id: $id) {
                        deleted
                        errors {
                            fieldErrors {
                                field
                                messages
                                __typename
                            }
                            message
                            code
                            __typename
                        }
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="Common_DeleteTransactionRule",
                graphql_query=mutation,
                variables={"id": rule_id},
            )

        result = run_async(_delete_rule())
        payload = result.get("deleteTransactionRule", {})
        errors = payload.get("errors")
        if errors:
            return f"Error deleting rule: {errors}"
        return f"Rule {rule_id} deleted successfully"
    except Exception as e:
        logger.error(f"Failed to delete transaction rule: {e}")
        return f"Error deleting transaction rule: {str(e)}"


@mcp.tool()
def update_transaction_rule(
    rule_id: str,
    original_statement_contains: Optional[str] = None,
    merchant_name_exactly: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    is_expense: bool = True,
    account_ids: Optional[List[str]] = None,
    set_category_id: Optional[str] = None,
    set_hide_from_reports: Optional[bool] = None,
    add_tag_ids: Optional[List[str]] = None,
    set_review_status: Optional[str] = None,
    apply_to_existing: bool = True,
) -> str:
    """
    Update an existing Monarch Money transaction rule.

    Pass only the fields you want to change. The rule ID comes from get_transaction_rules.
    To switch a rule from merchant-name matching to statement matching (or vice versa),
    pass the new criterion and leave the old one as None.

    Args:
        rule_id: The rule ID to update (from get_transaction_rules)
        original_statement_contains: Match on raw bank statement text (replaces existing criteria if set)
        merchant_name_exactly: Match on Monarch merchant name exactly (replaces existing criteria if set)
        amount_min: Minimum amount. amount_min only = "greater than"; both = "between"
        amount_max: Maximum amount. amount_max only = "less than"
        is_expense: Whether amount is expense/debit (default True)
        account_ids: Restrict rule to specific account IDs
        set_category_id: Category ID to assign to matched transactions
        set_hide_from_reports: Whether to hide matched transactions from reports
        add_tag_ids: List of tag IDs to add to matched transactions
        set_review_status: Set review status: "needs_review" or "reviewed"
        apply_to_existing: Apply the updated rule to existing transactions (default True)
    """
    try:
        async def _update_rule():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            # Fetch existing rule to use as defaults for fields not being updated
            fetch_query = gql_parse("""
                query GetTransactionRules {
                    transactionRules {
                        id
                        merchantCriteriaUseOriginalStatement
                        merchantCriteria { operator value }
                        merchantNameCriteria { operator value }
                        originalStatementCriteria { operator value }
                        setCategoryAction { id }
                        setMerchantAction { id }
                        setHideFromReportsAction
                    }
                }
            """)
            fetch_result = await client.gql_call(
                operation="GetTransactionRules",
                graphql_query=fetch_query,
                variables={},
            )
            existing = next(
                (r for r in fetch_result.get("transactionRules", []) if r["id"] == rule_id),
                None,
            )
            if existing is None:
                raise ValueError(f"Rule {rule_id} not found")

            mutation = gql_parse("""
                mutation Common_UpdateTransactionRuleMutationV2($input: UpdateTransactionRuleInput!) {
                    updateTransactionRuleV2(input: $input) {
                        errors {
                            fieldErrors {
                                field
                                messages
                                __typename
                            }
                            message
                            code
                            __typename
                        }
                        __typename
                    }
                }
            """)

            # Determine criteria: caller overrides take precedence, otherwise preserve existing
            if original_statement_contains is not None:
                use_original_statement = True
                orig_criteria = [{"operator": "contains", "value": original_statement_contains.lower()}]
                name_criteria = None
                merch_criteria = None
            elif merchant_name_exactly is not None:
                use_original_statement = False
                orig_criteria = None
                name_criteria = [{"operator": "eq", "value": merchant_name_exactly.lower()}]
                merch_criteria = None
            else:
                # Preserve existing criteria
                use_original_statement = existing.get("merchantCriteriaUseOriginalStatement", False)
                orig_criteria = [{"operator": c["operator"], "value": c["value"]}
                                  for c in (existing.get("originalStatementCriteria") or [])] or None
                name_criteria = [{"operator": c["operator"], "value": c["value"]}
                                  for c in (existing.get("merchantNameCriteria") or [])] or None
                merch_criteria = [{"operator": c["operator"], "value": c["value"]}
                                   for c in (existing.get("merchantCriteria") or [])] or None

            input_data: dict = {
                "id": rule_id,
                "merchantCriteriaUseOriginalStatement": use_original_statement,
                "merchantCriteria": merch_criteria,
                "merchantNameCriteria": name_criteria,
                "originalStatementCriteria": orig_criteria,
                "amountCriteria": None,
                "categoryIds": None,
                "accountIds": account_ids or None,
                "criteriaBusinessEntityIds": None,
                "criteriaBusinessEntityIsUnassigned": False,
                "setMerchantAction": existing.get("setMerchantAction", {}).get("id") if existing.get("setMerchantAction") else None,
                "setCategoryAction": set_category_id if set_category_id is not None else (existing.get("setCategoryAction") or {}).get("id"),
                "addTagsAction": add_tag_ids if add_tag_ids is not None else None,
                "linkGoalAction": None,
                "linkSavingsGoalAction": None,
                "reviewStatusAction": set_review_status,
                "splitTransactionsAction": None,
                "actionSetBusinessEntity": None,
                "actionSetBusinessEntityIsUnassigned": False,
                "setHideFromReportsAction": set_hide_from_reports if set_hide_from_reports is not None else existing.get("setHideFromReportsAction", False),
                "applyToExistingTransactions": apply_to_existing,
            }

            if amount_min is not None or amount_max is not None:
                if amount_min is not None and amount_max is not None:
                    input_data["amountCriteria"] = {
                        "operator": "between", "isExpense": is_expense,
                        "value": None, "valueRange": {"lower": amount_min, "upper": amount_max},
                    }
                elif amount_min is not None:
                    input_data["amountCriteria"] = {
                        "operator": "gt", "isExpense": is_expense,
                        "value": amount_min, "valueRange": None,
                    }
                else:
                    input_data["amountCriteria"] = {
                        "operator": "lt", "isExpense": is_expense,
                        "value": amount_max, "valueRange": None,
                    }

            return await client.gql_call(
                operation="Common_UpdateTransactionRuleMutationV2",
                graphql_query=mutation,
                variables={"input": input_data},
            )

        result = run_async(_update_rule())
        errors = result.get("updateTransactionRuleV2", {}).get("errors")
        if errors:
            return f"Error updating rule: {errors}"
        return f"Rule {rule_id} updated successfully"
    except Exception as e:
        logger.error(f"Failed to update transaction rule: {e}")
        return f"Error updating transaction rule: {str(e)}"


@mcp.tool()
def search_merchants(query: str, limit: int = 20) -> str:
    """
    Search for merchants by name. Returns matching merchants with their IDs and transaction counts.
    Useful for finding duplicate or variant merchants before merging.

    Args:
        query: Search string to match against merchant names
        limit: Max results to return (default 20)
    """
    try:
        async def _search():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            gql_query = gql_parse("""
                query Web_GetMerchantSettingsPage($offset: Int, $orderBy: MerchantOrdering, $search: String) {
                    merchants(offset: $offset, orderBy: $orderBy, search: $search) {
                        id
                        name
                        transactionCount
                        createdAt
                        logoUrl
                        __typename
                    }
                    merchantCount
                }
            """)
            return await client.gql_call(
                operation="Web_GetMerchantSettingsPage",
                graphql_query=gql_query,
                variables={"search": query, "orderBy": "TRANSACTION_COUNT", "offset": 0},
            )

        result = run_async(_search())
        merchants = result.get("merchants", [])[:limit]
        return json.dumps(merchants, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to search merchants: {e}")
        return f"Error searching merchants: {str(e)}"


@mcp.tool()
def update_merchant(
    merchant_id: str,
    name: str,
) -> str:
    """
    Rename a Monarch Money merchant. This renames the merchant globally —
    all transactions from this merchant will show the new name.

    Use get_transactions or get_transaction_details to find the merchant ID.

    Args:
        merchant_id: The Monarch merchant ID (from transaction merchant.id)
        name: New display name for the merchant
    """
    try:
        async def _update_merchant():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                mutation Common_UpdateMerchant($input: UpdateMerchantInput!) {
                    updateMerchant(input: $input) {
                        merchant {
                            id
                            name
                            __typename
                        }
                        errors {
                            fieldErrors { field messages __typename }
                            message
                            code
                            __typename
                        }
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="Common_UpdateMerchant",
                graphql_query=query,
                variables={
                    "input": {
                        "merchantId": merchant_id,
                        "name": name,
                        "recurrence": {"isRecurring": False, "amount": 0, "isActive": True},
                    }
                },
            )

        result = run_async(_update_merchant())
        errors = result.get("updateMerchant", {}).get("errors")
        if errors:
            return f"Error updating merchant: {errors}"
        new_name = result.get("updateMerchant", {}).get("merchant", {}).get("name")
        return f"Merchant renamed to '{new_name}'"
    except Exception as e:
        logger.error(f"Failed to update merchant: {e}")
        return f"Error updating merchant: {str(e)}"


@mcp.tool()
def merge_merchants(
    source_merchant_id: str,
    target_merchant_id: str,
) -> str:
    """
    Merge one merchant into another, moving all transactions to the target merchant,
    then deleting the source merchant.

    Use this to consolidate duplicate merchants (e.g. "Starbucks Coffee"
    into "Starbucks").

    Args:
        source_merchant_id: The merchant ID to delete (transactions will be moved away from this)
        target_merchant_id: The merchant ID to merge into (transactions will be moved here)
    """
    try:
        async def _merge():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            query = gql_parse("""
                mutation Common_DeleteMerchant($merchantId: ID!, $moveToId: ID) {
                    deleteMerchant(id: $merchantId, moveRelationsToMerchantId: $moveToId) {
                        success
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="Common_DeleteMerchant",
                graphql_query=query,
                variables={
                    "merchantId": source_merchant_id,
                    "moveToId": target_merchant_id,
                },
            )

        result = run_async(_merge())
        success = result.get("deleteMerchant", {}).get("success")
        assert success, f"Merge failed: {result}"
        return f"Merged merchant {source_merchant_id} into {target_merchant_id} successfully"
    except Exception as e:
        logger.error(f"Failed to merge merchants: {e}")
        return f"Error merging merchants: {str(e)}"


@mcp.tool()
def get_transaction_tags() -> str:
    """
    Get all tags configured in the account.
    Tags are user-defined labels that can be applied to any transaction.
    """
    try:

        async def _get_transaction_tags():
            client = await get_monarch_client()
            return await client.get_transaction_tags()

        result = run_async(_get_transaction_tags())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get transaction tags: {e}")
        return f"Error getting transaction tags: {str(e)}"


@mcp.tool()
def create_transaction_tag(
    name: str,
    color: Optional[str] = None,
) -> str:
    """
    Create a new tag for transactions.

    Args:
        name: Tag name (max 30 chars)
        color: Optional hex color code
    """
    try:

        async def _create_transaction_tag():
            client = await get_monarch_client()
            kwargs = {"name": name}
            if color:
                kwargs["color"] = color
            return await client.create_transaction_tag(**kwargs)

        result = run_async(_create_transaction_tag())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to create transaction tag: {e}")
        return f"Error creating transaction tag: {str(e)}"


@mcp.tool()
def update_transaction_tag(
    tag_id: str,
    name: str,
    color: str,
) -> str:
    """
    Update an existing transaction tag's name or color.

    Args:
        tag_id: The tag ID to update (from get_transaction_tags)
        name: New tag name
        color: New hex color code (e.g. "#FF5733")
    """
    try:
        async def _update_tag():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            mutation = gql_parse("""
                mutation Common_UpdateTransactionTag($input: UpdateTransactionTagInput!) {
                    updateTransactionTag(input: $input) {
                        tag {
                            id
                            name
                            color
                            order
                            __typename
                        }
                        errors {
                            message
                            __typename
                        }
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="Common_UpdateTransactionTag",
                graphql_query=mutation,
                variables={"input": {"id": tag_id, "name": name, "color": color}},
            )

        result = run_async(_update_tag())
        errors = result.get("updateTransactionTag", {}).get("errors")
        if errors:
            return f"Error updating tag: {errors}"
        tag = result.get("updateTransactionTag", {}).get("tag", {})
        return f"Tag updated: {tag.get('name')} ({tag.get('color')})"
    except Exception as e:
        logger.error(f"Failed to update transaction tag: {e}")
        return f"Error updating transaction tag: {str(e)}"


@mcp.tool()
def delete_transaction_tag(tag_id: str) -> str:
    """
    Delete a transaction tag by ID. This removes the tag from all transactions.

    Args:
        tag_id: The tag ID to delete (from get_transaction_tags)
    """
    try:
        async def _delete_tag():
            client = await get_monarch_client()
            from gql import gql as gql_parse

            mutation = gql_parse("""
                mutation Common_DeleteTransactionTag($id: ID!) {
                    deleteTransactionTag(id: $id) {
                        deleted
                        errors {
                            message
                            __typename
                        }
                        __typename
                    }
                }
            """)
            return await client.gql_call(
                operation="Common_DeleteTransactionTag",
                graphql_query=mutation,
                variables={"id": tag_id},
            )

        result = run_async(_delete_tag())
        errors = result.get("deleteTransactionTag", {}).get("errors")
        if errors:
            return f"Error deleting tag: {errors}"
        return f"Tag {tag_id} deleted successfully"
    except Exception as e:
        logger.error(f"Failed to delete transaction tag: {e}")
        return f"Error deleting transaction tag: {str(e)}"


@mcp.tool()
def set_transaction_tags(transaction_id: str, tag_ids: str) -> str:
    """
    Apply one or more tags to a transaction.

    Args:
        transaction_id: The transaction to tag
        tag_ids: JSON array of tag IDs to apply. Example: '["tag123", "tag456"]'

    Note: This replaces existing tags (not additive). Empty array removes all tags.
    """
    try:
        # Parse the tag_ids JSON
        tag_id_list = json.loads(tag_ids)

        async def _set_transaction_tags():
            client = await get_monarch_client()
            return await client.set_transaction_tags(transaction_id, tag_id_list)

        result = run_async(_set_transaction_tags())

        return json.dumps(result, indent=2, default=str)
    except json.JSONDecodeError as e:
        return f"Error parsing tag_ids JSON: {str(e)}. Please provide valid JSON array."
    except Exception as e:
        logger.error(f"Failed to set transaction tags: {e}")
        return f"Error setting transaction tags: {str(e)}"


# ============================================================================
# NEW TOOLS - Budgets
# ============================================================================


@mcp.tool()
def set_budget_amount(
    category_id: str,
    amount: float,
    month: Optional[str] = None,
    apply_to_future: Optional[bool] = None,
) -> str:
    """
    Set or update a budget amount for a specific category and month.

    Args:
        category_id: The category to budget
        amount: Budget amount (0 to clear/unset the budget)
        month: Month in YYYY-MM format. Defaults to current month
        apply_to_future: If true, apply to this and all future months
    """
    try:

        async def _set_budget_amount():
            client = await get_monarch_client()
            kwargs = {
                "category_id": category_id,
            }
            if month:
                # Convert YYYY-MM to start_date format expected by API
                kwargs["start_date"] = f"{month}-01"
            if apply_to_future is not None:
                kwargs["apply_to_future"] = apply_to_future
            return await client.set_budget_amount(amount, **kwargs)

        result = run_async(_set_budget_amount())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to set budget amount: {e}")
        return f"Error setting budget amount: {str(e)}"


@mcp.tool()
def get_cashflow_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Get high-level cashflow metrics (income, expenses, savings, savings rate).

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    """
    try:

        async def _get_cashflow_summary():
            client = await get_monarch_client()
            kwargs = {}
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            return await client.get_cashflow_summary(**kwargs)

        result = run_async(_get_cashflow_summary())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to get cashflow summary: {e}")
        return f"Error getting cashflow summary: {str(e)}"


# ============================================================================
# NEW TOOLS - Account Management
# ============================================================================


@mcp.tool()
def create_manual_account(
    name: str,
    account_type: str,
    balance: float,
    account_subtype: Optional[str] = None,
    include_in_net_worth: bool = True,
) -> str:
    """
    Create a new manual (non-linked) account for tracking assets or liabilities.

    Args:
        name: Account name
        account_type: Type from get_account_type_options (e.g., "depository", "investment", "loan")
        balance: Starting balance
        account_subtype: Subtype (e.g., "checking", "savings", "brokerage")
        include_in_net_worth: Include in net worth calculations (default: true)
    """
    try:

        async def _create_manual_account():
            client = await get_monarch_client()
            kwargs = {
                "account_name": name,
                "account_type": account_type,
                "account_balance": balance,
                "include_in_net_worth": include_in_net_worth,
            }
            if account_subtype:
                kwargs["account_subtype"] = account_subtype
            return await client.create_manual_account(**kwargs)

        result = run_async(_create_manual_account())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to create manual account: {e}")
        return f"Error creating manual account: {str(e)}"


@mcp.tool()
def update_account(
    account_id: str,
    name: Optional[str] = None,
    balance: Optional[float] = None,
    include_in_net_worth: Optional[bool] = None,
    hide_from_overview: Optional[bool] = None,
) -> str:
    """
    Update an existing account's settings or balance.

    Args:
        account_id: The account to update
        name: New account name
        balance: New balance (manual accounts only)
        include_in_net_worth: Update net worth inclusion
        hide_from_overview: Hide from main dashboard
    """
    try:

        async def _update_account():
            client = await get_monarch_client()
            kwargs = {}
            if name is not None:
                kwargs["name"] = name
            if balance is not None:
                kwargs["balance"] = balance
            if include_in_net_worth is not None:
                kwargs["include_in_net_worth"] = include_in_net_worth
            if hide_from_overview is not None:
                kwargs["hide_from_overview"] = hide_from_overview
            return await client.update_account(account_id, **kwargs)

        result = run_async(_update_account())

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to update account: {e}")
        return f"Error updating account: {str(e)}"


def main():
    """Main entry point for the server."""
    logger.info("Starting Monarch Money MCP Server...")
    try:
        mcp.run()
    except Exception as e:
        logger.error(f"Failed to run server: {str(e)}")
        raise


# Export for mcp run
app = mcp

if __name__ == "__main__":
    main()
