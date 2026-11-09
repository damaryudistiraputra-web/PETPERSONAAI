"""PawRoyalty API: authenticated, quota-limited pet photo editing proxy.

Image editing can use Cloudflare Workers AI free allocation (quota limited), or
an explicitly enabled paid Gemini image model. No secret is sent to Android.
"""
from __future__ import annotations

import base64
import html
import io
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("pawroyalty")
app = FastAPI(title="PawRoyalty API", docs_url=None, redoc_url=None)
ROOT = Path(os.environ.get("SQLITE_PATH", "./pawroyalty.db"))
MAX_PHOTO_BYTES = 6 * 1024 * 1024
MAX_INPUT_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
LIMIT = int(os.environ.get("DAILY_CREDITS", "5"))
# Shared budget circuit-breaker: rotating anonymous Firebase users cannot bypass it.
# 0 = pause all image generation; adjust to fit actual Cloudflare allowance.
GLOBAL_DAILY_LIMIT = int(os.environ.get("GLOBAL_DAILY_LIMIT", "20"))
IP_LIMIT = int(os.environ.get("DAILY_IP_LIMIT", "0"))  # Optional: enable only with a correctly trusted reverse proxy
ENV = os.environ.get("APP_ENV", "production")
REQUIRE_APP_CHECK = os.environ.get("REQUIRE_APP_CHECK", "true").lower() == "true"
MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")
CF_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"


def image_provider() -> str:
    # Explicit selection prevents accidentally falling back to a paid API.
    return os.environ.get("IMAGE_PROVIDER", "cloudflare").strip().lower()


