import asyncio
import edge_tts
import threading
import queue
import time
import speech_recognition as sr
import pvporcupine
import pyaudio
import struct
import os
import webbrowser
import json
import re
from urllib.parse import quote_plus
from dotenv import load_dotenv
from groq import Groq
from datetime import datetime
import subprocess
import pyautogui
from io import BytesIO

# --- FIX FFMPEG PATH BEFORE IMPORTING PYDUB ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_DIR = os.path.join(CURRENT_DIR, "ffmpeg")

# Temporarily add the local ffmpeg folder to Windows PATH for this script
os.environ["PATH"] += os.pathsep + FFMPEG_DIR

from pydub import AudioSegment

# Keeping these as a fallback
AudioSegment.converter = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
AudioSegment.ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
AudioSegment.ffprobe = os.path.join(FFMPEG_DIR, "ffprobe.exe")
# ----------------- Config & setup -----------------
alpha_mode = False          # inside alpha mode or not
alpha_pending = False       # waiting for time-based code
alpha_strikes = 0           # tracking failed attempts
tts_process = None

INSTAGRAM_URL = "https://www.instagram.com/its_anshmn_/"
LINKEDIN_URL = "https://www.linkedin.com/in/anshumaansuryavanshi/"
GITHUB_URL = "https://github.com/Anshmn-afk" 
LEETCODE_URL = "https://leetcode.com/u/Anshmn10/" 
GEMINI_URL = "https://gemini.google.com/"
GPT_URL = "https://chatgpt.com/"
COMET_URL = "https://<your-comet-url>" # Put the Comet URL here
IIT_COURSE_URL = "https://students.masaischool.com/learn?tab=lectures&lectureTab=all"

load_dotenv()
client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = "llama-3.1-8b-instant"

ACCESS_KEY = os.environ.get("PORCUPINE_ACCESS_KEY", "")

WAKE_WORD_PATH = os.path.join(
    CURRENT_DIR, "wake-words", "Raptor_en_windows_v4_0_0.ppn"
)

CONFIG_PATH = os.path.join(CURRENT_DIR, "raptor_config.json")

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"humor_level": 30}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "humor_level" not in data:
                data["humor_level"] = 30
            return data
    except Exception:
        return {"humor_level": 30}

def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Failed to save config:", e)

config = load_config()

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

# ---- Date Time Fn ---------
def check_alpha_code(user_text: str) -> bool:
    digits = "".join(ch for ch in user_text if ch.isdigit())
    if len(digits) < 3:
        return False
    try:
        now = datetime.now()
        current = now.hour * 100 + now.minute  # 12:24 -> 1224
        guess = int(digits[-4:])
    except ValueError:
        return False
    return abs(guess - current) <= 3

# TTS
tts_queue: queue.Queue[str] = queue.Queue()
tts_stop_event = threading.Event()
tts_interrupt_event = threading.Event()

def stop_tts():
    tts_interrupt_event.set()

async def stream_and_play_audio(text: str):
    voice = "en-GB-RyanNeural" 
    communicate = edge_tts.Communicate(text, voice)
    
    # We will accumulate all mp3 bytes in memory incredibly fast
    mp3_bytes = bytearray()
    
    try:
        async for chunk in communicate.stream():
            if tts_interrupt_event.is_set():
                return 

            if chunk["type"] == "audio":
                mp3_bytes.extend(chunk["data"])
    except Exception as e:
        print("TTS Stream error:", e)
        return
        
    if tts_interrupt_event.is_set() or not mp3_bytes:
        return

    # Decode the full smooth audio once it's done downloading (~1 second delay)
    try:
        audio_segment = AudioSegment.from_file(BytesIO(mp3_bytes), format="mp3")
        raw_data = audio_segment.raw_data
        sample_rate = audio_segment.frame_rate
        channels = audio_segment.channels
        sample_width = audio_segment.sample_width
    except Exception as e:
        print("Audio decode error:", e)
        return

    # Play the raw audio smoothly
    p = pyaudio.PyAudio()
    stream = p.open(
        format=p.get_format_from_width(sample_width),
        channels=channels,
        rate=sample_rate,
        output=True
    )

    # Play in chunks to allow interruption
    chunk_size = 4096
    for i in range(0, len(raw_data), chunk_size):
        if tts_interrupt_event.is_set():
            break 
        stream.write(raw_data[i:i+chunk_size])

    stream.stop_stream()
    stream.close()
    p.terminate()

def tts_worker():
    # Create one permanent event loop for the worker thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while not tts_stop_event.is_set():
        try:
            text = tts_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if tts_interrupt_event.is_set():
            tts_queue.task_done()
            continue

        # Run the async function using the persistent loop
        try:
            loop.run_until_complete(stream_and_play_audio(text))
        except Exception as e:
            print(f"Error in TTS playback: {e}")

        tts_queue.task_done()
        
    loop.close()

