# Test Call

Trigger a test outbound call to verify Patter + Twilio are working.

## Usage
Run from the Patter repo root:

### ShipCall test
```bash
cd .hackathon/shipcall && python3 demo.py $DEV_PHONE_NUMBER
```

### VoiceScope test
```bash
cd .hackathon/voicescope && python3 demo.py test-image.jpg $DEV_PHONE_NUMBER
```

## If the call doesn't connect
1. Check ngrok is running: `curl -s http://127.0.0.1:4040/api/tunnels | python3 -m json.tool`
2. Check Twilio credentials: `curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID.json | python3 -m json.tool | head -5`
3. Check phone number format is +E.164 (e.g., +15551234567)
4. Check webhook URL in .env matches ngrok URL
5. Try restarting ngrok with a fresh tunnel

## Expected Result
Phone rings within 10 seconds. AI speaks the first_message. You can have a conversation.
