# Hackathon Status Check

Check the current state of both hackathon projects and report what's working, what's broken, and what's next.

## Steps

1. Check if ShipCall server starts:
```bash
cd .hackathon/shipcall && python3 -c "import app" 2>&1 | head -5
```

2. Check if VoiceScope server starts:
```bash
cd .hackathon/voicescope && python3 -c "import app" 2>&1 | head -5
```

3. Check env vars are configured:
```bash
[ -f .hackathon/shipcall/.env ] && echo "ShipCall .env: EXISTS" || echo "ShipCall .env: MISSING"
[ -f .hackathon/voicescope/.env ] && echo "VoiceScope .env: EXISTS" || echo "VoiceScope .env: MISSING"
```

4. Check dependencies installed:
```bash
python3 -c "import patter; print(f'patter {patter.__version__}')" 2>&1
python3 -c "import fastapi; print(f'fastapi {fastapi.__version__}')" 2>&1
python3 -c "import google.generativeai; print('google-generativeai OK')" 2>&1
```

5. Report status in this format:
```
HACKATHON STATUS — [time]
ShipCall:    [READY / BLOCKED / NOT STARTED] — [one-line detail]
VoiceScope:  [READY / BLOCKED / NOT STARTED] — [one-line detail]
Next action: [what to do right now]
Timeline:    [where we are in the 3-hour sprint]
```
