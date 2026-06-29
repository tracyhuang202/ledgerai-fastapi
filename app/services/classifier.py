from __future__ import annotations
import asyncio
import json
import logging
import re
from uuid import UUID
import anthropic
from supabase import AsyncClient
from app.core.config import get_settings
from app.services.pdf_parser import RawTransaction

logger = logging.getLogger(__name__)
cfg    = get_settings()

CATEGORY_TAXONOMY = """
- Cost of Goods Sold (COGS): Inventory, Manufacturing, Shipping & Freight
- Operating Expenses: Software & SaaS, Office Supplies, Utilities, Rent & Lease, Insurance
- Payroll & Benefits: Salaries & Wages, Health Insurance, 401k, Contractors
- Marketing & Advertising: Digital Ads, PR & Communications, Events
- Travel & Entertainment: Airfare, Hotel & Lodging, Ground Transport, Meals, Client Entertainment
- Professional Services: Legal, Accounting & Audit, Consulting, Recruiting
- Financial Charges: Bank Fees, Interest Expense, Merchant Processing Fees
- Capital Expenditure: Equipment, Furniture, Software Licenses (perpetual)
- Revenue / Income: Product Sales, Service Revenue, Interest Income, Refunds Received
- Taxes & Licenses: Business License, Permits, Payroll Tax
- Other / Unclassified: (CPA review required)
"""

def normalize_vendor(raw: str) -> str:
    text = raw.upper()
    text = re.sub(r"\b\d{4,}\b", " ", text)
    text = re.sub(r"\d{2}/\d{2}", " ", text)
    text = re.sub(r"[*#@!|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

async def _tier1_exact(supabase, vendor, firm_id, client_id):
    pools = [
        ("firm_vendor_categories", {"firm_id": firm_id, "client_id": client_id}, "client"),
        ("firm_vendor_categories", {"firm_id": firm_id, "client_id": None},      "firm"),
        ("industry_vendor_categories", None,                                      "industry"),
        ("public_vendor_categories",   None,                                      "public"),
    ]
    for table, filters, tier in pools:
        q = supabase.table(table).select("*").eq("is_active", True)
        if filters:
            for k, v in filters.items():
                if v is None:
                    q = q.is_(k, "null")
                else:
                    q = q.eq(k, str(v))
        resp = await q.execute()
        for row in (resp.data or []):
            p = str(row["vendor_pattern"]).upper()
            mt = row.get("match_type", "exact")
            hit = (
                (mt == "exact"    and vendor == p) or
                (mt == "prefix"   and vendor.startswith(p)) or
                (mt == "contains" and p in vendor) or
                (mt == "regex"    and bool(re.search(row["vendor_pattern"], vendor, re.I)))
            )
            if hit:
                return dict(
                    category_l1=row["category_l1"], category_l2=row.get("category_l2"),
                    match_method="exact", match_confidence=float(row.get("confidence_score", 1.0)),
                    matched_rule_id=row["id"], matched_rule_tier=tier,
                )
    return None

async def _tier2_fuzzy(supabase, vendor, firm_id, client_id):
    try:
        resp = await supabase.rpc("fuzzy_match_vendor", {
            "p_vendor": vendor, "p_firm_id": str(firm_id),
            "p_client_id": str(client_id) if client_id else None,
            "p_threshold": cfg.fuzzy_threshold,
        }).execute()
        if resp.data:
            best = resp.data[0]
            return dict(
                category_l1=best["category_l1"], category_l2=best.get("category_l2"),
                match_method="fuzzy", match_confidence=float(best["similarity"]),
                matched_rule_id=best["id"], matched_rule_tier=best["tier"],
            )
    except Exception as e:
        logger.warning("Fuzzy match failed for '%s': %s", vendor, e)
    return None

async def _tier3_llm(llm, transaction, vendor):
    prompt = f"""You are a senior CPA. Classify this bank transaction into the correct accounting category.

{CATEGORY_TAXONOMY}

Transaction:
- Date: {transaction.transaction_date}
- Description: {transaction.raw_description}
- Normalized vendor: {vendor}
- Amount: ${transaction.amount:,.2f} ({'debit' if transaction.amount < 0 else 'credit'})

Respond ONLY with valid JSON (no markdown):
{{"category_l1": "<category>", "category_l2": "<subcategory or null>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}}"""
    try:
        msg = await llm.messages.create(
            model="claude-sonnet-4-6", max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = msg.content[0].text.strip()
        raw  = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        data = json.loads(raw)
        return dict(
            category_l1=data["category_l1"], category_l2=data.get("category_l2"),
            match_method="llm", match_confidence=float(data.get("confidence", 0.5)),
            llm_reasoning=data.get("reasoning", ""),
        )
    except Exception as e:
        logger.error("LLM classification failed: %s", e)
        return dict(category_l1=None, category_l2=None, match_method="llm", match_confidence=0.0, llm_reasoning=str(e))

async def classify_one(supabase, llm, transaction, firm_id, client_id=None):
    vendor = normalize_vendor(transaction.raw_description)
    result = await _tier1_exact(supabase, vendor, firm_id, client_id)
    if not result or result["match_confidence"] < 0.9:
        fuzzy = await _tier2_fuzzy(supabase, vendor, firm_id, client_id)
        if fuzzy and fuzzy["match_confidence"] >= cfg.fuzzy_confidence_promote:
            result = fuzzy
        else:
            llm_result = await _tier3_llm(llm, transaction, vendor)
            result = llm_result
            if result["match_confidence"] >= cfg.llm_writeback_threshold and result["category_l1"]:
                try:
                    await supabase.table("firm_vendor_categories").upsert({
                        "firm_id": str(firm_id), "client_id": str(client_id) if client_id else None,
                        "vendor_pattern": vendor, "match_type": "exact",
                        "category_l1": result["category_l1"], "category_l2": result.get("category_l2"),
                        "confidence_score": result["match_confidence"],
                        "description": f"Auto-learned: {result.get('llm_reasoning','')[:100]}",
                        "is_active": True,
                    }, on_conflict="firm_id,vendor_pattern").execute()
                except Exception as e:
                    logger.warning("Rule writeback failed: %s", e)
    needs_review = not result or not result.get("category_l1") or (result.get("match_confidence") or 0) < 0.6
    return {
        "transaction_date": str(transaction.transaction_date),
        "raw_description":  transaction.raw_description,
        "normalized_vendor": vendor,
        "amount": transaction.amount,
        "needs_review": needs_review,
        **(result or {}),
    }

async def classify_batch(supabase, llm, transactions, firm_id, client_id=None, on_progress=None):
    semaphore = asyncio.Semaphore(cfg.llm_concurrency)
    total = len(transactions)
    completed = 0
    async def _guarded(tx):
        nonlocal completed
        async with semaphore:
            result = await classify_one(supabase, llm, tx, firm_id, client_id)
            completed += 1
            if on_progress:
                on_progress(completed, total)
            return result
    return await asyncio.gather(*[_guarded(tx) for tx in transactions])