def image_provider_configured() -> bool:
    provider = image_provider()
    if provider == "cloudflare":
        return bool(os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"))
    if provider == "gemini":
        return (os.environ.get("ENABLE_PAID_IMAGE_GENERATION", "false").lower() == "true"
                and bool(os.environ.get("GEMINI_API_KEY")))
    return False


def image_generation_requires_billing() -> bool:
    # Workers Free includes a limited daily free allowance; over-quota calls fail.
    # This does not claim that image generation is unlimited or production-ready.
    return image_provider() == "gemini"


THEMES = {
    "single": {
        "ceo": "wearing a tailored executive suit, sitting at a luxury modern desk",
        "monarch": "as a royal monarch wearing an elegant crown, seated on a regal throne",
        "godfather": "as a fictional cinematic mob boss wearing a pinstripe suit in a dramatic study",
        "kaiju": "as a giant friendly animal towering above a miniature fictional city skyline",
        "zoomies": "as a playful racing champion in a fictional race car in a stadium",
        "agent": "as a stylish fictional secret agent in a cinematic espionage setting",
    },
    "duo": {
        "monarch_knight": "pet one as a royal sovereign and pet two as a protective knight in a palace",
        "rock_agent": "pet one as a rockstar and pet two as a tour bodyguard onstage",
        "cyber_wizards": "both pets as futuristic neon-lit science fiction adventurers",
    },
}


@contextmanager
def db_connect():
    ROOT.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(ROOT), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS credits (
            actor TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(actor, day)
        );
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY, uid TEXT NOT NULL, created_at TEXT NOT NULL,
            mode TEXT NOT NULL, theme TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, uid TEXT NOT NULL, generation_id TEXT NOT NULL,
            reason TEXT NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL,
            review_image BLOB
        );
        CREATE TABLE IF NOT EXISTS deletion_requests (
            id TEXT PRIMARY KEY, uid TEXT NOT NULL, email TEXT NOT NULL,
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        );
    """)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def today():
    return datetime.now(timezone.utc).date().isoformat()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@app.middleware("http")
async def no_cache_sensitive_api(request: Request, call_next):
    # Reject obviously unauthenticated/oversized uploads before Starlette's
    # multipart parser spools a hostile request to disk. Principal() still
    # performs the cryptographic Firebase ID token + App Check verification.
    if request.method == "POST" and request.url.path in ("/api/v1/generate", "/api/v1/report"):
        bearer = request.headers.get("authorization", "")
        app_check = request.headers.get("x-firebase-appcheck", "")
        if not bearer.startswith("Bearer ") or (REQUIRE_APP_CHECK and not app_check):
            return JSONResponse({"detail": "Authentication and app attestation required"}, status_code=401, headers={"Cache-Control": "no-store"})
        length_header = request.headers.get("content-length", "")
        if length_header.isdigit() and int(length_header) > 14 * 1024 * 1024:
            return JSONResponse({"detail": "Request body too large"}, status_code=413, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    # Returned Base64 portraits, auth-related status and deletion requests must
    # never be cached by intermediary proxies or on-device network caches.
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def credit_count(uid: str) -> int:
    with db_connect() as db:
        row = db.execute("SELECT count FROM credits WHERE actor=? AND day=?", ("u:" + uid, today())).fetchone()
        return row[0] if row else 0


def global_credit_count() -> int:
    with db_connect() as db:
        row = db.execute("SELECT count FROM credits WHERE actor=? AND day=?", ("g:all", today())).fetchone()
        return row[0] if row else 0


def adjust_credits(uid: str, ip: str, difference: int):
    """Atomic, SQLite-backed daily per-account AND IP guard. Multiple workers share one DB."""
    day = today()
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        counters = []
        limits = [("u:" + uid, LIMIT), ("g:all", GLOBAL_DAILY_LIMIT)]
        if IP_LIMIT > 0:
            limits.append(("ip:" + ip, IP_LIMIT))
        for actor, limit in limits:
            row = db.execute("SELECT count FROM credits WHERE actor=? AND day=?", (actor, day)).fetchone()
            used = row[0] if row else 0
            if difference > 0 and used + difference > limit:
                if actor == "g:all":
                    raise HTTPException(429, "The studio's free image allowance is exhausted today. Try again after UTC midnight.")
                raise HTTPException(429, "Daily AI credits exhausted. Try again after UTC midnight.")
            counters.append((actor, max(0, used + difference)))
        for actor, used in counters:
            db.execute("INSERT INTO credits (actor,day,count) VALUES (?,?,?) ON CONFLICT(actor,day) DO UPDATE SET count=excluded.count", (actor, day, used))


def firebase_auth():
    try:
        import firebase_admin
        from firebase_admin import credentials
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.ApplicationDefault(), {
                "projectId": os.environ["FIREBASE_PROJECT_ID"],
            })
        return firebase_admin
    except Exception as exc:
        log.error("Firebase not configured: %s", type(exc).__name__)
        raise HTTPException(503, "Authentication service is not configured") from None


def principal(request: Request, authorization: Annotated[str | None, Header()] = None,
              app_check_token: Annotated[str | None, Header(alias="X-Firebase-AppCheck")] = None) -> str:
    if ENV == "production" and not REQUIRE_APP_CHECK:
        raise HTTPException(503, "App Check must be enabled in production")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    admin = firebase_auth()
    try:
        decoded = admin.auth.verify_id_token(authorization[7:], check_revoked=True)
        uid = decoded["uid"]
    except Exception:
        raise HTTPException(401, "Invalid or expired authentication") from None
    if REQUIRE_APP_CHECK:
        if not app_check_token:
            raise HTTPException(401, "App attestation required")
        try:
            # Firebase Admin checks token signature and its registered Firebase app.
            from firebase_admin import app_check
            app_check.verify_token(app_check_token)
        except Exception:
            raise HTTPException(401, "Invalid app attestation") from None
    return uid


def client_ip(request: Request) -> str:
    # Trust the ASGI proxy's client.host only. Do NOT trust arbitrary X-Forwarded-For here.
    return request.client.host if request.client else "unknown"


def prune_old_records():
    # Execute with a daily cron in production: e.g. `python cleanup.py`.
    threshold = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    report_threshold = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with db_connect() as db:
        db.execute("DELETE FROM reports WHERE created_at<?", (report_threshold,))
        db.execute("UPDATE reports SET review_image=NULL WHERE created_at<?", (threshold,))
        # Keep lightweight generation IDs until account deletion: a locally saved
        # portrait can still be reported after 30 days. No images are retained here.
        db.execute("DELETE FROM credits WHERE day<?", ((datetime.now(timezone.utc)-timedelta(days=35)).date().isoformat(),))


@app.get("/health")
def health():
    return {"status": "ok", "model": CF_MODEL if image_provider() == "cloudflare" else MODEL,
            "provider": image_provider(), "image_provider_configured": image_provider_configured(),
            "image_generation_requires_billing": image_generation_requires_billing(), "auth_project_configured": bool(os.environ.get("FIREBASE_PROJECT_ID"))}


@app.get("/api/v1/quota")
def quota(uid: str = Depends(principal)):
    user_remaining = max(0, LIMIT - credit_count(uid))
    global_remaining = max(0, GLOBAL_DAILY_LIMIT - global_credit_count())
    return {"remaining": min(user_remaining, global_remaining), "limit": LIMIT,
            "user_remaining": user_remaining,
            "service_remaining": global_remaining, "service_limit": GLOBAL_DAILY_LIMIT,
            "reset": "UTC midnight",
            "image_provider_configured": image_provider_configured(),
            "image_generation_requires_billing": image_generation_requires_billing()}


async def normalized_photo(photo: UploadFile) -> bytes:
    if not photo.content_type or photo.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, "Select a JPG, PNG or WEBP photo")
    raw = await photo.read(MAX_PHOTO_BYTES + 1)
    if len(raw) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Each photo must be under 6MB")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_INPUT_PIXELS:
                raise HTTPException(413, "Photo dimensions are too large")
            if source.format not in ("JPEG", "PNG", "WEBP"):
                raise HTTPException(415, "Unsupported photo format")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1400, 1400))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            return output.getvalue()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Couldn't read that photo") from None


def safe_text(value: str, limit: int) -> str:
    return re.sub(r"[\x00-\x1F]+", " ", value).strip()[:limit]


def prompt_is_unsafe(value: str) -> bool:
    # Defense-in-depth for explicit requests; model safety filters still apply.
    return bool(re.search(r"\b(?:child sexual|sexual minor|bestiality|animal abuse|torture animal|graphic gore|terrorist propaganda|sexual assault)\b", value, re.I))


def cloudflare_reference_jpeg(raw: bytes) -> bytes:
    """Workers AI FLUX.2 Klein 4B requires every reference image < 512x512 px."""
    with Image.open(io.BytesIO(raw)) as input_image:
        frame = ImageOps.exif_transpose(input_image).convert("RGB")
        frame.thumbnail((480, 480))
        buffer = io.BytesIO()
        frame.save(buffer, "JPEG", quality=86)
        return buffer.getvalue()


def checked_provider_image(raw: bytes) -> bytes:
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(502, "AI provider returned an oversized image")
    try:
        with Image.open(io.BytesIO(raw)) as photo:
            if (photo.width < 64 or photo.height < 64 or
                    photo.width * photo.height > MAX_INPUT_PIXELS or
                    photo.format not in ("PNG", "JPEG", "WEBP")):
                raise ValueError("Invalid image")
            photo.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(502, "AI provider returned an invalid image") from None
    return raw


async def optional_gemini_caption(prompt: str) -> str:
    """Use a *free tier eligible* text model; never send pet photos to Gemini here."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or os.environ.get("ENABLE_FREE_GEMINI_CAPTIONS", "false").lower() != "true":
        return ""
    model = "gemini-2.5-flash-lite"
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text":
        "Write a short, friendly, family-safe pet portrait caption under 140 characters. "
        "Do not imply you saw the output image. Context: " + prompt[:800]
    }]}], "generationConfig": {"maxOutputTokens": 90, "temperature": 0.7}}
    try:
        async with httpx.AsyncClient(timeout=18) as client:
            response = await client.post(endpoint, headers={"x-goog-api-key": key}, json=payload)
        response.raise_for_status()
        items = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return safe_text(" ".join(p.get("text", "") for p in items), 180)
    except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
        # Successful image editing must NOT fail because an optional text call is unavailable.
        log.info("Optional Gemini caption unavailable")
        return ""