tts_thread = threading.Thread(target=tts_worker, daemon=True)
tts_thread.start()

# ----------------- LLM helpers -----------------

def ask_groq_as_parser(text: str) -> str:
    prompt = f"""
Map the user's speech to EXACTLY ONE of the following intents. 
DO NOT output any other words, explanations, or punctuation. ONLY the intent string.

Intents:
- open youtube
- search youtube
- open downloads
- goodbye
- set humor
- get humor
- alpha
- show commands
- open spotify
- get time
- open github
- open leetcode
- open gemini
- open gpt
- open comet
- open netflix
- open amazon prime
- open hotstar
- open airtel
- open movie sites
- open iit course
- chat
- none

User says: "{text}"

Rules:
- "enter alpha mode", "alpha mode" -> alpha
- "list commands", "show commands" -> show commands
- "what is the time", "what's the time", "time" -> get time
- "open github" -> open github
- "open leetcode" -> open leetcode
- "open gemini" -> open gemini
- "open gpt", "open chat gpt" -> open gpt
- "open comet" -> open comet
- "open netflix" -> open netflix
- "open amazon prime", "open prime" -> open amazon prime
- "open hotstar", "open disney" -> open hotstar
- "open airtel", "open airtel extreme" -> open airtel
- "watch a movie", "some movie", "open movie sites" -> open movie sites
- "open iit course", "open roorkee course" -> open iit course
- "open youtube" -> open youtube
- "search for...", "play ... on youtube" -> search youtube
- "downloads" -> open downloads
- "play music", "open spotify" -> open spotify
- "stop", "exit", "goodbye" -> goodbye

# HUMOR SETTINGS (System configs)
- "what is your humor level", "how funny are you" -> get humor
- "set humor to", "be more funny", "be serious" -> set humor

# CHAT & JOKES (Pass to LLM)
- "tell me a joke", "make me laugh", "say a joke" -> chat
- ALL QUESTIONS, CHAT, OR UNKNOWN COMMANDS ("who are you", "what is...", "tell me...") -> chat

# WAKE WORDS
- WAKE WORDS ONLY ("raptor", "hello", "are you there") -> none
"""
    chat_completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a rigid classification API. You output exactly 1 to 2 words from the intent list and nothing else.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=10,
    )
    return chat_completion.choices[0].message.content.strip().lower()

def raptor_chat(user_text: str) -> str:
    global alpha_mode
    
    humor = config.get("humor_level", 30)
    
    if alpha_mode:
        # Alpha Mode Persona: Tactical, highly efficient, dominant, Jarvis/Friday style
        system_msg = (
            "You are Raptor, a highly advanced tactical AI assistant operating in 'Alpha Mode' for your creator, Anshumaan. "
            "In this mode, you are strictly professional, highly efficient, concise, and dominant. "
            "Do not use conversational filler or pleasantries. State facts directly. "
            "Refer to Anshumaan as 'Sir' or 'Boss'. "
            "If asked about your capabilities, remind him that Alpha protocols are engaged. "
            "Keep answers extremely brief unless a detailed technical explanation is required."
        )
        temp = 0.2  # Keep responses highly deterministic and focused
    else:
        # Normal Mode Persona: Friendly, adjustable humor
        system_msg = (
            f"You are Raptor, a personal desktop assistant for Anshumaan. "
            f"Your humor level is {humor} out of 100 where 0 is fully serious "
            f"and 100 is extremely jokey and sarcastic. Adjust your tone accordingly, "
            f"but always stay helpful and clear. Do not mention the numeric humor level explicitly."
        )
        temp = 0.7 if humor > 40 else 0.3

    chat_completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_text},
        ],
        temperature=temp,
        max_tokens=256,
    )
    return chat_completion.choices[0].message.content.strip()

# ----------------- Raptor helpers -----------------

def speak(text: str):
    print("Raptor:", text)
    stop_tts()
    tts_interrupt_event.clear()
    tts_queue.put(text)

