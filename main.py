import sounddevice as sd
import numpy as np
import threading
import socketserver
import socket
import http.server
import subprocess
import sys
import requests
import time
import json
import os
import platform
import shutil
from typing import Optional
from pathlib import Path
import ctypes
import warnings
import tkinter as tk
from tkinter import simpledialog, messagebox
import pystray
from pystray import MenuItem as item, Menu
from PIL import Image, ImageDraw

try:
    import soundcard as sc  # Better Windows loopback capture than PortAudio WASAPI loopback
except Exception:
    sc = None

# soundcard can be chatty about discontinuities; those are common under load and usually harmless.
warnings.filterwarnings(
    "ignore",
    message="data discontinuity in recording",
    category=Warning,
)

PORT = 9000
SAMPLE_RATE = 44100
CHANNELS = 2
BITRATE = "192k"

# OS detection
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

def get_app_dir() -> str:
    # When packaged, prefer the exe/app location.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def resolve_ffmpeg_path() -> str:
    exe_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"

    candidate_dirs: list[str] = []
    # 1) Next to the exe (best for "plug and play")
    candidate_dirs.append(get_app_dir())
    # 2) In the PyInstaller temporary extraction dir (onefile)
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        candidate_dirs.append(meipass)
    # 3) Script directory (dev mode)
    candidate_dirs.append(os.path.dirname(os.path.abspath(__file__)))

    for base_dir in candidate_dirs:
        direct = os.path.join(base_dir, exe_name)
        if os.path.exists(direct):
            return direct
        in_bin = os.path.join(base_dir, "bin", exe_name)
        if os.path.exists(in_bin):
            return in_bin

    # 4) Fallback to PATH
    found = shutil.which(exe_name)
    if found:
        return found

    # Last resort; will error with a clear message.
    return exe_name

FFMPEG_PATH = resolve_ffmpeg_path()

# --- Config persistence ---
# Goal: keep distribution as a single executable.
# - Windows: store config in registry (no extra files next to the exe)
# - macOS/Linux: store config in user config directory (still "single app file" distribution)

REGISTRY_KEY_PATH = r"Software\SonosStreamer"

def _get_user_config_path() -> Path:
    home = Path.home()
    if IS_MAC:
        return home / "Library" / "Application Support" / "SonosStreamer" / "config.json"
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(home)
        return Path(base) / "SonosStreamer" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / "sonos-streamer" / "config.json"

def load_config() -> dict:
    # Legacy: previous versions stored config next to the script/exe.
    # If we find it and have no other config yet, import it once.
    def _try_load_legacy_file() -> dict:
        for base_dir in (get_app_dir(), os.path.dirname(os.path.abspath(__file__))):
            legacy_path = os.path.join(base_dir, "sonos_streamer_config.json")
            try:
                if os.path.exists(legacy_path):
                    with open(legacy_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                continue
        return {}

    if IS_WINDOWS:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH) as key:
                out: dict = {}
                for name in ("domain", "host", "password", "audio_mode", "public_enabled"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        if isinstance(value, str) and value:
                            if name == "public_enabled":
                                out[name] = value.strip() in ("1", "true", "True", "yes", "on")
                            else:
                                out[name] = value
                    except FileNotFoundError:
                        pass
                return out
        except Exception:
            # Fall back to file-based config below
            pass

    # If nothing was loaded so far, try legacy app-dir config.
    legacy = _try_load_legacy_file()
    if legacy:
        # Normalize expected fields.
        migrated = {
            "domain": str(legacy.get("domain", "")) if legacy.get("domain") else "",
            "host": str(legacy.get("host", "")) if legacy.get("host") else "",
            "password": str(legacy.get("password", "")) if legacy.get("password") else "",
            "audio_mode": str(legacy.get("audio_mode", "")) if legacy.get("audio_mode") else "",
            "public_enabled": bool(legacy.get("public_enabled", False)),
        }
        # Write into the primary storage so next run is fully self-contained.
        try:
            save_config(migrated)
        except Exception:
            pass
        # Return merged config (only non-empty strings / boolean)
        return {k: v for k, v in migrated.items() if (isinstance(v, bool) or (isinstance(v, str) and v))}

    config_path = _get_user_config_path()
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}

def save_config(config: dict) -> None:
    if IS_WINDOWS:
        try:
            import winreg

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH) as key:
                for name in ("domain", "host", "password", "audio_mode", "public_enabled"):
                    value = config.get(name)
                    if name == "public_enabled":
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, "1" if bool(value) else "0")
                    elif isinstance(value, str):
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            return
        except Exception:
            # Fall back to file-based config below
            pass

    config_path = _get_user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")

def get_public_ip():
    try:
        response = requests.get('https://api.ipify.org?format=json')
        return response.json()['ip']
    except:
        return None