async def edit_with_cloudflare(images: list[bytes], prompt: str) -> tuple[bytes, str]:
    account = os.environ.get("CF_ACCOUNT_ID", "")
    token = os.environ.get("CF_API_TOKEN", "")
    if not account or not token:
        raise HTTPException(503, "Cloudflare image provider is not configured")
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CF_MODEL}"
    # multipart/form-data is required, and reference files must be <512px per axis.
    files = [(f"input_image_{idx}", (f"pet{idx}.jpg", cloudflare_reference_jpeg(photo), "image/jpeg"))
             for idx, photo in enumerate(images)]
    data = {"prompt": prompt, "width": "768", "height": "768"}
    try:
        async with httpx.AsyncClient(timeout=115) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {token}"},
                                         data=data, files=files)
        if response.status_code in (401, 403):
            raise HTTPException(503, "Cloudflare token, access, or permissions are invalid")
        if response.status_code == 429:
            raise HTTPException(503, "Free image quota or rate limit reached. Try again later")
        if response.status_code == 400:
            raise HTTPException(422, "The image provider rejected this photo or scenario")
        if response.status_code >= 500:
            raise HTTPException(503, "Image provider unavailable. Try again later")
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", True):
            raise HTTPException(503, "Image provider did not complete the request")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise HTTPException(502, "Image provider returned invalid output")
        encoded = result.get("image", "")
        if not isinstance(encoded, str) or not encoded:
            raise HTTPException(502, "Image provider did not return a portrait")
        image = checked_provider_image(base64.b64decode(encoded, validate=True))
        # Cloudflare image editing does not return a caption. This is optional.
        caption = await optional_gemini_caption(prompt)
        return image, caption
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, base64.binascii.Error) as exc:
        log.warning("Cloudflare provider error: %s", type(exc).__name__)
        raise HTTPException(503, "Image provider request failed. Please retry later") from None


