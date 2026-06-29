from __future__ import annotations
import asyncio, json, logging, tempfile, uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from supabase import AsyncClient, create_async_client
import anthropic
from app.core.config import get_settings
from app.middleware.auth import AuthContext, require_auth
from app.services.classifier import classify_batch
from app.services.pdf_parser import ParseError, parse_pdf

logger = logging.getLogger(__name__)
router = APIRouter()
cfg = get_settings()
_jobs: dict[str, dict] = {}

async def get_supabase() -> AsyncClient:
    return await create_async_client(cfg.supabase_url, cfg.supabase_service_key)

async def get_llm() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)

async def _run_classification(upload_id, pdf_path, firm_id, client_id, supabase, llm):
    job = _jobs[upload_id]
    try:
        job.update(status="parsing", stage="Parsing PDF", progress=5)
        raw_txns = parse_pdf(pdf_path)
        job.update(progress=25, stage="Exact Match")
        total = len(raw_txns)
        def _on_progress(done, total_):
            pct = 25 + int(done / total_ * 70)
            stage = "Exact Match" if done < total_*0.4 else "Fuzzy Match" if done < total_*0.75 else "AI Inference"
            job.update(progress=pct, stage=stage)
        job["status"] = "classifying"
        classified = await classify_batch(supabase, llm, raw_txns, firm_id, client_id, _on_progress)
        rows = [{
            "id": str(uuid.uuid4()), "upload_id": upload_id,
            "firm_id": firm_id, "client_id": client_id,
            "transaction_date": t["transaction_date"],
            "raw_description": t["raw_description"],
            "normalized_vendor": t["normalized_vendor"],
            "amount": t["amount"],
            "category_l1": t.get("category_l1"),
            "category_l2": t.get("category_l2"),
            "match_method": t.get("match_method"),
            "match_confidence": t.get("match_confidence"),
            "matched_rule_id": t.get("matched_rule_id"),
            "matched_rule_tier": t.get("matched_rule_tier"),
            "llm_reasoning": t.get("llm_reasoning"),
        } for t in classified]
        if rows:
            await supabase.table("transactions").insert(rows).execute()
            await supabase.table("bill_uploads").update({
                "status": "review",
                "total_rows": total,
                "classified_rows": sum(1 for t in classified if t.get("category_l1")),
            }).eq("id", upload_id).execute()
        for t, r in zip(classified, rows):
            t["id"] = r["id"]
        job.update(status="done", stage="Complete", progress=100, transactions=classified,
                   total=total, classified=sum(1 for t in classified if t.get("category_l1")),
                   needs_review=sum(1 for t in classified if t.get("needs_review")))
    except ParseError as e:
        job.update(status="error", stage="Error", error=str(e))
    except Exception as e:
        job.update(status="error", stage="Error", error=f"Internal error: {e}")
        logger.exception("Classification failed for upload %s", upload_id)
    finally:
        Path(pdf_path).unlink(missing_ok=True)

@router.post("/bills/upload", status_code=202)
async def upload_bill(
    file: UploadFile = File(...),
    client_id: str = None,
    auth: AuthContext = Depends(require_auth),
    supabase: AsyncClient = Depends(get_supabase),
    llm: anthropic.AsyncAnthropic = Depends(get_llm),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")
    content = await file.read()
    if len(content) > cfg.max_pdf_size_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {cfg.max_pdf_size_mb} MB limit.")
    upload_id = str(uuid.uuid4())
    await supabase.table("bill_uploads").insert({
        "id": upload_id, "firm_id": auth.firm_id,
        "client_id": client_id, "uploaded_by": auth.user_id,
        "file_name": file.filename, "status": "pending",
    }).execute()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(content)
    tmp.close()
    _jobs[upload_id] = {"status": "pending", "stage": "Queued", "progress": 0,
                        "transactions": [], "error": None, "file_name": file.filename}
    asyncio.create_task(_run_classification(upload_id, tmp.name, auth.firm_id, client_id, supabase, llm))
    return {"upload_id": upload_id, "status": "pending"}

@router.get("/bills/{upload_id}")
async def get_upload_status(upload_id: str, auth: AuthContext = Depends(require_auth)):
    job = _jobs.get(upload_id)
    if not job:
        raise HTTPException(404, "Upload not found.")
    return job | {"upload_id": upload_id}

@router.get("/bills/{upload_id}/sse")
async def upload_sse(upload_id: str, token: str = None):
    async def _generate():
        while True:
            job = _jobs.get(upload_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Not found'})}\n\n"
                break
            yield f"data: {json.dumps({'status':job['status'],'progress':job['progress'],'stage':job['stage']})}\n\n"
            if job["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(_generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.patch("/bills/{upload_id}/transactions/{tx_id}")
async def edit_transaction(
    upload_id: str, tx_id: str, body: dict,
    auth: AuthContext = Depends(require_auth),
    supabase: AsyncClient = Depends(get_supabase),
):
    await supabase.table("transactions").update({
        "category_l1": body.get("category_l1"),
        "category_l2": body.get("category_l2"),
        "match_method": "manual", "match_confidence": 1.0, "is_reviewed": True,
    }).eq("id", tx_id).eq("firm_id", auth.firm_id).execute()
    if body.get("save_as_rule"):
        tx = await supabase.table("transactions").select("normalized_vendor,client_id").eq("id", tx_id).single().execute()
        if tx.data:
            await supabase.table("firm_vendor_categories").upsert({
                "firm_id": auth.firm_id, "client_id": tx.data.get("client_id"),
                "vendor_pattern": tx.data["normalized_vendor"], "match_type": "exact",
                "category_l1": body.get("category_l1"), "category_l2": body.get("category_l2"),
                "confidence_score": 1.0, "description": "Manually set by CPA", "is_active": True,
            }, on_conflict="firm_id,vendor_pattern").execute()
    job = _jobs.get(upload_id)
    if job:
        for t in job.get("transactions", []):
            if t.get("id") == tx_id:
                t.update(category_l1=body.get("category_l1"), category_l2=body.get("category_l2"),
                         match_method="manual", match_confidence=1.0, needs_review=False)
    return {"success": True}
