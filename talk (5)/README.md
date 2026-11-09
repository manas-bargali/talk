# Talk

A calm voice chatbot. One button: tap, speak, tap again. Your recording is uploaded, transcribed, answered by a free LLM, and the answer is shown as text and spoken aloud.

    mic (browser) -> 16 kHz WAV -> /api/talk -> speech-to-text -> LLM chain -> text + natural voice (server) or device voice

## Run

    python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env                                  # then paste your free keys
    python app.py                                         # open http://localhost:8000

Free keys: Gemini (aistudio.google.com/apikey), Groq (console.groq.com), Cerebras (cloud.cerebras.ai), OpenRouter (openrouter.ai).
Groq is used for speech-to-text (Whisper) with Gemini as backup, so add at least GROQ_API_KEY or GEMINI_API_KEY.

## How it stays inside free limits

- Chain order: Gemini -> Groq 120B -> Groq 20B -> Cerebras -> OpenRouter. First lane with room answers.
- Every request is counted before it is sent, against per-minute and per-day caps set BELOW the real free limits (edit in .env).
- A 429 or error puts that lane to rest (Retry-After respected) and the next one answers.
- Per-visitor cap (IP_RPM / IP_RPD), 30 s max recording, 3 MB upload cap, last 6 messages only, 300 reply tokens.
- Check usage any time: /api/status

## Deploy on Render (free)

1. Put the `talk` folder in a GitHub repo (the `.gitignore` already keeps `.env` out, so your keys never get uploaded).
2. On render.com: **New +** > **Blueprint** > pick the repo. Render reads `render.yaml`. (Or **New +** > **Web Service** with build `pip install -r requirements.txt` and start `gunicorn app:app --workers 1 --threads 8 --timeout 90 --bind 0.0.0.0:$PORT`.)
3. Paste your keys when asked: at least `GROQ_API_KEY` or `GEMINI_API_KEY`. Add `SARVAM_API_KEY` for the best Hindi voice. Keys are set in the Render dashboard (Environment tab), never in the code.
4. Open the `https://...onrender.com` address. HTTPS is automatic, which the microphone needs.

Things to know on the free plan:
- The service sleeps after about 15 minutes with no visitors; the next visit takes up to a minute to wake. Open the page once before a class.
- The disk is wiped on every restart, so `usage.json` resets. The safe caps in `.env` are set below the real provider limits to cover this.
- Keep `--workers 1` so the usage counters stay in one process.
- Optional settings (voice, caps, models) go in the Environment tab using the names in `.env.example`.
- Check the log tab after the first deploy: it prints which keys and server voices were found.

Any other host that runs `gunicorn app:app` (Railway, PythonAnywhere, a VPS) also works; set `TRUST_PROXY=1` behind a proxy.

## Notes

- Free tiers change often. If a model stops working ("All free models are resting"), run `python check.py`: it tests every key and model with one tiny request and tells you which one is dead. Then change that model name in .env and restart.
- Free tiers may use your prompts to improve their products. Do not speak private information.
- With no server voice available, the reply is spoken by your browser's built-in voice, and Hindi then needs a Hindi voice installed on the device.

## Sound

All sounds are generated in the browser (Web Audio), so there are no audio files and nothing to download.
Rising chime when recording opens, falling chime when it is sent, a very quiet sonar ping while it thinks, a warm chime before the voice replies,
a sparkle for XP, and short jingles for level-ups and unlocks. Errors use a soft low tone, never a harsh buzz.
The speaker button (top right) mutes everything and remembers your choice. On phones, the first tap unlocks sound; iPhones in silent mode also mute web sounds.

## Hardening notes (v4)

- Whole request has one deadline (DEADLINE, default 40 s) so a slow provider can never hang the page or trip the host timeout.
- A provider with a bad key or model name is rested for 1 hour (and logged) instead of being retried every 30 s.
- Per-visitor limits use the real connection IP. On a proxy host set TRUST_PROXY=1; the X-Forwarded-For header is never trusted blindly.
- The public /api/status shows one "energy" number only. Set DEBUG_STATUS=1 locally to see each provider.
- Run the tests: `python -m unittest -v`

## Model names (checked 9 Nov 2026)

Groq retired llama-3.3-70b-versatile and llama-3.1-8b-instant on 16 Aug 2026; Gemini 2.5 Flash-Lite had an earliest shutdown of 16 Oct 2026.
Defaults are now openai/gpt-oss-120b, openai/gpt-oss-20b (Groq), gpt-oss-120b (Cerebras) and gemini-3.1-flash-lite.
Provider catalogs change every few months, so keep `python check.py` handy.

## v6: settings menu (model, language, voice)

The slider icon (top right) opens three choices, remembered in the browser:

- **Model**: Auto, or pin one lane (Gemini, Groq 120B, Groq 20B, Cerebras, OpenRouter). Only providers that have a key appear.
  The pick goes to the front of the chain and the others stay behind it. If your pick is full for the day or errors out, the next lane answers by itself and the screen says so in amber ("... is full right now, so a backup answered"). You are never stuck.
- **I speak**: Auto, Hindi or English. Choosing Hindi makes Whisper listen for Hindi (auto-detect often mistakes Hindi for Urdu) and makes the model answer in Hindi.
- **Voice**: Auto, a named server voice, or "This device's own voice" (uses no quota).

## v6: better Hindi accent

The poor accent had two causes, and both are addressed.

1. **The voice was your device's built-in voice**, which can be robotic or missing for Hindi. Replies are now spoken by a server voice first, in this Auto order:

   | Voice | Accent quality | Cost / limit | Needs |
   |---|---|---|---|
   | Sarvam Bulbul v3 | Native Indian Hindi and Indian English | Rs 100 free credit at signup, then about Rs 3 per 1,000 characters; 2,500 characters per request | `SARVAM_API_KEY` |
   | Microsoft neural (edge-tts), `hi-IN-SwaraNeural` / `en-IN-NeerjaNeural` | Good natural Hindi and Indian English | Free, no key; unofficial service that can change or break | `pip install edge-tts` (in requirements.txt) |
   | Gemini speech | Follows plain-English accent instructions | Free tier only about 10 requests a day | `GEMINI_API_KEY` |
   | Device voice | Depends on the phone or PC | Free, offline | nothing |

   If one voice is unavailable the next one speaks, and finally the device voice. The page now also picks the best installed device voice (Google / Microsoft / "natural" voices ranked above plain ones).
2. **Hindi was written in Roman letters** ("aap kaise hain"), which any voice reads with an English accent. The model is now told to write Hindi in Devanagari and keep English words in English letters, which is what the voices are trained on.

Judge the voices yourself: with keys set, `python check.py` saves `voice-test-<engine>.mp3/wav` so you can listen to each one.
Set `SARVAM_SPEAKER`, `EDGE_VOICE_HI` or `SARVAM_PACE` in `.env` to change the speaker or slow the speech.

## Free-tier facts behind the defaults (checked 9 Nov 2026)

- Groq developer-plan base limits: gpt-oss-120b and gpt-oss-20b 30 requests/min, 1,000 requests/day, 8K tokens/min; Whisper turbo 20 requests/min, 2,000 requests/day. Groq's text-to-speech models cover English and Arabic only, so they are not used for Hindi.
- Gemini speech (`gemini-3.1-flash-tts-preview`) supports Hindi but its free quota is small and has been reported at about 10 requests/day.
- These change often. The safe caps in `.env` are set below the real limits on purpose.