async def edit_with_gemini(images: list[bytes], prompt: str) -> tuple[bytes, str]:
    if image_provider() == "cloudflare":
        return await edit_with_cloudflare(images, prompt)
    key = os.environ.get("GEMINI_API_KEY")
    if not image_provider_configured() or image_provider() != "gemini":
        raise HTTPException(503, "Image provider is unavailable. Configure Cloudflare Free or explicitly enable a paid provider.")
    parts = [{"text": prompt}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(photo).decode("ascii")}}
        for photo in images
    ]
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        "safetySettings": [
            {"category": category, "threshold": "BLOCK_MEDIUM_AND_ABOVE"}
            for category in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
        ],
    }
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=115) as client:
            response = await client.post(endpoint, headers={"x-goog-api-key": key}, json=payload)
        if response.status_code == 403:
            log.error("Provider authentication/permission denied")
            raise HTTPException(503, "AI provider permission or billing is not configured")
        if response.status_code == 400:
            log.warning("Provider rejected generation request")
            raise HTTPException(422, "The AI provider rejected this photo or scenario")
        if response.status_code == 429:
            raise HTTPException(503, "AI provider busy. Please try again later.")
        if response.status_code >= 500:
            raise HTTPException(503, "AI provider unavailable. Please try again later.")
        response.raise_for_status()
        result = response.json()
        items = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        image_part = next((part.get("inlineData") or part.get("inline_data") for part in items if part.get("inlineData") or part.get("inline_data")), None)
        if not image_part:
            raise HTTPException(422, "Image was blocked or no image was returned")
        generated = base64.b64decode(image_part["data"], validate=True)
        if len(generated) > 8 * 1024 * 1024:
            raise HTTPException(502, "AI image exceeds output size limit")
        try:
            with Image.open(io.BytesIO(generated)) as output_image:
                if output_image.width < 64 or output_image.height < 64 or output_image.width * output_image.height > MAX_INPUT_PIXELS:
                    raise ValueError("Invalid dimensions")
                output_image.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(502, "AI provider returned an invalid image") from None
        lore = next((safe_text(part.get("text", ""), 240) for part in items if part.get("text")), "")
        return generated, lore
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("Provider failure: %s", type(exc).__name__)
        raise HTTPException(503, "AI request failed. Please retry later.") from None


