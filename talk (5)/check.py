"""Tests each configured provider with ONE tiny request and says exactly what is wrong.  Run:  python check.py"""
import os, io, math, wave, struct, base64, requests
import app as a

def tone_wav():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)))
    return b.getvalue()

def report(label, r):
    if r.status_code < 300:
        print("OK    " + label); return True
    print("FAIL  %s -> HTTP %s: %s" % (label, r.status_code, r.text[:180].replace("\n", " "))); return False

bad = 0
for name, base, key_env, model, *_ in a.LLMS:
    k = os.getenv(key_env)
    if not k:
        print("skip  %s (no %s in .env)" % (name, key_env)); continue
    try:
        r = requests.post(base + "/chat/completions", headers={"Authorization": "Bearer " + k}, timeout=25,
                          json=a.body_for(base, model, [{"role": "user", "content": "Say hello in four words."}]))
        good = report("%s (%s)" % (name, model), r)
        if good:
            text = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if not text: print("FAIL  %s returned an empty reply" % name); good = False
            else: print("      reply: " + text[:60])
        bad += not good
    except Exception as e:
        print("FAIL  %s -> %s" % (name, type(e).__name__)); bad += 1

wav = tone_wav()
if os.getenv("GROQ_API_KEY"):
    m = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=25,
                      headers={"Authorization": "Bearer " + os.getenv("GROQ_API_KEY")},
                      files={"file": ("a.wav", wav, "audio/wav")}, data={"model": m})
    bad += not report("groq speech-to-text (%s)" % m, r)
if os.getenv("GEMINI_API_KEY"):
    m = os.getenv("GEMINI_STT_MODEL", "gemini-3.1-flash-lite")
    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m, timeout=25,
                      headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY")},
                      json={"contents": [{"parts": [{"text": "What is this sound?"}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}]}]})
    bad += not report("gemini speech-to-text (%s)" % m, r)

# ---- voices (v6): one short sentence per configured server voice, so you can hear which one sounds best
import time
a.STATE = os.devnull  # do not write usage.json
for engine in ("sarvam", "edge", "gemini"):
    if engine not in a.available_voices():
        print("skip  %s voice (not configured)" % engine); continue
    got = getattr(a, "tts_" + engine)("नमस्ते, मैं टॉक हूँ। आप कैसे हैं?", "hi", time.time() + 25)
    if got:
        fn = "voice-test-%s.%s" % (engine, "wav" if got[1] == "audio/wav" else "mp3")
        open(fn, "wb").write(got[0]); print("OK    %s voice -> saved %s (play it to judge the accent)" % (engine, fn))
    else:
        print("FAIL  %s voice returned nothing (see the warning above, or check key / credit / package)" % engine); bad += 1

print("\nAll good." if not bad else "\n%d problem(s). Fix the model name or key above in .env, then restart." % bad)
