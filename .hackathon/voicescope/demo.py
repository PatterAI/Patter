"""
VoiceScope Demo — standalone test script.

Sends a test image to Gemini 3 for analysis, then triggers a Patter
outbound call with the results. Use this to verify both integrations
work before running the full web app.

Usage:
    python3 demo.py <image_path> <phone_number>

Example:
    python3 demo.py circuit_board.jpg +15551234567
"""

import asyncio
import base64
import os
import sys

import google.generativeai as genai
from dotenv import load_dotenv
from patter import Patter

load_dotenv()


async def main():
    if len(sys.argv) < 3:
        print("Usage: python3 demo.py <image_path> <phone_number>")
        print("Example: python3 demo.py circuit_board.jpg +15551234567")
        sys.exit(1)

    image_path = sys.argv[1]
    phone_number = sys.argv[2]

    if not phone_number.startswith("+"):
        print("Error: Phone number must be in E.164 format (e.g., +15551234567)")
        sys.exit(1)

    # ── Step 1: Read the image ───────────────────────────────────────
    print(f"Reading image: {image_path}")
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Detect MIME type from extension
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
    mime_type = mime_map.get(ext, "image/jpeg")
    print(f"Image: {len(image_bytes)} bytes, {mime_type}")

    # ── Step 2: Gemini 3 analysis ────────────────────────────────────
    print("Sending to Gemini 3 for analysis...")
    genai.configure(api_key=os.getenv("GOOGLE_AI_STUDIO_KEY"))
    model = genai.GenerativeModel("gemini-3-flash-preview")

    response = model.generate_content([
        (
            "You are an expert visual analyst. Analyze this image in detail. "
            "What do you see? What might be wrong or noteworthy? "
            "What specific, actionable steps should the user take? "
            "Be precise. Reference specific components, locations, and details."
        ),
        {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()},
    ])
    analysis = response.text
    print(f"\n--- Gemini Analysis ({len(analysis)} chars) ---")
    print(analysis)
    print("--- End Analysis ---\n")

    # ── Step 3: Patter outbound call ─────────────────────────────────
    print(f"Initiating VoiceScope call to {phone_number}...")

    phone = Patter(
        mode="local",
        openai_key=os.getenv("OPENAI_API_KEY"),
        twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
        phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
        webhook_url=os.getenv("WEBHOOK_URL"),
    )

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
            "questions, answer based on the analysis above. Keep it conversational."
        ),
        voice="alloy",
        first_message=f"Hey, I just analyzed that photo you sent. {first_line}",
    )

    # Start the embedded server (needed for Twilio webhooks) and place the call
    async def place_call_after_startup():
        await asyncio.sleep(2)  # Wait for server to start
        await phone.call(to=phone_number, agent=agent)
        print("Call initiated! Pick up your phone.")

    # Run server and call concurrently
    await asyncio.gather(
        phone.serve(agent, port=8000),
        place_call_after_startup(),
    )


if __name__ == "__main__":
    asyncio.run(main())
