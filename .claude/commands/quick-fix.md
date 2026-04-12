# Quick Fix

Hackathon emergency fix. Read the error, fix it, done. No analysis, no root cause docs.

## Process
1. Read the error output the user pasted
2. Identify the file and line
3. Fix it with minimal change
4. Tell the user to re-run

## Common Fixes
- `ModuleNotFoundError`: Run `pip install -r requirements.txt` in the project dir
- `Connection refused on 8000`: Patter embedded server not started — check lifespan handler
- `Twilio 401`: Wrong SID/token in .env
- `openai.AuthenticationError`: Wrong OPENAI_API_KEY in .env
- `google.api_core.exceptions.InvalidArgument`: Check Gemini model name and image encoding
- Port in use: `lsof -ti:8000 | xargs kill -9` or `lsof -ti:8080 | xargs kill -9`

## Rules
- Never suggest "let's investigate further" — just fix it
- If you can't fix in 2 minutes, suggest a workaround
- Hardcode a fallback value if the dynamic path is broken