def update_ddns(ip):
    if not DDNS_DOMAIN or not DDNS_PASSWORD:
        return
    url = f"https://dynamicdns.park-your-domain.com/update?host={DDNS_HOST}&domain={DDNS_DOMAIN}&password={DDNS_PASSWORD}&ip={ip}"
    try:
        response = requests.get(url)
        print(f"DDNS response: {response.text.strip()}")
        if response.status_code == 200 and "<ErrCount>0</ErrCount>" in response.text:
            print(f"DDNS updated to {ip}")
            return
        print(f"DDNS update failed: {response.text}")
    except Exception as e:
        print(f"DDNS update error: {e}")

def create_icon():
    # Create a simple icon
    image = Image.new('RGB', (64, 64), color='blue')
    draw = ImageDraw.Draw(image)
    draw.ellipse((16, 16, 48, 48), fill='white')
    return image

def on_settings(icon, item):
    show_settings()

def on_exit(icon, item):
    icon.stop()
    os._exit(0)  # Force exit

def setup_tray():
    icon = pystray.Icon("Sonos Streamer", create_icon(), menu=Menu(
        item('Settings', on_settings),
        item('Exit', on_exit)
    ))
    icon.run()

# DDNS Configuration (defaults, will be overridden by config)
config = load_config()
DDNS_DOMAIN = config.get('domain', 'yourdomain.com')
DDNS_HOST = config.get('host', '@')
DDNS_PASSWORD = config.get('password', 'your_ddns_password')
DDNS_UPDATE_INTERVAL = 300  # seconds (5 minutes)
PUBLIC_ENABLED = bool(config.get('public_enabled', False))

# Audio capture configuration
# - "vbcable": uses VB-Audio Cable (Windows) / BlackHole (macOS)
# - "loopback": uses Windows WASAPI loopback (no VB-Cable required)
AUDIO_MODE = config.get('audio_mode', 'loopback' if IS_WINDOWS else 'vbcable')

def show_settings():
    root = tk.Tk()
    root.title("Sonos Streamer Settings")
    root.geometry("350x250")
    
    # Get IPs
    local_ip = socket.gethostbyname(socket.gethostname())
    public_ip = get_public_ip() or "Unable to fetch"
    
    tk.Label(root, text=f"Local IP: {local_ip}").pack()
    tk.Label(root, text=f"Public IP: {public_ip}").pack()
    tk.Label(root, text="").pack()  # Spacer
    
    tk.Label(root, text="Domain:").pack()
    domain_entry = tk.Entry(root)
    domain_entry.insert(0, DDNS_DOMAIN)
    domain_entry.pack()
    
    tk.Label(root, text="Host:").pack()
    host_entry = tk.Entry(root)
    host_entry.insert(0, DDNS_HOST)
    host_entry.pack()
    
    tk.Label(root, text="DDNS Password:").pack()
    password_entry = tk.Entry(root, show="*")
    password_entry.insert(0, DDNS_PASSWORD)
    password_entry.pack()

    public_enabled_var = tk.BooleanVar(value=PUBLIC_ENABLED)
    tk.Checkbutton(root, text="Enable public mode (DDNS + public URL)", variable=public_enabled_var).pack(anchor="w")

    tk.Label(root, text="Audio capture:").pack()
    audio_mode_var = tk.StringVar(value=AUDIO_MODE)
    modes = [("Windows Loopback (no VB-Cable)", "loopback"), ("Virtual Cable / BlackHole", "vbcable")]
    for label, value in modes:
        # On macOS, loopback generally requires a virtual device anyway, but we keep the option visible.
        tk.Radiobutton(root, text=label, variable=audio_mode_var, value=value).pack(anchor="w")
    
    def save():
        global DDNS_DOMAIN, DDNS_HOST, DDNS_PASSWORD, AUDIO_MODE, PUBLIC_ENABLED
        DDNS_DOMAIN = domain_entry.get()
        DDNS_HOST = host_entry.get()
        DDNS_PASSWORD = password_entry.get()
        AUDIO_MODE = audio_mode_var.get()
        PUBLIC_ENABLED = bool(public_enabled_var.get())
        config = {
            'domain': DDNS_DOMAIN,
            'host': DDNS_HOST,
            'password': DDNS_PASSWORD,
            'audio_mode': AUDIO_MODE,
            'public_enabled': PUBLIC_ENABLED,
        }
        save_config(config)
        messagebox.showinfo("Settings", "Settings saved!")
        root.destroy()
    
    tk.Button(root, text="Save", command=save).pack()
    root.mainloop()

