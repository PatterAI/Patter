"""
VoiceScope — Snap a photo, get a call from an AI expert.

FastAPI backend that:
1. Accepts image uploads + phone number via POST /analyze
2. Sends the image to Gemini 3 vision for analysis
3. Creates a Patter voice agent with the analysis injected
4. Places an outbound call to the user via Twilio

Run:
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from patter import Patter

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voicescope")

# ── Configure Gemini ────────────────────────────────────────────────────

gemini = genai.Client(api_key=os.getenv("GOOGLE_AI_STUDIO_KEY"))
GEMINI_MODEL = "gemini-2.0-flash"

# ── Configure Patter (local mode) ───────────────────────────────────────

phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# ── Default agent for inbound calls (Patter embedded server) ────────────

default_agent = phone.agent(
    system_prompt=(
        "You are VoiceScope, an expert visual analyst. "
        "If someone calls you, let them know they should upload a photo "
        "through the web interface and you'll call them back with your analysis."
    ),
    voice="alloy",
    first_message="Hi, this is VoiceScope. Upload a photo on our website and I'll call you back with my analysis.",
)


# ── Lifespan: start the Patter embedded server in the background ────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Patter's embedded server on port 8000 (handles Twilio webhooks)
    patter_task = asyncio.create_task(phone.serve(default_agent, port=8000))
    logger.info("Patter embedded server starting on port 8000")
    yield
    # Shutdown
    patter_task.cancel()
    try:
        await patter_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="VoiceScope", lifespan=lifespan)


# ── Gemini 3 vision analysis ────────────────────────────────────────────

ANALYSIS_PROMPT = (
    "You are an expert visual analyst. Analyze this image in detail. "
    "What do you see? What might be wrong or noteworthy? "
    "What specific, actionable steps should the user take? "
    "Be precise. Reference specific components, locations, and details. "
    "Structure your response as: 1) What you see, 2) What's notable or wrong, "
    "3) Recommended actions."
)


async def analyze_image(image_bytes: bytes, mime_type: str) -> str:
    """Send an image to Gemini and get a structured analysis."""
    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ANALYSIS_PROMPT,
        ],
    )
    return response.text


# ── Patter outbound call with analysis ──────────────────────────────────

async def voicescope_callback(user_phone: str, analysis: str):
    """Create a Patter agent with the analysis and call the user."""
    # Extract a concise opening line from the analysis
    first_line = analysis.split(".")[0] + "." if "." in analysis else analysis[:100]

    agent = phone.agent(
        system_prompt=(
            "You are VoiceScope, an expert visual analyst. "
            "The user just sent you a photo. Here is your detailed analysis:\n"
            "---\n"
            f"{analysis}\n"
            "---\n"
            "Call them and explain what you found. Be specific and actionable. "
            "Start with the most important finding. If they ask follow-up "
            "questions, answer based on the analysis above. Keep it conversational "
            "and helpful, not robotic. Speak naturally as if you're an expert "
            "colleague explaining what you see."
        ),
        voice="alloy",
        first_message=f"Hey, I just analyzed that photo you sent. {first_line}",
    )

    await phone.call(to=user_phone, agent=agent)
    logger.info("VoiceScope call initiated to %s", user_phone)


# ── API Endpoints ───────────────────────────────────────────────────────

@app.post("/analyze")
async def analyze_and_call(
    image: UploadFile = File(...),
    phone_number: str = Form(...),
):
    """Upload an image and receive a callback with the AI analysis."""
    if not phone_number.startswith("+"):
        return JSONResponse(
            status_code=400,
            content={"error": "Phone number must be in E.164 format (e.g., +15551234567)"},
        )

    image_bytes = await image.read()
    if not image_bytes:
        return JSONResponse(
            status_code=400,
            content={"error": "Empty image file"},
        )

    mime_type = image.content_type or "image/jpeg"

    # Run Gemini analysis
    logger.info("Analyzing image (%d bytes, %s) for %s", len(image_bytes), mime_type, phone_number)
    try:
        analysis = await analyze_image(image_bytes, mime_type)
    except Exception as e:
        logger.error("Gemini analysis failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"error": "Image analysis failed. Please try again."},
        )

    logger.info("Analysis complete (%d chars). Initiating callback.", len(analysis))

    # Fire the call in the background
    asyncio.create_task(voicescope_callback(phone_number, analysis))

    return {
        "status": "analyzing",
        "message": "You'll get a call in ~10 seconds with the analysis.",
        "analysis_preview": analysis[:200] + "..." if len(analysis) > 200 else analysis,
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "voicescope"}


# ── Serve the web UI ────────────────────────────────────────────────────

@app.get("/")
async def index():
    """Serve the single-page web UI."""
    return FileResponse("index.html")
