"""INI configuration (meteorrx.ini next to the executable)."""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field

DEFAULT_INI = r"""; =====================================================================
;  METEOR-X - Meteor-M LRPT receiver - configuration file
;  Fichier de configuration / configuration file (UTF-8)
;  Lines starting with ';' are comments.  Les lignes ';' sont des commentaires.
; =====================================================================

[station]
; Station position (decimal degrees, WGS84). Malta JM75FU by default.
latitude      = 35.8800
longitude     = 14.4500
altitude_m    = 50
; Only passes with a maximum elevation above this value are scheduled (deg)
min_elevation = 15
; Your callsign (shown in the window title)
callsign      =

[sdr]
; The receiver values below are those of the active receiver profile (data\receivers.json):
; each receiver (identified by its serial number) keeps its own settings and calibrations.
; device = rtlsdr | sdrplay | hackrf | lime | pluto | file
device          = rtlsdr
; SDR centre frequency (MHz). 137.500 keeps the 137.900 channel clear of the tuner DC spike.
center_mhz      = 137.500
; Frequency correction of the receiver reference (ppm, float)
ppm             = 0.0

; ---- RTL-SDR --------------------------------------------------------
rtl_index       = 0
; Serial number of the dongle to use (set by the receiver profile; empty = rtl_index)
rtl_serial      =
rtl_sample_rate = 1200000
; Tuner gain in dB, or "auto"
rtl_gain_db     = 38.6
rtl_bias_tee    = no
; Folder holding rtlsdr.dll + libusb-1.0.dll. Empty = automatic: MeteorRX\drivers, then the
; SatDump / SDR++ / SDR# installation folders.
rtl_dll_dir     =

; ---- SDRplay RSP, all models: RSP1, RSP1A, RSP1B, RSP2, RSPduo, RSPdx, RSPdx-R2 ----------------
; (SDRplay API 3.x must be installed)
; Leave empty to use the standard install path
sdrplay_api_dll   =
sdrplay_serial    =
sdrplay_sample_rate = 2000000
; IF gain reduction 20..59 dB; LNA state at 137 MHz: RSP1 0..3, RSP1A/1B/duo 0..9, RSP2 0..8, RSPdx 0..26
sdrplay_grdb      = 40
sdrplay_lna_state = 3
sdrplay_agc       = no
sdrplay_bias_tee  = no
; Broadcast FM notch (recommended near FM transmitters)
sdrplay_fm_notch  = yes
; Antenna port: RSP2 A/B, RSPdx A/B/C (other models: single port)
sdrplay_antenna   = A

; ---- HackRF One (uses hackrf.dll of SatDump / SDR++ / PothosSDR; WinUSB driver via Zadig) ----
hackrf_serial      =
; 2 MS/s minimum; 2.4 MS/s keeps both Meteor channels away from the DC spike
hackrf_sample_rate = 2400000
; LNA (IF) gain 0..40 dB in 8 dB steps, VGA (baseband) gain 0..62 dB in 2 dB steps, RF amplifier +14 dB
hackrf_lna_gain    = 24
hackrf_vga_gain    = 30
hackrf_amp         = no
hackrf_bias_tee    = no
; Folder of hackrf.dll; empty = automatic (SatDump, SDR++, PothosSDR installation folders)
hackrf_dll_dir     =

; ---- LimeSDR Mini 1.x / 2.0 (uses LimeSuite.dll of SatDump; FTDI D3XX driver) --------------
lime_serial        =
; 2.4 MS/s keeps both Meteor channels away from the DC residue
lime_sample_rate   = 2400000
; RX gain 0..73 dB
lime_gain_db       = 40
; RX path: auto (LNAW below 2 GHz) | LNAW | LNAH | LNAL
lime_antenna       = auto
; Folder of LimeSuite.dll; empty = automatic (SatDump installation folder first)
lime_dll_dir       =

; ---- PlutoSDR (ADALM-Pluto) over the network, no driver/library needed ----------------------
; ip:192.168.2.1 = Pluto on USB (USB network adapter); ip:<address> on Ethernet / Wi-Fi
pluto_uri          = ip:192.168.2.1
; >= 2083334 (AD9363 without FIR). 2160000 = same as the MARL test-pattern generator (shared RX/TX clock)
pluto_sample_rate  = 2160000
; RX gain 0..71 dB, or auto (AGC slow attack)
pluto_gain_db      = 50

; ---- File playback (testing / re-processing) ------------------------
; WAV IQ (SDR#, SatDump baseband .wav) or raw .cu8 / .cs8 / .cs16 / .cf32
file_path       =
; For raw files only (Hz); WAV files carry their own rate
file_sample_rate = 1000000
; Centre frequency of the recording (MHz), 0 = same as center_mhz
file_center_mhz = 0
; yes = paced in real time, no = as fast as the CPU allows
file_realtime   = no

[satellites]
; name = NORAD id ; frequencies MHz separated by "/" ; mode auto|72k|80k
METEOR-M2 3 = 57166 ; 137.900 ; auto
METEOR-M2 4 = 59051 ; 137.900 ; auto

[tle]
; TLEs are cached in data\tle.txt and refreshed when older than max_age_hours.
; Sources tried in order (CelesTrak, then SatNOGS). On failure the cache is kept and
; the download is retried every 15 min. Offline: menu TLE > Import TLE file
; (e.g. https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle saved as .txt).
max_age_hours = 12
; Optional comma-separated list of URL templates replacing the defaults ({norad} = NORAD id)
urls          =
; Optional HTTP(S) proxy, e.g. http://192.168.1.1:3128 (the Windows proxy setting is used otherwise)
proxy         =

[decoder]
; Default mode when no satellite is selected (manual): auto | 72k | 80k
mode            = auto
; Recording starts when the spectrum SNR exceeds this value (dB), when the carrier is detected
; (4th-power AFC, works far below this SNR) or on decoder lock
trigger_snr_db  = 3.0
; Doppler correction from TLE during passes
doppler         = yes
; Advanced DSP - empty = built-in values (measured with tools\bench_sensitivity.py).
; The pll_bw / fll_gain keys of older versions are ignored.
carrier_loop_bw =
clock_loop_gain =
rrc_alpha       = 0.5

[recording]
; Output folder; {documents} = the user's Documents folder
output_dir          = {documents}\MeteorRX
; Arm the receiver this many seconds before AOS
pre_aos_s           = 60
; Stop when no decodable signal for this long (after it was present), or at LOS
signal_loss_stop_s  = 90
; Give up on a recording that decoded nothing this many seconds after the signal was detected (a spur or
; an interferer can trigger one). 0 = never give up. Keeps a false trigger from swallowing the next pass.
false_trigger_s     = 300
; Panoramic ("earth curvature") correction: writes a ..._corr.png beside each image, 1 km/pixel on the
; ground, 1568 -> 2800 px wide. The original images are kept untouched.
geometric_correction = yes
; Smallest city labelled by the "Major cities" overlay in the Enhancements window. Lower it for a denser
; map (25000 is about the smallest that stays readable), raise it for only the big ones.
city_min_population = 200000
; Housekeeping of Documents\MeteorRX at start-up.
; purge_empty: yes = remove folders a pass left completely empty, no = keep them
purge_empty         = yes
; purge_days: delete whole receptions older than this many days. 0 = never delete (default), 30 = a sensible
; value for an unattended station. This removes decoded images, so it only runs if you set it yourself.
purge_days          = 0
save_cadu           = yes
; Save pass IQ beside MeteorRX.exe in IQ Recordings (~300 kS/s, 1.2 MB/s).
save_baseband       = yes
fill_missing        = yes
; Rotate images of northbound passes by 180 deg (north up)
rotate_northbound   = yes
; Composite outputs: 221 = natural colour (ch2,ch2,ch1), 123 = (ch1,ch2,ch3)
composites          = 221, 123
; Also save inverted versions of the IR channels (4, 5, 6)
ir_inverted         = yes

[gui]
; Interface language: fr | en | it | es | de  (table in meteorrx/i18n.py; add a language there)
language        = fr
; Start the automatic scheduler at launch
auto_start      = yes
; Window at launch: maximized | fullscreen | last (size/position of the previous session). F11 = full screen
window          = maximized
; Volume of the audio monitor (0-100); the monitor itself always starts muted
audio_volume    = 50
"""


