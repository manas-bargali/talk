import os, re, io, json, time, wave, base64, asyncio, threading, importlib.util
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 30 s of 16 kHz WAV is ~1 MB
if os.getenv("TRUST_PROXY"):  # set TRUST_PROXY=1 on Render/Railway/etc so the REAL visitor IP is used (not a spoofable header)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# name, base url, key env var, model, safe requests/min, safe requests/day (kept BELOW real free limits; override in .env)
# This order is also the AUTO order. A model picked in the menu is simply moved to the front; the rest stay as backups.
LLMS = [
    ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",     os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"), 8, 150),
    ("groq-120b",  "https://api.groq.com/openai/v1",                          "GROQ_API_KEY",       os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), 15, 400),
    ("groq-20b",   "https://api.groq.com/openai/v1",                          "GROQ_API_KEY",       os.getenv("GROQ_SMALL_MODEL", "openai/gpt-oss-20b"), 15, 500),
    ("cerebras",   "https://api.cerebras.ai/v1",                              "CEREBRAS_API_KEY",   os.getenv("CEREBRAS_MODEL", "gpt-oss-120b"), 15, 500),
    ("openrouter", "https://openrouter.ai/api/v1",                            "OPENROUTER_API_KEY", os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"), 10, 40),
]
LABELS = {"gemini": "Gemini", "groq-120b": "Groq · GPT-OSS 120B", "groq-20b": "Groq · GPT-OSS 20B (faster)",
          "cerebras": "Cerebras · GPT-OSS 120B", "openrouter": "OpenRouter (free)"}
VOICE_LABELS = {"sarvam": "Sarvam Bulbul (Indian voice)", "edge": "Microsoft neural (Indian)",
                "gemini": "Gemini voice", "device": "This device's own voice"}
TTS_ORDER = ("sarvam", "edge", "gemini")  # AUTO voice order: best Indian accent first, then free, then the tiny-quota one
IP_RPM, IP_RPD = int(os.getenv("IP_RPM", 6)), int(os.getenv("IP_RPD", 60))
DEADLINE = float(os.getenv("DEADLINE", 40))  # whole request must finish in this many seconds (host kills slow requests)

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.json")
lock = threading.Lock()


def system_prompt(lang="auto"):
    """The voice can only pronounce Hindi well if it is WRITTEN in Devanagari. Roman Hindi gets an English accent."""
    base = ("You are Talk, a calm voice assistant. Your answer will be spoken aloud: reply in 1 to 3 short sentences, "
            "plain text only, no markdown, lists or emojis. ")
    if lang == "en":
        return base + "Reply in English."
    hindi = ("Always write Hindi words in Devanagari script, never in Roman letters, and end Hindi sentences with '।'. "
             "Keep English words such as 'computer' or 'app' in English letters. The speaking voice reads Devanagari with a "
             "correct Hindi accent but reads Roman Hindi with a wrong English accent.")
    if lang == "hi":
        return base + "Reply in natural, simple, spoken Hindi. " + hindi
    return base + "Reply in the same language the user speaks (English, Hindi or Hinglish). " + hindi


SYSTEM = system_prompt("auto")


def load_usage(path):
    try:
        d = json.load(open(path))
    except Exception:
        return {}
    for u in d.values():
        u["until"] = 0  # a restart is a fresh start: providers that were resting get tried again
    return d


usage = load_usage(STATE)


def day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def lim(name, rpm, rpd):
    k = name.upper().replace("-", "_")
    return int(os.getenv(k + "_RPM", rpm)), int(os.getenv(k + "_RPD", rpd))


def _u(name):
    u = usage.setdefault(name, {"day": day(), "n": 0, "stamps": [], "until": 0})
    if u["day"] != day():
        u.update(day=day(), n=0)
    return u


def reserve(name, rpm, rpd):
    """Count a request BEFORE sending it. False = lane is full or resting, so skip it."""
    if not name.startswith("ip:"):
        rpm, rpd = lim(name, rpm, rpd)
    with lock:
        u, now = _u(name), time.time()
        u["seen"] = now
        u["stamps"] = [t for t in u["stamps"] if now - t < 60]
        if now < u["until"] or u["n"] >= rpd or len(u["stamps"]) >= rpm:
            return False
        u["n"] += 1
        u["stamps"].append(now)
        if len(usage) > 3000:  # forget old visitors so memory cannot grow forever
            for k in [k for k, v in usage.items() if k.startswith("ip:") and now - v.get("seen", 0) > 86400]:
                del usage[k]
        try:
            json.dump({k: v for k, v in usage.items() if not k.startswith("ip:")}, open(STATE, "w"))
        except Exception:
            pass
        return True


