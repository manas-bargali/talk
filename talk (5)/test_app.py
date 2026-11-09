import os, io, time, base64, wave, unittest
from unittest import mock
import app as a

a.STATE = os.devnull  # tests must not write usage.json


def fresh():
    """Clear lane counters so one test cannot starve another."""
    a.usage.clear()


class Tests(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_limiter_counts_and_blocks(self):
        n = "t-%s" % time.time()
        self.assertEqual([a.reserve(n, 2, 5) for _ in range(4)], [True, True, False, False])

    def test_cooldown_blocks_lane(self):
        n = "c-%s" % time.time()
        a.cooldown(n, 60)
        self.assertFalse(a.reserve(n, 5, 5))

    def test_restart_clears_resting(self):
        import json, tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"gemini": {"day": "2000-01-01", "n": 3, "stamps": [], "until": time.time() + 3600}}, f); f.close()
        self.assertEqual(a.load_usage(f.name)["gemini"]["until"], 0)
        os.unlink(f.name)

    def test_reasoning_models_get_room_to_think(self):
        b = a.body_for("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", [])
        self.assertGreaterEqual(b["max_tokens"], 800)
        self.assertEqual(b["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", a.body_for("https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free", []))

    def test_speakable_strips_symbols(self):
        self.assertEqual(a.speakable("**Hi** there! \U0001F600 [link](http://x.y)\n- item"), "Hi there! link item")

    def test_history_is_trimmed_and_validated(self):
        raw = '[{"role":"user","content":"a"},{"role":"system","content":"evil"},{"role":"assistant","content":"' + "x" * 900 + '"}]'
        h = a.clean_history(raw)
        self.assertEqual([m["role"] for m in h], ["user", "assistant"])
        self.assertEqual(len(h[1]["content"]), 500)

    def test_public_status_hides_provider_details(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "x"}):
            r = a.app.test_client().get("/api/status").get_json()
        self.assertIn("energy", r)
        self.assertNotIn("lanes", r)

    def test_oversize_upload_returns_json(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "x"}):
            r = a.app.test_client().post("/api/talk", data={"audio": (io.BytesIO(b"0" * 4_000_000), "a.wav")},
                                         content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)
        self.assertIn("error", r.get_json())

    def test_no_keys_gives_clear_message(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = a.app.test_client().post("/api/talk")
        self.assertEqual(r.status_code, 503)
        self.assertIn("API key", r.get_json()["error"])

    def test_spoofed_forwarded_header_does_not_change_visitor(self):
        with a.app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            self.assertEqual(a.request.remote_addr, "9.9.9.9")

    # ---- v6: model menu ----
    def test_picked_model_goes_first_and_rest_stay_as_backups(self):
        names = [l[0] for l in a.lane_order("groq-120b")]
        self.assertEqual(names[0], "groq-120b")
        self.assertEqual(names[1:], [n for n in [l[0] for l in a.LLMS] if n != "groq-120b"])
        self.assertEqual([l[0] for l in a.lane_order(None)], [l[0] for l in a.LLMS])

    def _reply(self, text):
        return {"choices": [{"message": {"content": text}}]}

    def test_chat_uses_picked_model(self):
        seen = []
        def fake(name, url, deadline, **kw):
            seen.append(name); return self._reply("hi")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}, clear=True), mock.patch.object(a, "post", fake):
            self.assertEqual(a.chat([], time.time() + 30, "groq-120b"), ("hi", "groq-120b"))
        self.assertEqual(seen, ["groq-120b"])

    def test_chat_falls_back_when_picked_model_is_full(self):
        for _ in range(400):  # fill groq-120b's safe daily cap
            a.reserve("groq-120b", 15, 400) or None
        a.cooldown("groq-120b", 60)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}, clear=True), \
                mock.patch.object(a, "post", lambda name, *x, **k: self._reply("ok")):
            text, who = a.chat([], time.time() + 30, "groq-120b")
        self.assertEqual(who, "gemini")  # next in chain order, picked lane skipped

    def test_chat_falls_back_when_picked_model_errors(self):
        def fake(name, url, deadline, **kw):
            return None if name == "gemini" else self._reply("from groq")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}, clear=True), mock.patch.object(a, "post", fake):
            self.assertEqual(a.chat([], time.time() + 30, "gemini"), ("from groq", "groq-120b"))

    def test_options_only_list_configured_providers(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True):
            r = a.app.test_client().get("/api/options").get_json()
        ids = [m["id"] for m in r["models"]]
        self.assertEqual(ids, ["auto", "groq-120b", "groq-20b"])
        self.assertEqual(r["voices"][0]["id"], "auto")
        self.assertEqual(r["voices"][-1]["id"], "device")
        self.assertEqual([l["id"] for l in r["languages"]], ["auto", "hi", "en"])

    # ---- v6: accent ----
    def test_hindi_prompt_demands_devanagari(self):
        for lang in ("hi", "auto"):
            self.assertIn("Devanagari", a.system_prompt(lang))
        self.assertNotIn("Devanagari", a.system_prompt("en"))

    def test_reply_lang(self):
        self.assertEqual(a.reply_lang("नमस्ते, how are you"), "hi")
        self.assertEqual(a.reply_lang("Hello there"), "en")

    def test_whisper_gets_language_and_devanagari_hint_for_hindi(self):
        got = {}
        def fake(name, url, deadline, **kw):
            got.update(kw["data"]); return {"text": " नमस्ते "}
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True), mock.patch.object(a, "post", fake):
            self.assertEqual(a.transcribe(b"x", time.time() + 30, "hi"), "नमस्ते")
        self.assertEqual(got["language"], "hi")
        self.assertIn("prompt", got)

    def test_whisper_auto_sends_no_language(self):
        got = {}
        def fake(name, url, deadline, **kw):
            got.update(kw["data"]); return {"text": "hello"}
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True), mock.patch.object(a, "post", fake):
            a.transcribe(b"x", time.time() + 30, "auto")
        self.assertNotIn("language", got)

    def test_sarvam_request_and_response(self):
        sent = {}
        audio = b"ID3" + b"\x00" * 20
        def fake(name, url, deadline, **kw):
            sent.update(url=url, **kw); return {"audios": [base64.b64encode(audio).decode()]}
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "s"}, clear=True), mock.patch.object(a, "post", fake):
            got = a.tts_sarvam("नमस्ते", "hi", time.time() + 30)
        self.assertEqual(got, (audio, "audio/mpeg"))
        self.assertEqual(sent["json"]["language_code"], "hi-IN")
        self.assertEqual(sent["headers"], {"api-subscription-key": "s"})

    def test_gemini_pcm_becomes_a_playable_wav(self):
        pcm = b"\x01\x00" * 2400
        resp = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000",
                                                                       "data": base64.b64encode(pcm).decode()}}]}}]}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g"}, clear=True), mock.patch.object(a, "post", lambda *x, **k: resp):
            audio, kind = a.tts_gemini("hello", "en", time.time() + 30)
        self.assertEqual(kind, "audio/wav")
        w = wave.open(io.BytesIO(audio))
        self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()), (24000, 1, 2, 2400))

    def test_voice_chain_picks_then_falls_back(self):
        calls = []
        def mk(name, ok):
            def f(text, lang, deadline):
                calls.append(name); return (b"A", "audio/mpeg") if ok else None
            return f
        with mock.patch.object(a, "tts_sarvam", mk("sarvam", False)), mock.patch.object(a, "tts_edge", mk("edge", True)), \
                mock.patch.object(a, "tts_gemini", mk("gemini", True)):
            self.assertEqual(a.speak_server("x", "hi", time.time() + 30), (b"A", "audio/mpeg", "edge"))
            self.assertEqual(calls, ["sarvam", "edge"])
            calls.clear()
            self.assertEqual(a.speak_server("x", "hi", time.time() + 30, "gemini")[2], "gemini")
            self.assertEqual(calls, ["gemini"])

    def test_voice_chain_gives_up_cleanly(self):
        with mock.patch.object(a, "tts_sarvam", lambda *x: None), mock.patch.object(a, "tts_edge", lambda *x: None), \
                mock.patch.object(a, "tts_gemini", lambda *x: None):
            self.assertIsNone(a.speak_server("x", "en", time.time() + 30))

    def test_edge_lane_is_skipped_when_package_missing_or_disabled(self):
        with mock.patch.dict(os.environ, {"EDGE_TTS": "0"}):
            self.assertIsNone(a.tts_edge("x", "hi", time.time() + 30))
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("importlib.util.find_spec", return_value=None):
            self.assertIsNone(a.tts_edge("x", "hi", time.time() + 30))

    def test_payment_required_rests_the_lane_for_an_hour(self):
        r = mock.Mock(status_code=402, text="no credit", headers={})
        with mock.patch.object(a.requests, "post", return_value=r):
            self.assertIsNone(a.post("sarvam-tts", "http://x", time.time() + 30))
        self.assertFalse(a.reserve("sarvam-tts", 20, 100))

    def test_talk_end_to_end_with_menu_choices(self):
        wav = b"RIFF" + b"\x00" * 5000
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True), \
                mock.patch.object(a, "transcribe", lambda w, d, lang="auto": "नमस्ते"), \
                mock.patch.object(a, "chat", lambda m, d, prefer=None: ("जी हाँ।", "groq-20b")), \
                mock.patch.object(a, "tts_sarvam", lambda *x: (b"AUD", "audio/mpeg")):
            r = a.app.test_client().post("/api/talk", data={"audio": (io.BytesIO(wav), "a.wav"), "model": "gemini", "lang": "hi", "voice": "sarvam"},
                                         content_type="multipart/form-data").get_json()
        self.assertEqual(r["provider"], "groq-20b")
        self.assertTrue(r["fell_back"])  # asked for gemini, groq answered
        self.assertEqual(r["voice"], "sarvam")
        self.assertEqual(base64.b64decode(r["audio"]), b"AUD")

    def test_device_voice_skips_server_speech(self):
        wav = b"RIFF" + b"\x00" * 5000
        boom = mock.Mock(side_effect=AssertionError("server voice must not run"))
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True), \
                mock.patch.object(a, "transcribe", lambda *x, **k: "hello"), \
                mock.patch.object(a, "chat", lambda *x, **k: ("Hi.", "groq-120b")), mock.patch.object(a, "speak_server", boom):
            r = a.app.test_client().post("/api/talk", data={"audio": (io.BytesIO(wav), "a.wav"), "voice": "device"},
                                         content_type="multipart/form-data").get_json()
        self.assertNotIn("audio", r)
        self.assertFalse(r["fell_back"])

    def test_tampered_menu_values_become_auto(self):
        wav = b"RIFF" + b"\x00" * 5000
        seen = {}
        def fake_chat(m, d, prefer=None):
            seen["prefer"] = prefer; return ("ok", "groq-120b")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "q"}, clear=True), \
                mock.patch.object(a, "transcribe", lambda *x, **k: "hello"), mock.patch.object(a, "chat", fake_chat), \
                mock.patch.object(a, "speak_server", lambda *x, **k: None):
            r = a.app.test_client().post("/api/talk", data={"audio": (io.BytesIO(wav), "a.wav"), "model": "../etc", "voice": "evil", "lang": "xx"},
                                         content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(seen["prefer"])


if __name__ == "__main__":
    unittest.main()