@app.post("/api/v1/generate")
async def generate(
    request: Request,
    mode: Annotated[str, Form()],
    theme: Annotated[str, Form()],
    name1: Annotated[str, Form()] = "",
    name2: Annotated[str, Form()] = "",
    prompt: Annotated[str, Form()] = "",
    photos: list[UploadFile] = File(...),
    uid: str = Depends(principal),
):
    if mode not in ("single", "duo", "custom"):
        raise HTTPException(422, "Unknown generation mode")
    if len(photos) != (2 if mode == "duo" else 1):
        raise HTTPException(422, "This mode requires a different number of photos")
    if mode != "custom" and theme not in THEMES[mode]:
        raise HTTPException(422, "Unknown style")
    scenario = safe_text(prompt, 350)
    if mode == "custom" and not scenario:
        raise HTTPException(422, "Describe a custom pet scenario")
    if prompt_is_unsafe(scenario):
        raise HTTPException(422, "That scenario is not permitted")
    name1 = safe_text(name1, 50)
    name2 = safe_text(name2, 50)
    normalized = [await normalized_photo(photo) for photo in photos]
    if not image_provider_configured():
        raise HTTPException(503, "Portrait generation is not configured. Add Cloudflare Workers AI credentials to the backend.")
    target = THEMES[mode][theme] if mode != "custom" else scenario
    # Images are supplied inline, not written to disk. Reject text/logos in generated output.
    instruction = (
        "Edit the supplied pet reference photo(s), preserving exact species, individual muzzle, eyes, "
        "ear shape, unique fur markings and recognizable expression. "
        + ("Two separate pets are pictured: keep pet 1 and pet 2 distinct and clearly visible. " if mode == "duo" else "")
        + "Create one believable editorial pet photograph featuring the same pet(s) "
        + f"{target}. "
        + "Prioritize identity over costume: preserve the animal's breed, facial proportions, "
        + "eye color and exact fur markings. Do not replace the pet with a generic lookalike. "
        + "Use natural light, credible fur textures, realistic shadows and restrained color grading. "
        + "Keep eyes and muzzle visible; avoid plastic CGI, extra paws, extra ears or distorted anatomy. "
        + "Make the imaginative costume playful, family friendly, clearly fictional and NON-VIOLENT. "
        + "No text, letters, brand names, watermarks, logos, or interface elements in the image. "
        + "No human faces, no nudity, no sexual context. "
        + f"Pet one name: {name1 or 'pet'}; pet two name: {name2 or 'pet'}. "
        + "Also write a witty, harmless one-sentence caption (under 180 characters)."
    )
    ip = client_ip(request)
    adjust_credits(uid, ip, 1)
    try:
        image, lore = await edit_with_gemini(normalized, instruction)
        record_id = uuid.uuid4().hex
        with db_connect() as db:
            db.execute("INSERT INTO generations(id,uid,created_at,mode,theme) VALUES(?,?,?,?,?)",
                       (record_id, uid, utcnow(), mode, theme))
        return {"generation_id": record_id, "image_base64": base64.b64encode(image).decode("ascii"),
                "image_mime_type": ("image/png" if image.startswith(b"\x89PNG") else "image/jpeg"), "lore": lore}
    except Exception:
        # Refund unsuccessful generations; no misleading quota charge.
        adjust_credits(uid, ip, -1)
        raise


class Report(BaseModel):
    generation_id: str = Field(min_length=20, max_length=70)
    reason: str = Field(min_length=3, max_length=100)
    notes: str = Field(default="", max_length=500)
    image_base64: str = Field(min_length=10, max_length=2_000_000)