def cooldown(name, secs):
    with lock:
        _u(name)["until"] = time.time() + secs


def post(name, url, deadline, **kw):
    """POST with a shared deadline. Rests the lane by cause: 429 -> Retry-After, bad key/model/no credit -> 1 h, other -> 30 s."""
    left = deadline - time.time()
    if left < 3:
        return None
    try:
        r = requests.post(url, timeout=min(left, 15), **kw)
    except requests.RequestException as e:
        app.logger.warning("%s failed: %s", name, type(e).__name__)
        cooldown(name, 30)
        return None
    if r.status_code == 429:
        try:
            s = float(r.headers.get("retry-after", 60))
        except ValueError:
            s = 60
        app.logger.warning("%s rate-limited, resting %ss", name, int(s))
        cooldown(name, min(s, 3600))
    elif r.status_code in (401, 402, 403, 404):  # 402 = out of credit (Sarvam)
        app.logger.warning("%s HTTP %s: check the API key / model name / credit. %s", name, r.status_code, r.text[:150])
        cooldown(name, 3600)
    elif r.status_code >= 400:
        app.logger.warning("%s HTTP %s: %s", name, r.status_code, r.text[:150])
        cooldown(name, 30)
    else:
        try:
            return r.json()
        except ValueError:
            cooldown(name, 30)
    return None


