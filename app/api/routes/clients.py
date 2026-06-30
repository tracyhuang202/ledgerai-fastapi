from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
from supabase import acreate_client
from app.core.config import get_settings
from app.middleware.auth import AuthContext, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()
cfg = get_settings()

async def get_supabase():
    return await acreate_client(cfg.supabase_url, cfg.supabase_service_key)

@router.get("/clients")
async def list_clients(
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    resp = await supabase.table("clients").select("*").eq("firm_id", auth.firm_id).eq("is_active", True).order("name").execute()
    return resp.data or []

@router.post("/clients", status_code=201)
async def create_client(
    body: dict,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    if not body.get("name"):
        raise HTTPException(400, "Client name is required.")
    resp = await supabase.table("clients").insert({
        "firm_id": auth.firm_id,
        "name": body["name"],
        "industry": body.get("industry"),
        "entity_type": body.get("entity_type"),
        "ein": body.get("ein"),
        "created_by": auth.user_id,
        "is_active": True,
    }).execute()
    return resp.data[0]

@router.patch("/clients/{client_id}")
async def update_client(
    client_id: str, body: dict,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    allowed = ["name","industry","entity_type","ein","is_active"]
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields to update.")
    await supabase.table("clients").update(updates).eq("id", client_id).eq("firm_id", auth.firm_id).execute()
    return {"success": True}

@router.delete("/clients/{client_id}", status_code=204)
async def archive_client(
    client_id: str,
    auth: AuthContext = Depends(require_auth),
    supabase=Depends(get_supabase),
):
    await supabase.table("clients").update({"is_active": False}).eq("id", client_id).eq("firm_id", auth.firm_id).execute()