def _exe_dir() -> str:
    """Installation root. Portable layout: <root>\\app\\meteorrx (sources) + <root>\\runtime (Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    parent = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = os.path.dirname(parent)
    if os.path.basename(parent).lower() == "app" and os.path.isdir(os.path.join(root, "runtime")):
        return root
    return parent


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".meteorrx_write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def app_dir() -> str:
    """Folder holding meteorrx.ini and data/: next to the exe (portable install), or
    %LOCALAPPDATA%\\MeteorRX when the program folder is read-only (e.g. Program Files)."""
    d = _exe_dir()
    if _writable(d):
        return d
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    alt = os.path.join(base, "MeteorRX")
    os.makedirs(alt, exist_ok=True)
    return alt


def documents_dir() -> str:
    home = os.path.expanduser("~")
    d = os.path.join(home, "Documents")
    return d if os.path.isdir(d) else home


@dataclass
class SatConf:
    name: str
    norad: int
    freqs_mhz: list
    mode: str


@dataclass
class Config:
    path: str
    raw: configparser.ConfigParser
    satellites: list = field(default_factory=list)
    _dirty: set = field(default_factory=set)

    # convenience accessors ---------------------------------------------------
    def get(self, sec, key, fallback=None):
        return self.raw.get(sec, key, fallback=fallback)

    def getf(self, sec, key, fallback=0.0) -> float:
        v = self.raw.get(sec, key, fallback=None)
        try:
            return float(v) if v not in (None, "") else fallback
        except ValueError:
            return fallback

    def geti(self, sec, key, fallback=0) -> int:
        return int(self.getf(sec, key, fallback))

    def getb(self, sec, key, fallback=False) -> bool:
        try:
            return self.raw.getboolean(sec, key, fallback=fallback)
        except ValueError:
            return fallback

    @property
    def output_dir(self) -> str:
        d = self.get("recording", "output_dir", "{documents}\\MeteorRX")
        d = d.replace("{documents}", documents_dir()).replace("\\", os.sep)
        return os.path.expandvars(d)

    @property
    def iq_recordings_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "IQ Recordings")

    @property
    def data_dir(self) -> str:
        return os.path.join(app_dir(), "data")

    def set(self, sec, key, value):
        if not self.raw.has_section(sec):
            self.raw.add_section(sec)
        self.raw.set(sec, key, str(value))
        self._dirty.add((sec, key))

    def save(self):
        """Rewrite only changed values, keeping comments: update lines in place.
        Keys set by the program that are not yet in the file (older INI) are appended to their section."""
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        sec = None
        done = set()
        out = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                sec = s[1:-1]
            elif sec and "=" in s and not s.startswith(";") and sec != "satellites":
                k = s.split("=", 1)[0].strip()
                if (sec, k) in self._dirty and self.raw.has_option(sec, k):
                    ln = f"{ln.split('=', 1)[0]}= {self.raw.get(sec, k)}"
                    done.add((sec, k))
            out.append(ln)
        missing = sorted(k for k in self._dirty if k not in done and k[0] != "satellites")
        for sec, key in missing:
            line = f"{key:<15} = {self.raw.get(sec, key)}"
            idx = None
            cur = None
            for i, ln in enumerate(out):              # insert after the last line of the section
                st = ln.strip()
                if st.startswith("[") and st.endswith("]"):
                    cur = st[1:-1]
                elif cur == sec and st:
                    idx = i
                if cur == sec and idx is None:
                    idx = i
            if idx is None:
                out += ["", f"[{sec}]", line]
            else:
                out.insert(idx + 1, line)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, self.path)
        self._dirty.clear()


def _parse_sats(cp: configparser.ConfigParser) -> list[SatConf]:
    out = []
    if not cp.has_section("satellites"):
        return out
    for name, val in cp.items("satellites"):
        parts = [p.strip() for p in val.split(";")]
        try:
            norad = int(parts[0])
            freqs = [float(x) for x in parts[1].split("/")] if len(parts) > 1 and parts[1] else [137.9]
            mode = parts[2].lower() if len(parts) > 2 and parts[2] else "auto"
            out.append(SatConf(name.upper(), norad, freqs, mode if mode in ("auto", "72k", "80k") else "auto"))
        except (ValueError, IndexError):
            continue
    return out


def load(path: str | None = None) -> Config:
    path = path or os.path.join(app_dir(), "meteorrx.ini")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_INI)
    cp = configparser.ConfigParser(inline_comment_prefixes=None, interpolation=None)
    cp.optionxform = str  # keep satellite names as written
    defaults = configparser.ConfigParser(interpolation=None)
    defaults.optionxform = str
    defaults.read_string(DEFAULT_INI)
    cp.read_string(DEFAULT_INI)
    if cp.has_section("satellites"):
        cp.remove_section("satellites")
        cp.add_section("satellites")
    cp.read(path, encoding="utf-8")
    if not cp.items("satellites"):
        for k, v in defaults.items("satellites"):
            cp.set("satellites", k, v)
    cfg = Config(path=path, raw=cp)
    cfg.satellites = _parse_sats(cp)
    return cfg