# ---------------------------------------------------------------- hearing (speech to text)
def transcribe(wav, deadline, lang="auto"):
    """lang: 'hi' / 'en' forces the language (best for Hindi: auto-detect often mistakes Hindi for Urdu); 'auto' lets the model decide."""
    code = lang if lang in ("hi", "en") else os.getenv("STT_LANGUAGE", "")
    k = os.getenv("GROQ_API_KEY")
    if k and reserve("groq-stt", 15, 500):
        data = {"model": os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo"), "temperature": 0}
        if code:
            data["language"] = code
        if code == "hi":  # a Devanagari hint makes Whisper write Hindi in Devanagari instead of Roman or Urdu letters
            data["prompt"] = "यह हिंदी में बातचीत है।"
        j = post("groq-stt", "https://api.groq.com/openai/v1/audio/transcriptions", deadline,
                 headers={"Authorization": "Bearer " + k}, files={"file": ("a.wav", wav, "audio/wav")}, data=data)
        if j and "text" in j:
            return j["text"].strip()
    k = os.getenv("GEMINI_API_KEY")
    if k and reserve("gemini-stt", 8, 100):
        m = os.getenv("GEMINI_STT_MODEL", "gemini-3.1-flash-lite")
        ask = "Transcribe this speech exactly. Output only the transcript."
        if code == "hi":
            ask = "Transcribe this Hindi speech exactly, writing Hindi in Devanagari script and English words in English letters. Output only the transcript."
        j = post("gemini-stt", f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", deadline,
                 headers={"x-goog-api-key": k},
                 json={"contents": [{"parts": [
                     {"text": ask},
                     {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}]}]})
        try:
            return j["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- thinking (the LLM chain)
def body_for(base, model, messages):
    b = {"model": model, "messages": messages, "max_tokens": 300, "temperature": 0.6}
    if "gpt-oss" in model:  # reasoning model: it "thinks" first, so give it room and ask for little thinking
        b["max_tokens"] = 800
        if "openrouter" not in base:
            b["reasoning_effort"] = "low"
    return b


def lane_order(prefer=None):
    """Chain order, with the visitor's pick moved to the front. Everything else stays behind it as a backup."""
    return sorted(LLMS, key=lambda lane: lane[0] != prefer) if prefer else list(LLMS)


def chat(messages, deadline, prefer=None):
    for name, base, key_env, model, rpm, rpd in lane_order(prefer):
        key = os.getenv(key_env)
        if not key or time.time() > deadline - 3 or not reserve(name, rpm, rpd):
            continue
        j = post(name, base + "/chat/completions", deadline, headers={"Authorization": "Bearer " + key},
                 json=body_for(base, model, messages))
        try:
            text = speakable(j["choices"][0]["message"]["content"] or "")
            if text:
                return text, name
        except Exception:
            pass
    return None, None


def speakable(t):
    """Models sometimes ignore 'no markdown'. The voice would read symbols aloud, so strip them."""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_#`>~]+", "", t)
    t = re.sub("[\U0001F300-\U0001FAFF☀-➿]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_history(raw):
    try:
        h = json.loads(raw or "[]")
    except Exception:
        return []
    return [{"role": m["role"], "content": str(m["content"])[:500]}
            for m in h[-6:] if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]


# ---------------------------------------------------------------- speaking (server voices with an Indian accent)
def reply_lang(text):
    """'hi' if the reply contains Devanagari (Hindi or Hinglish written the right way), else 'en'."""
    return "hi" if re.search(r"[ऀ-ॿ]", text) else "en"


def wav_from_pcm(pcm, rate=24000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return b.getvalue()


def sniff(audio):
    return "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"


def tts_sarvam(text, lang, deadline):
    """Sarvam Bulbul: made in India for Indian languages, so the Hindi accent and rhythm are native. Uses paid credit (Rs 100 free on signup)."""
    k = os.getenv("SARVAM_API_KEY")
    if not k or not reserve("sarvam-tts", 20, 100):
        return None
    j = post("sarvam-tts", "https://api.sarvam.ai/text-to-speech", deadline, headers={"api-subscription-key": k},
             json={"text": text[:900], "language_code": "hi-IN" if lang == "hi" else "en-IN",
                   "speaker": os.getenv("SARVAM_SPEAKER", "shubh"), "model": os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
                   "pace": float(os.getenv("SARVAM_PACE", 1.0)), "speech_sample_rate": 24000, "output_audio_codec": "mp3"})
    try:
        audio = base64.b64decode(j["audios"][0])
        return (audio, sniff(audio)) if audio else None
    except Exception:
        return None


def tts_gemini(text, lang, deadline):
    """Gemini speech: understands accent instructions in plain words. Free tier is tiny (about 10 a day), so this is the last server lane."""
    k = os.getenv("GEMINI_API_KEY")
    if not k or not reserve("gemini-tts", 4, 8):
        return None
    style = ("Speak in a warm, natural Indian Hindi accent, like a friendly teacher from Delhi. Pronounce Hindi words correctly and English words with a light Indian accent."
             if lang == "hi" else "Speak in a warm, clear Indian English accent, like a friendly teacher.")
    m = os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    j = post("gemini-tts", f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", deadline,
             headers={"x-goog-api-key": k},
             json={"contents": [{"parts": [{"text": f"{style} Say this: {text[:900]}"}]}],
                   "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                       "prebuiltVoiceConfig": {"voiceName": os.getenv("GEMINI_TTS_VOICE", "Kore")}}}}})
    try:
        part = j["candidates"][0]["content"]["parts"][0]
        blob = part.get("inlineData") or part["inline_data"]
        pcm = base64.b64decode(blob["data"])  # raw 16-bit mono PCM, usually 24 kHz
        rate = re.search(r"rate=(\d+)", blob.get("mimeType") or blob.get("mime_type") or "")
        return (wav_from_pcm(pcm, int(rate.group(1)) if rate else 24000), "audio/wav") if pcm else None
    except Exception:
        return None


def tts_edge(text, lang, deadline):
    """Microsoft neural voices through the free edge-tts package. Unofficial and untested from here: if it breaks, this lane rests and the next one speaks."""
    if os.getenv("EDGE_TTS", "1") == "0" or importlib.util.find_spec("edge_tts") is None:
        return None
    if not reserve("edge-tts", 10, 2000):
        return None
    import edge_tts
    voice = os.getenv("EDGE_VOICE_HI", "hi-IN-SwaraNeural") if lang == "hi" else os.getenv("EDGE_VOICE_EN", "en-IN-NeerjaNeural")

    async def run():
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text[:900], voice).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    try:
        audio = asyncio.run(asyncio.wait_for(run(), timeout=max(2, min(deadline - time.time(), 12))))
    except Exception as e:
        app.logger.warning("edge-tts failed: %s", type(e).__name__)
        cooldown("edge-tts", 120)
        return None
    return (audio, "audio/mpeg") if audio else None


def available_voices():
    out = []
    if os.getenv("SARVAM_API_KEY"):
        out.append("sarvam")
    if os.getenv("EDGE_TTS", "1") != "0" and importlib.util.find_spec("edge_tts") is not None:
        out.append("edge")
    if os.getenv("GEMINI_API_KEY"):
        out.append("gemini")
    return out


def speak_server(text, lang, deadline, prefer=None):
    """Try the picked voice first, then the others. None = use the device voice in the browser."""
    order = list(TTS_ORDER)
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    for name in order:
        if deadline - time.time() < 4:
            break
        got = globals()["tts_" + name](text, lang, deadline)
        if got:
            return got[0], got[1], name
    return None


# ---------------------------------------------------------------- web
@app.errorhandler(413)
def too_big(e):
    return jsonify(error="That recording is too large. Keep it under 30 seconds."), 413


@app.errorhandler(500)
def broke(e):
    return jsonify(error="Something broke on the server. Try again in a moment."), 500


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/options")
def options():
    """What the settings menu can offer: only providers that have a key. Names only, never counts."""
    models = [{"id": "auto", "label": "Auto (best available)"}]
    models += [{"id": n, "label": LABELS.get(n, n)} for n, _, k, _, _, _ in LLMS if os.getenv(k)]
    voices = [{"id": "auto", "label": "Auto (most natural available)"}]
    voices += [{"id": v, "label": VOICE_LABELS[v]} for v in available_voices()]
    voices.append({"id": "device", "label": VOICE_LABELS["device"]})
    langs = [{"id": "auto", "label": "Auto-detect"}, {"id": "hi", "label": "हिंदी (Hindi)"}, {"id": "en", "label": "English"}]
    return jsonify(models=models, voices=voices, languages=langs)


@app.get("/api/status")
def status():
    """Public view is one number. Provider names and counts are shown only if DEBUG_STATUS is set."""
    fracs, lanes = [], {}
    for name, _, key_env, _, rpm, rpd in LLMS:
        if not os.getenv(key_env):
            continue
        _, d = lim(name, rpm, rpd)
        with lock:
            u = _u(name)
            used, rest = u["n"], time.time() < u["until"]
        fracs.append(0 if rest else max(0, d - used) / d)
        lanes[name] = {"used_today": used, "daily_cap": d, "resting": rest}
    out = {"configured": bool(fracs), "energy": round(100 * sum(fracs) / len(fracs)) if fracs else 0}
    if os.getenv("DEBUG_STATUS"):
        out["lanes"] = lanes
    return jsonify(out)


@app.post("/api/talk")
def talk():
    if not any(os.getenv(k) for _, _, k, _, _, _ in LLMS):
        return jsonify(error="The server has no API key yet. Add one in .env and restart."), 503
    if not reserve("ip:" + (request.remote_addr or "?"), IP_RPM, IP_RPD):
        return jsonify(error="You are going fast. Please wait a minute and try again."), 429
    f = request.files.get("audio")
    wav = f.read() if f else b""
    if len(wav) < 4000:
        return jsonify(error="That recording was too short. Tap, speak, then tap again."), 400
    # menu choices: anything unknown quietly becomes "auto", so a stale or tampered value can never break a request
    prefer = request.form.get("model")
    prefer = prefer if prefer in {n for n, *_ in LLMS} else None
    lang = request.form.get("lang")
    lang = lang if lang in ("hi", "en") else "auto"
    voice = request.form.get("voice")
    voice = voice if voice in TTS_ORDER + ("device",) else "auto"
    deadline = time.time() + DEADLINE
    text = transcribe(wav, deadline, lang)
    if text is None:
        return jsonify(error="Speech service is resting. Try again in a minute."), 503
    if not text:
        return jsonify(error="I couldn't hear anything. Try again closer to the mic."), 422
    history = clean_history(request.form.get("history"))
    reply, provider = chat([{"role": "system", "content": system_prompt(lang)}] + history + [{"role": "user", "content": text}],
                           deadline, prefer)
    if not reply:
        return jsonify(transcript=text, error="All free models are resting for now. Try again in a few minutes."), 503
    out = {"transcript": text, "reply": reply, "provider": provider, "provider_label": LABELS.get(provider, provider),
           "fell_back": bool(prefer and provider != prefer)}
    if voice != "device":
        spoken = speak_server(reply, reply_lang(reply), deadline, voice if voice != "auto" else None)
        if spoken:
            out.update(audio=base64.b64encode(spoken[0]).decode(), audio_type=spoken[1], voice=spoken[2],
                       voice_label=VOICE_LABELS[spoken[2]])
    return jsonify(out)


if __name__ == "__main__":
    print("API keys found:", [n for n, _, k, _, _, _ in LLMS if os.getenv(k)] or "NONE (add keys to .env)")
    print("Server voices:", available_voices() or "none (the browser's own voice will be used)")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