def ddns_thread():
    last_ip = None
    while True:
        ip = get_public_ip()
        if ip and ip != last_ip:
            update_ddns(ip)
            last_ip = ip
        time.sleep(DDNS_UPDATE_INTERVAL)

def _find_device_containing(name_substring: str, min_input_channels: int = 2) -> Optional[int]:
    for idx, dev in enumerate(sd.query_devices()):
        if name_substring in dev.get("name", "") and dev.get("max_input_channels", 0) >= min_input_channels:
            return idx
    return None

def _find_wasapi_output_device() -> Optional[int]:
    """Return a device index that belongs to the Windows WASAPI host API."""
    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return None

    wasapi_hostapi_idx = None
    for i, hostapi in enumerate(hostapis):
        name = str(hostapi.get("name", ""))
        if "wasapi" in name.lower():
            wasapi_hostapi_idx = i
            break

    if wasapi_hostapi_idx is None:
        return None

    try:
        default_out = hostapis[wasapi_hostapi_idx].get("default_output_device", None)
        if isinstance(default_out, int) and default_out >= 0:
            return default_out
    except Exception:
        pass

    for idx, dev in enumerate(devices):
        if dev.get("hostapi") == wasapi_hostapi_idx and dev.get("max_output_channels", 0) >= 2:
            return idx
    return None

def _find_wasapi_loopback_input_device(min_input_channels: int = 2) -> Optional[int]:
    """Some PortAudio/WASAPI setups expose loopback as a dedicated *input* device."""
    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return None

    wasapi_hostapi_idx = None
    for i, hostapi in enumerate(hostapis):
        name = str(hostapi.get("name", ""))
        if "wasapi" in name.lower():
            wasapi_hostapi_idx = i
            break
    if wasapi_hostapi_idx is None:
        return None

    for idx, dev in enumerate(devices):
        if dev.get("hostapi") != wasapi_hostapi_idx:
            continue
        if dev.get("max_input_channels", 0) < min_input_channels:
            continue
        if "loopback" in str(dev.get("name", "")).lower():
            return idx
    return None

def resolve_audio_source():
    """Return (device_index, extra_settings, mode_label, setup_hint, stream_channels)."""

    # Prefer explicit user choice.
    requested = (AUDIO_MODE or "").lower().strip()

    # 1) Windows WASAPI loopback (no extra driver)
    if IS_WINDOWS and requested == "loopback":
        if sc is None:
            raise RuntimeError(
                "Windows loopback capture requires the 'soundcard' package. Rebuild the EXE with soundcard included or switch Audio capture to 'Virtual Cable / BlackHole'."
            )
        return (
            None,
            None,
            "Windows loopback",
            "No VB-Audio Cable needed. Leave your normal speakers/headphones as default output.",
            CHANNELS,
        )

    # 2) Virtual cable mode
    if IS_WINDOWS:
        idx = _find_device_containing("CABLE Output")
        if idx is not None:
            dev = sd.query_devices(idx)
            ch = int(min(CHANNELS, dev.get("max_input_channels", CHANNELS)))
            ch = max(1, ch)
            return (
                idx,
                None,
                "VB-Audio Cable",
                "Set Windows audio output to 'VB-Audio Cable (CABLE Input)'.",
                ch,
            )
        if requested == "vbcable":
            raise RuntimeError("VB-Audio Cable not found. Install VB-Audio Cable OR switch Audio capture to 'Windows Loopback (no VB-Cable)'.")

    if IS_MAC:
        idx = _find_device_containing("BlackHole")
        if idx is not None:
            dev = sd.query_devices(idx)
            ch = int(min(CHANNELS, dev.get("max_input_channels", CHANNELS)))
            ch = max(1, ch)
            return (
                idx,
                None,
                "BlackHole",
                "Set macOS system audio output to the BlackHole device.",
                ch,
            )
        raise RuntimeError("BlackHole not found. For system audio capture on macOS, install BlackHole (or similar) and select it.")

    # Linux/other: try a generic default input
    return (
        None,
        None,
        "default input",
        "Using default input device.",
        CHANNELS,
    )

device_index, device_extra_settings, device_mode_label, device_setup_hint, stream_channels = resolve_audio_source()

