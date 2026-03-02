import threading
import queue
import time
import speech_recognition as sr
import pvporcupine
import pyaudio
import struct
import os
import webbrowser
from urllib.parse import quote_plus
from dotenv import load_dotenv
from groq import Groq

# ----------------- Config & setup -----------------

load_dotenv()
client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = "llama-3.1-8b-instant"  # Groq fast model

ACCESS_KEY = "eXY0Gg0V9IxHQXvl/qH9HHIzKma9OPCvyZgGNxM2ukk9+BNrLOibng=="

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WAKE_WORD_PATH = os.path.join(
    PROJECT_ROOT, "wake-words", "Raptor_en_windows_v4_0_0.ppn"
)

print("Wake word file path:", WAKE_WORD_PATH)
print("Exists?", os.path.exists(WAKE_WORD_PATH))

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[WAKE_WORD_PATH],
)

r = sr.Recognizer()
mic = sr.Microphone()

pa = pyaudio.PyAudio()
audio_stream = pa.open(
    rate=porcupine.sample_rate,
    channels=1,
    format=pyaudio.paInt16,
    input=True,
    frames_per_buffer=porcupine.frame_length,
)

# ----------------- TTS worker (with interrupt) -----------------

tts_queue: queue.Queue[str] = queue.Queue()
tts_stop_event = threading.Event()
tts_interrupt_event = threading.Event()


def tts_worker():
    while not tts_stop_event.is_set():
        try:
            text = tts_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        # If interrupted before starting, skip this text
        if tts_interrupt_event.is_set():
            tts_queue.task_done()
            continue

        safe = text.replace('"', '\\"')
        os.system(
            f'powershell -Command "Add-Type –AssemblyName System.Speech; '
            f'$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
            f'$speak.Speak(\\"{safe}\\")"'
        )

        tts_queue.task_done()


tts_thread = threading.Thread(target=tts_worker, daemon=True)
tts_thread.start()

# ----------------- LLM helper -----------------


def ask_groq_as_parser(text: str) -> str:
    prompt = f"""
You are a command parser for a desktop Jarvis assistant.

Supported commands (INTENT):
- "open youtube"
- "search youtube"
- "open downloads"
- "goodbye"
- "none"

User says: "{text}"

Rules:
- Respond with EXACTLY ONE of the INTENT strings above.
- Do NOT add punctuation, quotes, or explanations.
- If the user asks you to search or play something on YouTube, respond with "search youtube".
- If the user just says wake words or check-ins like "jarvis", "raptor", "hello", "can you hear me", "are you there", respond with "none".
- If the user says to stop, exit, quit, or go to sleep, respond with "goodbye".
"""
    chat_completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You map user speech to one of a small set of command intents.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=16,
    )
    return chat_completion.choices[0].message.content.strip().lower()


# ----------------- Raptor helpers -----------------


def speak(text: str):
    print("Raptor:", text)
    # Tell TTS worker to drop whatever it was about to say
    tts_interrupt_event.set()
    # Queue new text
    tts_queue.put(text)
    # Reset interrupt for the worker to use on next item
    tts_interrupt_event.clear()


def handle_command(text: str) -> bool:
    print("DEBUG: handle_command got:", text)

    try:
        intent = ask_groq_as_parser(text)
        print("Groq intent:", intent)
    except Exception as e:
        print("Groq error:", e)
        intent = text.lower()

    t = intent

    if t == "open youtube" or "open youtube" in t:
        print("DEBUG: in open youtube branch")
        speak("Opening YouTube")
        webbrowser.open("https://www.youtube.com")

    elif t == "search youtube":
        print("DEBUG: in search youtube branch")
        query = quote_plus(text)
        url = f"https://www.youtube.com/results?search_query={query}"
        speak("Searching on YouTube")
        webbrowser.open(url)

    elif (
        t == "open downloads"
        or "open downloads" in t
        or "downloads folder" in t
    ):
        print("DEBUG: in open downloads branch")
        speak("Opening your Downloads folder")
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.startfile(downloads)

    elif t == "goodbye" or "goodbye" in t:
        print("DEBUG: in goodbye branch")
        speak("Goodbye. Going to sleep.")
        return False

    elif t == "none":
        print("DEBUG: in none branch")
        speak("I'm listening.")
        return True

    else:
        print("DEBUG: in fallback branch")
        speak("I am not sure how to do that yet.")
        return True

    return True


# ----------------- Listener thread -----------------


class ListenerThread(threading.Thread):
    def __init__(self, command_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.command_queue = command_queue
        self.stop_event = stop_event

    def run(self):
        print("Listener thread started. Say 'raptor' to wake me.")

        while not self.stop_event.is_set():
            # ---------- Wake-word loop ----------
            pcm = audio_stream.read(
                porcupine.frame_length,
                exception_on_overflow=False,
            )
            pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)
            keyword_index = porcupine.process(pcm)

            if keyword_index >= 0:
                speak("Yes, sir?")

                session_active = True
                while session_active and not self.stop_event.is_set():
                    # ---------- Command listening loop ----------
                    with mic as source:
                        # Interrupt any ongoing TTS so we can listen
                        tts_interrupt_event.set()

                        r.adjust_for_ambient_noise(source)
                        try:
                            audio = r.listen(
                                source,
                                timeout=3,
                                phrase_time_limit=5,
                            )
                        except sr.WaitTimeoutError:
                            # Silent timeout; keep listening quietly
                            # print("DEBUG: no speech detected in this window")
                            continue

                    try:
                        text = r.recognize_google(audio)
                        print(f"[Listener] You said: {text}")
                        self.command_queue.put(text)

                        if "goodbye" in text.lower():
                            session_active = False
                            speak("Ending session.")
                    except sr.UnknownValueError:
                        # Probably noise; ignore
                        # print("[Listener] Could not understand")
                        continue

        print("Listener thread stopping.")


# ----------------- Main loop -----------------


def main():
    cmd_queue: queue.Queue[str] = queue.Queue()
    stop_event = threading.Event()

    listener = ListenerThread(cmd_queue, stop_event)
    listener.start()

    try:
        session_alive = True
        while session_alive:
            try:
                text = cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            session_alive = handle_command(text)

            if not session_alive:
                stop_event.set()
                break

    except KeyboardInterrupt:
        speak("Stopping now. Goodbye.")
        stop_event.set()

    finally:
        audio_stream.close()
        pa.terminate()
        porcupine.delete()
        tts_stop_event.set()
        tts_thread.join(timeout=1.0)
        print("Cleaned up audio and TTS.")


if __name__ == "__main__":
    main()