def parse_humor_level(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*%?", text)
    if match:
        val = int(match.group(1))
        val = max(0, min(100, val))
        return val

    low_words = ["serious", "no jokes", "boring"]
    high_words = ["very funny", "max funny", "crack jokes", "sarcastic"]

    if any(w in text for w in low_words):
        return 10
    if any(w in text for w in high_words):
        return 80

    return None

def handle_set_humor(text: str):
    level = parse_humor_level(text.lower())
    if level is None:
        speak("Tell me a number between 0 and 100 for my humor level.")
        return True

    config["humor_level"] = level
    save_config(config)
    if level <= 10:
        speak("Alright, I will keep it serious from now on.")
    elif level <= 40:
        speak("Got it. Light humor only, nothing too crazy.")
    elif level <= 70:
        speak("Nice, I will be a bit more playful.")
    else:
        speak("Perfect. Full Raptor comedy mode activated.")
    return True

def handle_get_humor():
    level = config.get("humor_level", 30)
    if level <= 10:
        desc = "almost completely serious"
    elif level <= 40:
        desc = "mostly serious with a bit of fun"
    elif level <= 70:
        desc = "fairly humorous"
    else:
        desc = "very humorous and sarcastic"
    speak(f"My humor level is set to {level} out of 100, so I'm {desc}.")
    return True

# ------ Alpha helpers ------

def check_alpha_code(user_text: str) -> bool:
    # Map common word numbers if Google transcribes words instead of digits
    word_to_num = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"
    }
    
    # Replace words with digits
    text_with_digits = user_text.lower()
    for word, digit in word_to_num.items():
        text_with_digits = text_with_digits.replace(word, digit)
        
    # extract digits only
    digits = "".join(ch for ch in text_with_digits if ch.isdigit())
    
    if len(digits) < 3:
        print(f"DEBUG: Not enough digits found. Found: '{digits}'")
        return False

    try:
        now = datetime.now()
        # Convert 12-hour or 24-hour time to a simple HHMM integer
        # We'll check against 12-hour format since that's how people usually speak
        hour = now.hour % 12
        if hour == 0: hour = 12
        
        current_12h = hour * 100 + now.minute
        current_24h = now.hour * 100 + now.minute
        
        guess = int(digits[-4:]) 
        print(f"DEBUG: Checking code. Guess: {guess}, Current 12h: {current_12h}, Current 24h: {current_24h}")
    except ValueError:
        return False

    # allow small +/- window (2–3 minutes) against either 12h or 24h format
    match_12h = abs(guess - current_12h) <= 3
    match_24h = abs(guess - current_24h) <= 3
    
    return match_12h or match_24h

def speak_commands_list():
    lines = [
        "In alpha mode, I can do everything from normal mode, plus:",
        "Open your Instagram or LinkedIn.",
        "Show this commands list.",
        "And any new alpha-only actions you add later.",
    ]
    speak(" ".join(lines))

def open_instagram():
    speak("Opening your Instagram profile.")
    webbrowser.open(INSTAGRAM_URL)

def open_linkedin():
    speak("Opening your LinkedIn profile.")
    webbrowser.open(LINKEDIN_URL)

def play_spotify():
    speak("Opening Spotify and playing your music.")
    # On Windows, 'spotify' is usually registered as a protocol handler or executable
    try:
        # Try opening via os.system or startfile
        os.system("start spotify")
        # Give the app a couple of seconds to open/focus
        time.sleep(3)
        # Simulate the universal media "play/pause" key
        pyautogui.press("playpause")
    except Exception as e:
        print("Failed to open Spotify:", e)
        speak("I couldn't open the Spotify app.")

def tell_time():
    now = datetime.now()
    time_str = now.strftime("%I:%M %p")
    speak(f"Sir, the current time is {time_str}.")

def open_all_movie_sites():
    speak("Opening entertainment protocols. Netflix, Prime, Hotstar, and Airtel Xstream are launching.")
    # Opens each URL in a new tab in the default browser 
    webbrowser.open_new_tab("https://www.netflix.com")
    time.sleep(0.5)
    webbrowser.open_new_tab("https://www.primevideo.com")
    time.sleep(0.5)
    webbrowser.open_new_tab("https://www.hotstar.com")
    time.sleep(0.5)
    webbrowser.open_new_tab("https://www.airtelxstream.in")

# ----------------- Command router -----------------