@app.post("/api/v1/report")
def report(body: Report, uid: str = Depends(principal)):
    # The reported generated preview is saved only after an affirmative Report action,
    # so moderators have the actual offending material for review.
    try:
        image = base64.b64decode(body.image_base64, validate=True)
        if len(image) > 1_000_000:
            raise ValueError("image too large")
        with Image.open(io.BytesIO(image)) as photo:
            if photo.width * photo.height > 1_000_000:
                raise ValueError("Preview dimensions too large")
            photo.verify()
    except (ValueError, UnidentifiedImageError, OSError):
        raise HTTPException(422, "Invalid report preview") from None
    with db_connect() as db:
        row = db.execute("SELECT id FROM generations WHERE id=? AND uid=?", (body.generation_id, uid)).fetchone()
        if not row:
            raise HTTPException(404, "Generated item not found")
        db.execute("INSERT INTO reports(id,uid,generation_id,reason,notes,created_at,review_image) VALUES(?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, uid, body.generation_id, safe_text(body.reason, 100), safe_text(body.notes, 500), utcnow(), image))
    return {"received": True}


@app.delete("/api/v1/me")
def delete_user_data(uid: str = Depends(principal)):
    admin = firebase_auth()
    try:
        admin.auth.delete_user(uid)
    except admin.auth.UserNotFoundError:
        pass
    except Exception:
        raise HTTPException(503, "Account deletion unavailable; please try again") from None
    with db_connect() as db:
        db.execute("DELETE FROM reports WHERE uid=?", (uid,))
        db.execute("DELETE FROM generations WHERE uid=?", (uid,))
        db.execute("DELETE FROM credits WHERE actor=?", ("u:" + uid,))
        db.execute("DELETE FROM deletion_requests WHERE uid=?", (uid,))
    return {"deleted": True}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    developer = os.environ.get("DEVELOPER_LEGAL_NAME", "")
    contact = os.environ.get("PRIVACY_CONTACT_EMAIL", "")
    if not developer or not contact:
        raise HTTPException(503, "Developer must publish their legal contact information before launch")
    developer, contact = html.escape(developer), html.escape(contact)
    return f"""<!doctype html><html lang='en'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Privacy Policy — PawRoyalty Studio</title><style>body{{max-width:720px;margin:3em auto;padding:1em;font:16px system-ui;line-height:1.7}}</style>
<h1>Privacy Policy — PawRoyalty Studio</h1><p>Developer: {developer}. Contact: {contact}.</p>
<p>Updated: November 9, 2026.</p><h2>What we process</h2>
<p>When you generate a portrait, selected pet photos, names and scenarios are sent via HTTPS to our backend and the image provider configured for the service (Cloudflare Workers AI by default, or Gemini image generation if enabled). Photos are transmitted only after your consent. If optional Gemini captions are enabled, the textual scenario is separately sent to Gemini; photos are not sent to Gemini for captions. Please upload only photos you are entitled to use.</p>
<p>Our backend processes uploaded photos in memory and does not persist the uploaded photos or generated image. We do not upload photos from your gallery unless you request a generation. Generated portraits are stored locally in the app's private Collection for up to 24 recent creations; older entries are removed as new ones arrive. They are not automatically uploaded again or exported to your device Gallery. You can delete saved portraits within the app or export them yourself. Exported Gallery photos are not deleted when you delete the app account.</p>
<h2>Account and usage data</h2><p>Firebase Authentication creates an anonymous account identifier. Firebase App Check processes app/device integrity tokens. We retain anonymous account IDs, daily account and server-visible IP request counters, and generated content IDs for rate limits and abuse prevention. The IP quota counters are shared security data and are normally removed after 35 days by scheduled cleanup. Generation identifiers and style metadata are kept until account deletion so older locally saved images can still be reported. They do not contain the generated photograph. If you submit an account deletion request through the website, we hold the supplied account ID and contact email while reviewing the request. When you report a generated result, we store its small preview image for up to 30 days so moderators can review the content; report reason and notes are retained up to 90 days for moderation and abuse investigation. Our hosting provider may retain request metadata in service logs.</p>
<h2>Third parties</h2><p>Images and prompts are shared with Cloudflare Workers AI (or Google's Gemini image-generation service if the developer enables that alternative), subject to the relevant provider's data processing and retention terms. Optional textual captions use Gemini Flash-Lite when enabled. Firebase is used for authentication and app attestation. Hosting infrastructure processes requests. We do not sell uploaded photos. Please review the provider retention policies before uploading sensitive photos.</p>
<h2>Security and rights</h2><p>We require HTTPS, use server-side API keys and do not request broad photo-storage access. You can delete your anonymous app account and linked backend records through Settings → Your account inside the app. To request account deletion outside the app, use <a href='/delete-account'>the external deletion request form</a>. You may also contact {contact} with privacy questions.</p>
</html>"""


@app.get("/delete-account", response_class=HTMLResponse)
def external_deletion_page():
    return """<!doctype html><html lang='en'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PawRoyalty account deletion</title><body style='font:16px system-ui;max-width:650px;margin:3em auto;padding:1em'>
<h1>Request account deletion</h1><p>To delete immediately, open PawRoyalty Studio → Settings → Your account → Delete account and app data.</p>
<p>If you cannot use the app, provide the anonymous account ID displayed inside the app (if available) and a contact email. We will follow up to verify ownership before taking action. Do not send your photos.</p>
<form action='/deletion-request' method='post'><label>Anonymous account ID <input name='uid' required maxlength='128'></label><p><label>Contact email <input name='email' type='email' required maxlength='254'></label></p>
<label style='display:none'>Leave this empty <input name='website'></label><button type='submit'>Submit deletion request</button></form>
<p>Deleting the app alone does not delete your account. Images saved to your Gallery must be removed separately.</p></body></html>"""


@app.post("/deletion-request", response_class=HTMLResponse)
def external_deletion_request(uid: str = Form(...), email: str = Form(...), website: str = Form("")):
    if website or not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", uid) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        raise HTTPException(400, "Invalid deletion request")
    with db_connect() as db:
        db.execute("INSERT INTO deletion_requests(id,uid,email,created_at) VALUES(?,?,?,?)", (uuid.uuid4().hex, uid, email, utcnow()))
    return "<html><body><h1>Deletion request received</h1><p>We will contact you to verify ownership before processing. You may delete immediately inside the app.</p></body></html>"