# HTTP streaming handler
class StreamHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stream":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # Start ffmpeg encoder
        ffmpeg = subprocess.Popen(
            [
                FFMPEG_PATH,
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "-i", "pipe:0",
                "-acodec", "libmp3lame",
                "-b:a", BITRATE,
                "-f", "mp3",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
        )

        stop_event = threading.Event()

        def loopback_pump_soundcard():
            # Uses Windows loopback capture from the default speaker device.
            com_initialized = False
            try:
                if IS_WINDOWS:
                    # soundcard uses Windows Core Audio (COM). Threads must initialize COM.
                    # COINIT_APARTMENTTHREADED = 2
                    hr = ctypes.windll.ole32.CoInitializeEx(None, 2)
                    # S_OK (0) or S_FALSE (1) are success; RPC_E_CHANGED_MODE can be ignored.
                    if hr in (0, 1):
                        com_initialized = True
                if sc is None:
                    raise RuntimeError("soundcard package is not available")
                speaker = sc.default_speaker()
                mic = sc.get_microphone(speaker.name, include_loopback=True)
                # soundcard records float32 in [-1, 1]
                with mic.recorder(samplerate=SAMPLE_RATE) as rec:
                    while not stop_event.is_set():
                        data = rec.record(numframes=1024)
                        if data is None:
                            continue

                        # Ensure 2D array
                        if getattr(data, "ndim", 1) == 1:
                            data = np.expand_dims(data, axis=1)

                        # Ensure requested channel count
                        if data.shape[1] == 1 and CHANNELS == 2:
                            data = np.repeat(data, 2, axis=1)
                        elif data.shape[1] >= 2 and CHANNELS == 1:
                            data = data[:, :1]

                        pcm = np.clip(data, -1.0, 1.0)
                        pcm = (pcm * 32767.0).astype(np.int16)
                        try:
                            ffmpeg.stdin.write(pcm.tobytes())
                        except (BrokenPipeError, OSError, ValueError):
                            # Client/encoder went away.
                            stop_event.set()
                            break
            except Exception as e:
                # If capture fails, stop the stream.
                print("Loopback capture error:", e)
            finally:
                if IS_WINDOWS and com_initialized:
                    try:
                        ctypes.windll.ole32.CoUninitialize()
                    except Exception:
                        pass
                # Do not close ffmpeg stdin here; main thread owns ffmpeg lifecycle.

        def audio_callback(indata, frames, time, status):
            if status:
                print(status, file=sys.stderr)
            try:
                data = indata
                # Ensure 2D
                if getattr(data, "ndim", 1) == 1:
                    data = np.expand_dims(data, axis=1)
                # Upmix/downmix to match encoder CHANNELS
                if data.shape[1] == 1 and CHANNELS == 2:
                    data = np.repeat(data, 2, axis=1)
                elif data.shape[1] >= 2 and CHANNELS == 1:
                    data = data[:, :1]
                ffmpeg.stdin.write(data.tobytes())
            except BrokenPipeError:
                raise sd.CallbackStop()

        try:
            if IS_WINDOWS and (AUDIO_MODE or "").lower().strip() == "loopback":
                t = threading.Thread(target=loopback_pump_soundcard, daemon=True)
                t.start()
                while True:
                    chunk = ffmpeg.stdout.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        break
            else:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    device=device_index,
                    channels=stream_channels,
                    dtype="int16",
                    callback=audio_callback,
                    extra_settings=device_extra_settings,
                ):
                    while True:
                        chunk = ffmpeg.stdout.read(4096)
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                            break
        except Exception as e:
            # Treat common client disconnects as normal.
            msg = str(e)
            if "10053" in msg or "10054" in msg:
                pass
            else:
                print("Client disconnected:", e)
        finally:
            stop_event.set()
            try:
                try:
                    ffmpeg.stdin.close()
                except Exception:
                    pass
                ffmpeg.kill()
            except Exception:
                pass

    def log_message(self, *args):
        return


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    # Show settings if not configured
    if DDNS_DOMAIN == 'yourdomain.com' or not DDNS_PASSWORD:
        show_settings()
        # Reload config
        config = load_config()
        DDNS_DOMAIN = config.get('domain', 'yourdomain.com')
        DDNS_HOST = config.get('host', '@')
        DDNS_PASSWORD = config.get('password', 'your_ddns_password')
        AUDIO_MODE = config.get('audio_mode', AUDIO_MODE)
        PUBLIC_ENABLED = bool(config.get('public_enabled', PUBLIC_ENABLED))
    
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"Streaming at http://{local_ip}:{PORT}/stream")
    if PUBLIC_ENABLED and DDNS_DOMAIN != 'yourdomain.com':
        public_url = f"http://{DDNS_HOST}.{DDNS_DOMAIN}:{PORT}/stream" if DDNS_HOST != '@' else f"http://{DDNS_DOMAIN}:{PORT}/stream"
        print(f"Public URL: {public_url}")
    print(f"Audio capture: {device_mode_label}")
    print(device_setup_hint)
    
    # Start DDNS update thread (public mode only)
    if PUBLIC_ENABLED:
        threading.Thread(target=ddns_thread, daemon=True).start()
    
    # Start tray icon
    threading.Thread(target=setup_tray, daemon=True).start()
    
    server = ThreadedHTTPServer(("", PORT), StreamHandler)
    server.serve_forever()
