from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
from supabase import create_async_client
from app.core.config import get_settings
from app.middleware.auth import AuthContext, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()
cfg = get_settings()

async def get_supabase():
    return await create_async_client(cfg.supabase_url, cfg.supabase_service_key)

@router.get("/rules")
async def list_rules(
    include_public: bool = True,
    client_id: str = None,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    firm_resp = await supabase.table("firm_vendor_categories").select("*").eq("firm_id", auth.firm_id).eq("is_active", True).execute()
    rules = [{"tier": "client" if r.get("client_id") else "firm", **r} for r in (firm_resp.data or [])]
    if client_id:
        client_resp = await supabase.table("clients").select("industry").eq("id", client_id).single().execute()
        if client_resp.data and client_resp.data.get("industry"):
            ind_resp = await supabase.table("industry_vendor_categories").select("*").eq("industry", client_resp.data["industry"]).eq("is_active", True).execute()
            rules += [{"tier": "industry", **r} for r in (ind_resp.data or [])]
    if include_public:
        pub_resp = await supabase.table("public_vendor_categories").select("*").eq("is_active", True).execute()
        rules += [{"tier": "public", **r} for r in (pub_resp.data or [])]
    return rules

@router.post("/rules", status_code=201)
async def create_rule(
    body: dict,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    resp = await supabase.table("firm_vendor_categories").insert({
        "firm_id": auth.firm_id,
        "client_id": body.get("client_id"),
        "vendor_pattern": body.get("vendor_pattern", "").upper().strip(),
        "match_type": body.get("match_type", "exact"),
        "category_l1": body.get("category_l1"),
        "category_l2": body.get("category_l2"),
        "description": body.get("description"),
        "override_lower": body.get("override_lower", False),
        "confidence_score": 1.0,
        "is_active": True,
    }).execute()
    return resp.data[0]

@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str, body: dict,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    allowed = ["category_l1","category_l2","match_type","description","is_active","override_lower"]
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields to update.")
    await supabase.table("firm_vendor_categories").update(updates).eq("id", rule_id).eq("firm_id", auth.firm_id).execute()
    return {"success": True}

@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    await supabase.table("firm_vendor_categories").update({"is_active": False}).eq("id", rule_id).eq("firm_id", auth.firm_id).execute()
