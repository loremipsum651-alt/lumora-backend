import os
import requests
import httpx
import random
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
import urllib3

# Matiin warning SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Load file .env
load_dotenv()

# ==== KONFIGURASI DARI .env ====
CHATGPT2API_URL = os.getenv("CHATGPT2API_URL")
CHATGPT2API_KEY = os.getenv("CHATGPT2API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
WAROENGPAY_API_KEY = os.getenv("WAROENGPAY_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Lumora SaaS Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==== STATE BUAT NGITUNG PARALLEL ====
active_requests = {}

# ==== MODEL REQUEST ====
class ImageRequest(BaseModel):
    prompt: str
    n: int = 1
    size: str = "1024x1024"
    reference_images: list[str] = None # List base64 images

class CheckoutRequest(BaseModel):
    plan: str  # 'basic' atau 'pro'

# ==== HELPER: Ambil User dari Token Login (Bypass Gotrue) ====
def get_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token tidak valid atau tidak ada.")
    token = authorization.replace("Bearer ", "")
    
    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {token}",
        "apikey": SUPABASE_KEY
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        if response.status_code != 200:
            print(f"ERROR AUTH SUPABASE: {response.text}", flush=True)
            raise HTTPException(status_code=401, detail="Sesi habis, silakan login ulang.")
            
        user_data = response.json()
        class UserObj:
            def __init__(self, data):
                self.id = data["id"]
                self.email = data.get("email")
        return UserObj(user_data)
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR AUTH SUPABASE: {e}", flush=True)
        raise HTTPException(status_code=401, detail="Sesi habis, silakan login ulang.")

# ==== ENDPOINT: AMBIL DATA USER (BUAT DASHBOARD) ====
@app.get("/api/me")
async def get_me(authorization: str = Header(None)):
    user = get_user(authorization)
    user_id = user.id

    res = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    profile = res.data
    if not profile:
        raise HTTPException(status_code=404, detail="Profil tidak ditemukan.")

    plan_type = profile.get("plan_type", "free")
    expires_at_str = profile.get("plan_expires_at")
    daily_gen = profile.get("daily_generations", 0)
    last_date_str = profile.get("last_generation_date")

    now = datetime.utcnow()
    today_str = now.strftime("%Y-%m-%d")

    if last_date_str != today_str:
        daily_gen = 0

    is_active = False
    if plan_type in ["basic", "pro"] and expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if expires_at > now:
                is_active = True
        except:
            pass

    if is_active:
        current_plan = plan_type
        if plan_type == "basic":
            quota_limit = 50
            quota_remaining = max(0, 50 - daily_gen)
        else: # pro
            quota_limit = None
            quota_remaining = None
    else:
        current_plan = None
        quota_limit = 5
        quota_remaining = max(0, 5 - daily_gen)

    return {
        "plan": current_plan,
        "quota_limit": quota_limit,
        "quota_remaining": quota_remaining,
        "expires_at": expires_at_str if is_active else None
    }

# ==== ENDPOINT: GENERATE GAMBAR ====
@app.post("/api/generate")
async def generate_image(req: ImageRequest, authorization: str = Header(None)):
    user = get_user(authorization)
    user_id = user.id

    res = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    profile = res.data
    if not profile:
        raise HTTPException(status_code=404, detail="Profil tidak ditemukan di database.")

    plan = profile.get("plan_type", "free")
    expires_at_str = profile.get("plan_expires_at")
    daily_gen = profile.get("daily_generations", 0)
    last_date_str = profile.get("last_generation_date")

    now = datetime.utcnow()
    today_str = now.strftime("%Y-%m-%d")

    if last_date_str != today_str:
        daily_gen = 0

    is_active = False
    if plan in ["basic", "pro"] and expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if expires_at > now:
                is_active = True
        except:
            pass

    # ==== CEK KUOTA & CONCURRENCY ====
    if is_active and plan == "pro":
        max_parallel = 5
        if daily_gen >= 999999:
            raise HTTPException(status_code=403, detail="Kuota tidak terduga.")
    elif is_active and plan == "basic":
        max_parallel = 3
        if daily_gen >= 50:
            raise HTTPException(status_code=403, detail="Kuota Basic (50/hari) habis. Silakan upgrade ke Pro.")
    else:
        max_parallel = 1
        if daily_gen >= 5:
            raise HTTPException(status_code=403, detail="Kuota gratis (5/hari) habis. Silakan beli paket.")

    # Cek counter parallel
    current_active = active_requests.get(user_id, 0)
    if current_active >= max_parallel:
        raise HTTPException(status_code=429, detail=f"Batas parallel plan lo ({max_parallel}) tercapai. Tunggu gambar sebelumnya selesai.")

    active_requests[user_id] = current_active + 1

    try:
        # ==== LOGIKA RASIO "AUTO" ====
        final_size = req.size
        if req.size == "auto":
            if not req.reference_images:
                final_size = random.choice(["1024x1024", "1792x1024", "1024x1792"])
            else:
                final_size = "1024x1024" # Fallback default kalau img2img

        requested_n = max(1, min(req.n, 4)) # Maksimal 4 per request (batasan API)
        final_n = min(requested_n, max_parallel)

        # ==== PILIH ENDPOINT (GENERATIONS vs EDITS) ====
        if req.reference_images:
            endpoint = f"{CHATGPT2API_URL}/images/edits"
            payload = {
                "model": "gpt-image-2",
                "prompt": req.prompt,
                "n": final_n,
                "size": final_size,
                "image": req.reference_images[0] if len(req.reference_images) == 1 else req.reference_images,
                "response_format": "b64_json"
            }
        else:
            endpoint = f"{CHATGPT2API_URL}/images/generations"
            payload = {
                "model": "gpt-image-2", 
                "prompt": req.prompt,
                "n": final_n,
                "size": final_size,
                "response_format": "b64_json",
            }

        headers = {"Authorization": f"Bearer {CHATGPT2API_KEY}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
        
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Error VPS: {resp.text[:200]}")

        # Update kuota harian
        supabase.table("profiles").update({
            "daily_generations": daily_gen + final_n,
            "last_generation_date": today_str
        }).eq("id", user_id).execute()

        data = resp.json()
        images = data.get("data", [])
        return {"images": [{"b64": img.get("b64_json", "")} for img in images if img.get("b64_json")]}
    
    finally:
        # Selalu kurangi counter, walaupun error
        active_requests[user_id] -= 1

# ==== ENDPOINT: CHECKOUT (BUAT TAGIHAN WAROENGPAY) ====
@app.post("/api/checkout")
async def checkout(req: CheckoutRequest, authorization: str = Header(None)):
    user = get_user(authorization)
    user_id = user.id

    amount = 10000 if req.plan == "basic" else 25000
    
    wp_payload = {
        "amount": amount,
        "reference": f"SUB-{user_id[:8]}-{int(datetime.now().timestamp())}",
        "description": f"Langganan Lumora {req.plan.capitalize()} (3 Hari)",
    }
    
    headers = {
        "Authorization": f"Bearer {WAROENGPAY_API_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        res = await client.post("https://waroengpay.com/api/payments", json=wp_payload, headers=headers)
        if res.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Gagal buat tagihan WaroengPay.")
        
        wp_data = res.json()
        
    supabase.table("profiles").update({
        "pending_payment_id": wp_data["id"],
        "pending_plan": req.plan
    }).eq("id", user_id).execute()

    return {"pay_url": wp_data["pay_url"]}

# ==== ENDPOINT: CEK STATUS (POLLING) ====
@app.get("/api/check-status")
async def check_status(authorization: str = Header(None)):
    user = get_user(authorization)
    user_id = user.id

    res = supabase.table("profiles").select("pending_payment_id, pending_plan").eq("id", user_id).single().execute()
    profile = res.data
    payment_id = profile.get("pending_payment_id")
    plan = profile.get("pending_plan")

    if not payment_id:
        return {"status": "no_pending_payment"}

    headers = {"Authorization": f"Bearer {WAROENGPAY_API_KEY}"}
    async with httpx.AsyncClient() as client:
        res = await client.get(f"https://waroengpay.com/api/payments/{payment_id}", headers=headers)
        
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="Gagal cek ke WaroengPay.")

    wp_data = res.json()
    status = wp_data.get("status")

    if status == "paid":
        new_expiry = datetime.utcnow() + timedelta(days=3)
        supabase.table("profiles").update({
            "plan_type": plan,
            "plan_expires_at": new_expiry.isoformat(),
            "pending_payment_id": None,
            "pending_plan": None,
            "daily_generations": 0
        }).eq("id", user_id).execute()
        
        return {"status": "paid", "plan": plan}
        
    return {"status": status}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)