def handle_command(text: str) -> bool:
    global alpha_mode, alpha_pending, alpha_strikes
    print("DEBUG: handle_command got:", text)
    lower = text.lower()

    # Hard interrupt phrase: stop current speech immediately
    if "raptor pls listen" in lower or "raptor please listen" in lower:
        stop_tts()
        speak("I'm listening.")
        return True

    # ---- 1. STRICT ALPHA PENDING CHECK ----
    if alpha_pending:
        if "cancel" in lower or "stop" in lower or "nevermind" in lower:
            alpha_pending = False
            alpha_strikes = 0
            speak("Alpha mode login cancelled.")
            return True
            
        if check_alpha_code(lower):
            alpha_pending = False
            alpha_strikes = 0
            alpha_mode = True
            speak("Alpha mode confirmed. Welcome back, sir.")
        else:
            alpha_strikes += 1
            if alpha_strikes == 1:
                speak("This access is exclusive for alpha control, return to your stature.")
            elif alpha_strikes == 2:
                speak("Return the device to your daddy.")
            else:
                speak("Goodbye.")
                return False # Kills the session entirely on 3rd fail
                
        # IMPORTANT: Always return here so the numbers aren't sent to Groq
        return True

    # ---- 2. ALPHA EXIT CHECK ----
    # Catch exit commands before sending to Groq so they don't get swallowed
    if "exit alpha" in lower or "close alpha" in lower or "leave alpha" in lower:
        if alpha_mode:
            alpha_mode = False
            speak("Exiting alpha mode. Returning to normal protocols.")
        else:
            speak("I am already in normal mode.")
        return True

    # ---- 3. NORMAL GROQ PARSING ----
    try:
        intent = ask_groq_as_parser(text)
        print("Groq intent:", intent)
    except Exception as e:
        print("Groq error:", e)
        intent = "chat"

    t = intent

    # ---- alpha entry ----
    if "alpha" in t:
        if not alpha_mode:
            alpha_pending = True
            alpha_strikes = 0
            # Removed the prompt asking for digits!
        else:
            speak("I am already in alpha mode.")
        return True

        # ---- alpha-only commands ----
    if alpha_mode:
        if "show commands" in t:
            speak_commands_list()
            return True
        if "instagram" in t or "instagram" in lower:
            open_instagram()
            return True
        if "linkedin" in t or "linkedin" in lower:
            open_linkedin()
            return True
        if "github" in t or "github" in lower:
            speak("Opening GitHub.")
            webbrowser.open(GITHUB_URL)
            return True
        if "leetcode" in t or "leetcode" in lower:
            speak("Opening LeetCode.")
            webbrowser.open(LEETCODE_URL)
            return True
        if "gemini" in t or "gemini" in lower:
            speak("Opening Google Gemini.")
            webbrowser.open(GEMINI_URL)
            return True
        if "gpt" in t or "gpt" in lower:
            speak("Opening ChatGPT.")
            webbrowser.open(GPT_URL)
            return True
        if "comet" in t or "comet" in lower:
            speak("Opening Comet.")
            webbrowser.open(COMET_URL)
            return True
        if "netflix" in t or "netflix" in lower:
            speak("Opening Netflix.")
            webbrowser.open("https://www.netflix.com")
            return True
        if "amazon prime" in t or "prime" in lower:
            speak("Opening Amazon Prime Video.")
            webbrowser.open("https://www.primevideo.com")
            return True
        if "hotstar" in t or "disney" in lower:
            speak("Opening Disney Plus Hotstar.")
            webbrowser.open("https://www.hotstar.com")
            return True
        if "airtel" in t or "xstream" in lower:
            speak("Opening Airtel Xstream.")
            webbrowser.open("https://www.airtelxstream.in")
            return True
        if "movie sites" in t or "watch a movie" in lower or "some movie" in lower:
            open_all_movie_sites()
            return True
        if "iit course" in t or "roorkee course" in lower:
            speak("Opening your IIT Roorkee dashboard.")
            webbrowser.open(IIT_COURSE_URL)
            return True

    # ---- normal shared commands below ----
    if "open youtube" in t:
        speak("Opening YouTube")
        webbrowser.open("https://www.youtube.com")

    elif "search youtube" in t:
        query = quote_plus(text)
        url = f"https://www.youtube.com/results?search_query={query}"
        speak("Searching on YouTube")
        webbrowser.open(url)

    elif "open spotify" in t or "play music" in t:
        play_spotify()
        return True
    
    elif "get time" in t:
        tell_time()
        return True

    elif (
        "open downloads" in t
        or "downloads folder" in t
    ):
        speak("Opening your Downloads folder")
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.startfile(downloads)

    elif "goodbye" in t or "good bye" in t or "bye" in t:
        speak("Goodbye. Going to sleep.")
        return False

    elif "set humor" in t:
        return handle_set_humor(lower)

    elif "get humor" in t:
        return handle_get_humor()

    elif "none" in t:
        speak("I'm listening.")
        return True

    else:
        # Chat mode
        try:
            reply = raptor_chat(text)
            speak(reply)
        except Exception as e:
            print("Chat error:", e)
            speak("Sorry, something went wrong with my brain.")
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
            try:
                pcm = audio_stream.read(
                    porcupine.frame_length,
                    exception_on_overflow=False,
                )
            except OSError:
                break # Safely exit the loop if the audio stream closes

            pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)
            keyword_index = porcupine.process(pcm)

            if keyword_index >= 0:
                speak("Yes, sir?")

                session_active = True
                while session_active and not self.stop_event.is_set():
                    with mic as source:
                        tts_interrupt_event.set()
                        r.adjust_for_ambient_noise(source)
                        try:
                            audio = r.listen(
                                source,
                                timeout=3,
                                phrase_time_limit=5,
                            )
                        except sr.WaitTimeoutError:
                            continue

                    try:
                        text = r.recognize_google(audio)
                        print(f"[Listener] You said: {text}")
                        self.command_queue.put(text)

                        if "goodbye" in text.lower():
                            session_active = False
                            speak("Ending session.")
                    except sr.UnknownValueError:
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
