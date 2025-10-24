import sys, os, json, math, time, asyncio, contextlib, logging, webbrowser, ctypes
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from collections import defaultdict
from logging.handlers import RotatingFileHandler

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
import aiohttp
from qasync import QEventLoop

with contextlib.suppress(Exception):
    import keyboard
with contextlib.suppress(Exception):
    from pynput import mouse

APP_NAME = "CrypTick"
APP_DESC = "Lightweight always-on-top crypto price ticker with profiles and multi-network support."

# --- Installer-safe paths: read-only assets beside EXE, writes to %APPDATA%\CrypTick ---
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # PyInstaller-safe base
CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home() / "AppData/Roaming") / "CrypTick"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = CONFIG_DIR / "app_state.json"
LOG_FILE   = str((CONFIG_DIR / "ticker_debug.log").resolve())

# Read-only resources distributed with the app
NETWORKS_FILE = BASE_DIR / "networks.json"
ASSETS_DIR    = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
ICON_FILE     = ASSETS_DIR / "cryptick.png"

# Logos cache lives in %APPDATA%
CACHE_DIR = CONFIG_DIR / "cache" / "logos"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# -------- logging: truncate at ~1MB, keep single file --------
class OverwriteRotatingFileHandler(RotatingFileHandler):
    """Rotate by deleting and recreating when exceeding maxBytes. No backups kept."""
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.baseFilename)
        self.mode = "w"
        self.stream = self._open()

file_handler = OverwriteRotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=0, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

log = logging.getLogger("ticker")
log.setLevel(logging.INFO)
log.handlers.clear()
log.addHandler(file_handler)
log.addHandler(stream_handler)
# --------------------------------------------------------------

# ---------- API ----------
GT_MULTI = "https://api.geckoterminal.com/api/v2/networks/{net}/tokens/multi/{csv}?include=top_pools&include_composition=false"
GT_INFO  = "https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/info"

def info_url(net_id: str, addr: str) -> str:
    return GT_INFO.format(net=net_id, addr=addr)

def price_str(p: Optional[float]) -> str:
    if p is None: return "—"
    if p >= 100:  return f"${p:,.2f}"
    if p >= 1:    return f"${p:,.3f}"
    if p >= 0.1:  return f"${p:.4f}"
    return "$" + f"{p:.8f}".rstrip("0").rstrip(".")

def pct_str(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.2f}%"

def short_addr(addr: str) -> str:
    return addr if len(addr) <= 10 else f"{addr[:6]}…{addr[-4:]}"

def format_changes(m5: Optional[float], h24: Optional[float]) -> str:
    m5s  = "—" if m5  is None else f"{m5:+.0f}%"
    h24s = "—" if h24 is None else f"{h24:+.0f}%"
    return f"{m5s} last 5m / {h24s} last 24h"

def make_item_html(name: str, price: Optional[float], m5: Optional[float], h24: Optional[float],
                   separator_text: str, bold_name: bool, bold_price: bool, bold_changes: bool) -> str:
    ns = f"<b>{name}</b>" if bold_name else name
    ps = f"<b>{price_str(price)}</b>" if bold_price else price_str(price)
    cs = f"<b>{format_changes(m5, h24)}</b>" if bold_changes else format_changes(m5, h24)
    base = f"{ns} • {ps} • {cs}"
    sep = f" {separator_text}" if separator_text else ""
    return base + sep

# ---------- Address normalization & keys ----------
def normalize_address(net_id: str, addr: str) -> str:
    if not addr:
        return addr
    return addr.lower() if addr.startswith("0x") else addr

def key_for(net_id: str, addr: str) -> str:
    return f"{net_id}:{normalize_address(net_id, addr)}"

# ---------- Defaults & State ----------
DEFAULT_STATE = {
    "profiles": {
        "High Risk Assets": [],
        "Medium Risk Assets": [],
        "Low Risk Assets": []
    },
    "active_profile": "High Risk Assets",
    "token_names": {},
    "token_logos": {},
    "profile_settings": {},
    "settings": {
        "refresh_sec": 30,
        "opacity": 0.5,
        "font_family": "Open Sans",
        "font_px": 15,
        "font_color": "#FFFFFF",
        "hotkey": "F8"
    }
}


def _ensure_profile_settings(s: Dict[str,Any], name: str) -> Dict[str,Any]:
    g = s["settings"]
    ps = s["profile_settings"].setdefault(name, {})
    ps.setdefault("monitor_index", None)               # None => not shown and not refreshed
    ps.setdefault("opacity", g.get("opacity", 0.5))
    ps.setdefault("font_family", g.get("font_family", "Open Sans"))
    ps.setdefault("font_px", g.get("font_px", 15))
    ps.setdefault("font_color", g.get("font_color", "#ffffff"))
    ps.setdefault("click_through", True)               # default: ON
    ps.setdefault("show_logo", True)                   # default: ON
    ps.setdefault("refresh_sec", g.get("refresh_sec", 30))
    ps.setdefault("use_custom_names", False)
    ps.setdefault("separator_text", "|")               # new free-text separator
    # bold defaults
    ps.setdefault("bold_name", True)                   # default: ON
    ps.setdefault("bold_price", False)
    ps.setdefault("bold_changes", False)
    return ps

def load_networks() -> List[Dict[str, Any]]:
    with open(NETWORKS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        save_state(DEFAULT_STATE)
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("token_names", {})
    s.setdefault("token_logos", {})
    s.setdefault("profiles", DEFAULT_STATE["profiles"])
    s.setdefault("active_profile", DEFAULT_STATE["active_profile"])
    s.setdefault("profile_settings", {})
    s.setdefault("settings", {})
    for k, v in DEFAULT_STATE["settings"].items():
        s["settings"].setdefault(k, v)
    for pname in s["profiles"].keys():
        _ensure_profile_settings(s, pname)
        for t in s["profiles"][pname]:
            t.setdefault("custom_name", "")
            t["address"] = normalize_address(t.get("network_id",""), t.get("address",""))
    save_state(s)
    return s

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def fetch_token_name_sync(net: str, addr: str) -> Optional[str]:
    import urllib.request, json as _json
    url = info_url(net, addr)
    req = urllib.request.Request(url, headers={"Accept":"application/json","Cache-Control":"no-cache","Pragma":"no-cache"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = _json.loads(resp.read().decode("utf-8"))
        return payload.get("data",{}).get("attributes",{}).get("name")

def _logo_file_for(key: str) -> Path:
    safe = key.replace(":", "_")
    return CACHE_DIR / f"{safe}.png"

def _load_logo_from_disk(key: str) -> Optional[QtGui.QPixmap]:
    p = _logo_file_for(key)
    if p.exists():
        pm = QtGui.QPixmap()
        if pm.load(str(p)):
            return pm
    return None

# ---------- Windows click-through ----------
def _set_click_through(hwnd: int, enable: bool):
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_LAYERED = 0x00080000
    user32 = ctypes.windll.user32
    SetWindowLong = user32.SetWindowLongW
    GetWindowLong = user32.GetWindowLongW
    style = GetWindowLong(hwnd, GWL_EXSTYLE)
    if enable:
        style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style = (style | WS_EX_LAYERED) & (~WS_EX_TRANSPARENT)
    SetWindowLong(hwnd, GWL_EXSTYLE, style)

def _hwnd_for_widget(w: QtWidgets.QWidget) -> Optional[int]:
    try:
        return int(w.winId())
    except Exception:
        return None

# ---------- App icon ----------
def build_fallback_icon() -> QtGui.QIcon:
    size = 256
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing)
    brush = QtGui.QBrush(QtGui.QColor("#10b981"))
    p.setBrush(brush); p.setPen(QtCore.Qt.NoPen)
    p.drawEllipse(0,0,size,size)
    f = QtGui.QFont("Segoe UI", int(size*0.52), QtGui.QFont.Bold)
    p.setFont(f); p.setPen(QtGui.QColor("white"))
    rect = QtCore.QRect(0, int(size*0.12), size, int(size*0.8))
    p.drawText(rect, Qt.AlignCenter, "C")
    p.end()
    return QtGui.QIcon(pm)

def load_app_icon() -> QtGui.QIcon:
    if ICON_FILE.exists():
        ico = QtGui.QIcon(str(ICON_FILE))
        if not ico.isNull():
            return ico
    return build_fallback_icon()
   
APP_ICON = None  # set in main()

# ---------- Hotkey capture ----------
class HotkeyCaptureDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Press a key combo or middle mouse")
        self.setModal(True)
        self.setFixedSize(420, 160)
        lab = QtWidgets.QLabel("Press keys (e.g., F9, Ctrl+H). Click middle mouse for Mouse3.")
        lab.setAlignment(Qt.AlignCenter)
        self.preview = QtWidgets.QLabel("Waiting…")
        self.preview.setAlignment(Qt.AlignCenter)
        self.captured = None

        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(lab); v.addWidget(self.preview)
        btn = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        btn.rejected.connect(self.reject); v.addWidget(btn)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        mods = []
        if e.modifiers() & Qt.ControlModifier: mods.append("ctrl")
        if e.modifiers() & Qt.AltModifier: mods.append("alt")
        if e.modifiers() & Qt.ShiftModifier: mods.append("shift")
        key = QtGui.QKeySequence(e.key()).toString().lower()
        if key.startswith("f") and key[1:].isdigit():
            combo = "+".join(mods + [key])
        else:
            base = key
            combo = "+".join(mods + [base]) if base else None
        if combo:
            self.captured = combo
            self.preview.setText(f"Captured: {combo}")
            QtCore.QTimer.singleShot(300, self.accept)

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == Qt.MiddleButton:
            self.captured = "mouse3"
            self.preview.setText("Captured: mouse3")
            QtCore.QTimer.singleShot(300, self.accept)

# ---------- Add Token ----------
class AddTokenDialog(QtWidgets.QDialog):
    def __init__(self, networks: List[Dict[str,Any]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add token")
        self.setModal(True)
        self.setFixedWidth(560)

        self.address = QtWidgets.QLineEdit(); self.address.setPlaceholderText("Token address")
        self.label = QtWidgets.QLineEdit(); self.label.setPlaceholderText("Custom name (optional)")

        self.networks = networks
        self.net_combo = QtWidgets.QComboBox()
        for n in networks:
            self.net_combo.addItem(n["attributes"]["name"], n["id"])

        form = QtWidgets.QFormLayout()
        form.addRow("Network", self.net_combo)
        form.addRow("Address", self.address)
        form.addRow("Custom name", self.label)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)

        v = QtWidgets.QVBoxLayout(self)
        v.addLayout(form); v.addWidget(btns)

    def get_values(self):
        return {
            "network_id": self.net_combo.currentData(),
            "network_name": self.net_combo.currentText(),
            "address": self.address.text().strip(),
            "custom_name": self.label.text().strip()
        }

# ---------- About Dialog ----------
class AboutDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About CrypTick")
        self.setWindowIcon(APP_ICON)
        self.resize(720, 760)

        icon_label = QtWidgets.QLabel()
        pm = APP_ICON.pixmap(84, 84)
        icon_label.setPixmap(pm)
        icon_label.setAlignment(Qt.AlignCenter)

        title = QtWidgets.QLabel(f"<h2 style='margin:6px 0 0 0'>{APP_NAME}</h2>"
                                 f"<div style='color:#999'>{APP_DESC}</div>")
        title.setAlignment(Qt.AlignCenter)

        content = QtWidgets.QTextBrowser()
        content.setOpenExternalLinks(True)
        content.setHtml(self._html())

        footer = QtWidgets.QLabel("❤️  Built for the Crypto Community by GD aka MOARPH | © 2025")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color:#888; margin-top:6px;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(icon_label)
        layout.addWidget(title)
        layout.addWidget(content, 1)
        layout.addWidget(footer)

    def _html(self) -> str:
        return """
<style>
ul{margin-top:6px}
code{background:#222;color:#ddd;padding:1px 4px;border-radius:4px}
</style>
<p><b>CrypTick</b> is an always-on-top ticker for crypto prices. It pulls live prices and 5m/24h changes per token across multiple networks and shows them as a slim bar over any app, including games and browsers.</p>
<p><i>The app refreshes automatically every 30 seconds to fetch new token data.</i></p>
<h3>Key concepts</h3>
<ul>
  <li><b>Profiles</b>: group tokens (e.g., Low Risk, High Risk). Each profile has its own visual settings and monitor target.</li>
  <li><b>Monitors</b>: assign one or more profiles to the same monitor. The bar merges them horizontally in one row.</li>
  <li><b>Hotkey</b>: set a global hotkey or middle mouse to cycle the active profile in the dashboard.</li>
</ul>
<h3>How to use</h3>
<ol>
  <li><b>Create or select a profile</b> in the left panel of the Dashboard.</li>
  <li><b>Add tokens</b> using “Add token”. Pick network, paste token address, optionally set a custom name.</li>
  <li><b>Reorder tokens</b> with “Move Up/Down”. The bar uses this exact order.</li>
  <li><b>Set profile settings</b>:
    <ul>
      <li><b>Monitor</b>: where to display. “None” disables and stops fetching for that profile.</li>
      <li><b>Font size / color / opacity</b>: live styling for that profile segment.</li>
      <li><b>Allow click-through</b>: when on, the bar lets your clicks pass to apps beneath.</li>
      <li><b>Show token logos</b>: displays token logos if available. Cached on disk.</li>
      <li><b>Use custom names</b>: shows your custom names instead of token names.</li>
      <li><b>Separator</b>: choose any separator text. Leave empty for no separator.</li>
      <li><b>Bold options</b>: choose which of Name, Price, and Changes to emphasize.</li>
    </ul>
  </li>
  <li><b>Start / Pause / Stop</b>:
    <ul>
      <li><b>Start</b> makes bars appear on all monitors with at least one assigned profile.</li>
      <li><b>Pause</b> freezes updates but leaves bars visible.</li>
      <li><b>Stop</b> hides all bars and stops network calls.</li>
    </ul>
  </li>
  <li><b>Tray icon</b>: double-click to reopen the Dashboard. Right-click for quick commands.</li>
</ol>
<h3>Notes</h3>
<ul>
  <li>Only profiles assigned to a monitor are refreshed, reducing API usage.</li>
  <li>When multiple profiles share a monitor, they are shown side by side in a single top bar.</li>
  <li>The bar scrolls only when content exceeds width and continues smoothly across refreshes.</li>
</ul>
"""

# ---------- Dashboard ----------
class Dashboard(QtWidgets.QWidget):
    startTracking = QtCore.Signal()
    pauseTracking = QtCore.Signal()
    stopTracking  = QtCore.Signal()
    settingsChanged = QtCore.Signal()

    def __init__(self, state: Dict[str,Any], networks: List[Dict[str,Any]]):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — Dashboard")
        self.setWindowIcon(APP_ICON)
        self.resize(1200, 820)
        self.state = state
        self.networks = networks

        # profiles
        self.profile_list = QtWidgets.QListWidget()
        self.profile_list.addItems(self.state["profiles"].keys())
        self.profile_list.setCurrentRow(list(self.state["profiles"].keys()).index(self.state["active_profile"]))
        self.profile_list.currentItemChanged.connect(self.on_profile_changed)
        self.profile_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.profile_list.customContextMenuRequested.connect(self.on_profiles_context)


        add_prof = QtWidgets.QPushButton("New profile"); add_prof.clicked.connect(self.add_profile)
        del_prof = QtWidgets.QPushButton("Delete profile"); del_prof.clicked.connect(lambda: self.delete_profile(self.current_profile_name()))

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("Profiles"))
        left.addWidget(self.profile_list)
        hlp = QtWidgets.QHBoxLayout(); hlp.addWidget(add_prof); hlp.addWidget(del_prof)
        left.addLayout(hlp)
        about_btn = QtWidgets.QPushButton("About CrypTick")
        about_btn.clicked.connect(self.show_about)
        about_btn.setCursor(Qt.PointingHandCursor)
        about_btn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        about_btn.setFixedHeight(32)
        about_btn.setStyleSheet("""
            QPushButton {
                background-color:#10b981; color:white;
                font-weight:600; border-radius:16px; padding:4px 12px;
            }
                QPushButton:hover { background-color:#0ea774; }
        """)
        left.addSpacing(6)
        left.addWidget(about_btn, 0, Qt.AlignHCenter)
        left.addSpacing(6)



        # table (with Custom name column)
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Label/Name","Network","Address","Last price","24h %","5m %","Custom name"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_table_edit)
        self.refresh_note = QtWidgets.QLabel("Token data refreshes once every 30 seconds.")
        self.refresh_note.setStyleSheet("color:#888; margin:6px 4px 0 4px;")

        self.refresh_table()

        add_tok = QtWidgets.QPushButton("Add token"); add_tok.clicked.connect(self.add_token)
        rm_tok  = QtWidgets.QPushButton("Remove selected"); rm_tok.clicked.connect(self.remove_selected)
        mv_up   = QtWidgets.QPushButton("Move Up"); mv_up.clicked.connect(self.move_up)
        mv_down = QtWidgets.QPushButton("Move Down"); mv_down.clicked.connect(self.move_down)

        # per-profile settings panel
        self.font_px = QtWidgets.QSpinBox(); self.font_px.setRange(8, 48)
        self.font_color = QtWidgets.QLineEdit(); self.font_color.setReadOnly(True)
        pick_color = QtWidgets.QPushButton("Pick color…"); pick_color.clicked.connect(self.pick_color)
        color_row = QtWidgets.QWidget(); color_layout = QtWidgets.QHBoxLayout(color_row); color_layout.setContentsMargins(0,0,0,0)
        color_layout.addWidget(self.font_color); color_layout.addWidget(pick_color)

        self.opacity = QtWidgets.QDoubleSpinBox(); self.opacity.setRange(0.3, 1.0); self.opacity.setSingleStep(0.05)

        # Two-column toggles
        # Left toggles
        self.click_through = QtWidgets.QCheckBox("Allow click-through")
        self.show_logo = QtWidgets.QCheckBox("Show token logos")
        self.use_custom = QtWidgets.QCheckBox("Use custom names")
        left_toggles = QtWidgets.QVBoxLayout()
        left_toggles.addWidget(self.click_through)
        left_toggles.addWidget(self.show_logo)
        left_toggles.addWidget(self.use_custom)
        left_toggles.addStretch(1)

        # Right toggles and separator field
        self.separator_edit = QtWidgets.QLineEdit()
        self.separator_edit.setPlaceholderText('e.g., |')
        self.separator_edit.setFixedWidth(120)

        sep_row = QtWidgets.QWidget()
        _sep_h = QtWidgets.QHBoxLayout(sep_row)
        _sep_h.setContentsMargins(0,0,0,0)
        _sep_h.setSpacing(8)
        _sep_h.addWidget(QtWidgets.QLabel("Separator"))
        _sep_h.addWidget(self.separator_edit, 0, Qt.AlignLeft)
        _sep_h.addStretch(1)

        self.bold_name = QtWidgets.QCheckBox("Bold token name")
        self.bold_price = QtWidgets.QCheckBox("Bold token price")
        self.bold_changes = QtWidgets.QCheckBox("Bold price changes")

        right_toggles = QtWidgets.QVBoxLayout()
        right_toggles.addWidget(sep_row)
        right_toggles.addWidget(self.bold_name)
        right_toggles.addWidget(self.bold_price)
        right_toggles.addWidget(self.bold_changes)
        right_toggles.addStretch(1)


        toggles_row = QtWidgets.QHBoxLayout()
        toggles_row.addLayout(left_toggles, 1)
        toggles_row.addSpacing(12)
        toggles_row.addLayout(right_toggles, 1)

        # Monitors + hotkey
        self.monitor_combo = QtWidgets.QComboBox()
        self.monitor_combo.addItem("None", None)
        for idx, scr in enumerate(QtGui.QGuiApplication.screens()):
            g = scr.geometry()
            self.monitor_combo.addItem(f"{idx}: {g.width()}x{g.height()} @ ({g.x()},{g.y()})", idx)

        self.hotkey = QtWidgets.QLineEdit(self.state["settings"]["hotkey"]); self.hotkey.setReadOnly(True)
        pick_hotkey = QtWidgets.QPushButton("Set global cycle hotkey…"); pick_hotkey.clicked.connect(self.pick_hotkey)
        hk_row = QtWidgets.QWidget(); hk_layout = QtWidgets.QHBoxLayout(hk_row); hk_layout.setContentsMargins(0,0,0,0)
        hk_layout.addWidget(self.hotkey); hk_layout.addWidget(pick_hotkey)

        form = QtWidgets.QFormLayout()
        form.addRow("Font size (px)", self.font_px)
        form.addRow("Font color", color_row)
        form.addRow("Opacity", self.opacity)
        form.addRow("Toggles", toggles_row)
        form.addRow("Monitor", self.monitor_combo)
        form.addRow("Cycle hotkey", hk_row)

        save_settings = QtWidgets.QPushButton("Save profile settings"); 
        save_settings.clicked.connect(self.save_settings)
        
        start = QtWidgets.QPushButton("Start Tracking"); start.clicked.connect(self.startTracking.emit)
        pause = QtWidgets.QPushButton("Pause Tracking"); pause.clicked.connect(self.pauseTracking.emit)
        stop  = QtWidgets.QPushButton("Stop Tracking");  stop.clicked.connect(self.stopTracking.emit)

        self.status = QtWidgets.QLabel("Idle"); self.status.setStyleSheet("color:#aaa;")

        right = QtWidgets.QVBoxLayout()
        right.addWidget(self.table)
        btns = QtWidgets.QHBoxLayout(); 
        btns.addWidget(add_tok); btns.addWidget(rm_tok); btns.addStretch(1); btns.addWidget(mv_up); btns.addWidget(mv_down)
        right.addLayout(btns)
        right.addWidget(self.refresh_note)
        right.addWidget(self.status)
        right.addSpacing(8)
        right.addWidget(QtWidgets.QLabel("Profile settings"))
        right.addLayout(form)
        right.addWidget(save_settings)

        ctl = QtWidgets.QHBoxLayout()
        ctl.addWidget(start); ctl.addWidget(pause); ctl.addWidget(stop)
        right.addLayout(ctl)

        layout = QtWidgets.QHBoxLayout(self)
        col1 = QtWidgets.QWidget(); col1.setLayout(left)
        layout.addWidget(col1, 1); layout.addLayout(right, 3)

        self._load_profile_settings_into_ui()

    # dashboard helpers
    def set_status(self, msg: str):
        self.status.setText(msg)

    def current_profile_name(self) -> str:
        return self.profile_list.currentItem().text()

    def _load_profile_settings_into_ui(self):
        pname = self.current_profile_name()
        ps = _ensure_profile_settings(self.state, pname)
        self.font_px.setValue(ps["font_px"])
        self.font_color.setText(ps["font_color"])
        self.opacity.setValue(ps["opacity"])
        self.click_through.setChecked(ps["click_through"])
        self.show_logo.setChecked(ps["show_logo"])
        self.use_custom.setChecked(ps["use_custom_names"])
        self.separator_edit.setText(ps.get("separator_text", "|"))
        self.bold_name.setChecked(ps.get("bold_name", True))
        self.bold_price.setChecked(ps.get("bold_price", False))
        self.bold_changes.setChecked(ps.get("bold_changes", False))
        idx = 0
        for i in range(self.monitor_combo.count()):
            if self.monitor_combo.itemData(i) == ps["monitor_index"]:
                idx = i; break
        self.monitor_combo.setCurrentIndex(idx)

    def pick_color(self):
        col = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.font_color.text()), self, "Pick font color", QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if col.isValid():
            self.font_color.setText(col.name(QtGui.QColor.HexRgb))

    def pick_hotkey(self):
        dlg = HotkeyCaptureDialog(self)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.captured:
            self.hotkey.setText(dlg.captured)

    def show_about(self):
        AboutDialog(self).exec()

    def on_profile_changed(self):
        self.state["active_profile"] = self.current_profile_name()
        save_state(self.state)
        self.refresh_table()
        self._load_profile_settings_into_ui()

    def add_profile(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New profile", "Profile name")
        if not ok or not name.strip(): return
        name = name.strip()
        if name in self.state["profiles"]:
            QtWidgets.QMessageBox.warning(self, "Exists", "Profile already exists."); return
        self.state["profiles"][name] = []
        _ensure_profile_settings(self.state, name)
        save_state(self.state)
        self.profile_list.addItem(name)
        self.profile_list.setCurrentRow(self.profile_list.count()-1)

    def del_profile(self):
        name = self.current_profile_name()
        # allow deleting any profile, including defaults
        profs = self.state["profiles"]
        if name not in profs:
            return
        del profs[name]
        # if the deleted one was active, pick another or create a blank one
        if self.state["active_profile"] == name:
            if profs:
                self.state["active_profile"] = next(iter(profs.keys()))
            else:
                self.state["profiles"] = {"New Profile": []}
                self.state["active_profile"] = "New Profile"
        save_state(self.state)
        self.profile_list.clear()
        self.profile_list.addItems(self.state["profiles"].keys())
        # select the active one
        names = list(self.state["profiles"].keys())
        self.profile_list.setCurrentRow(names.index(self.state["active_profile"]))


    def add_token(self):
        dlg = AddTokenDialog(self.networks, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted: return
        vals = dlg.get_values()
        addr = normalize_address(vals["network_id"], vals["address"])
        if not addr: return
        entry = {"network_id": vals["network_id"], "address": addr, "custom_name": vals["custom_name"]}
        k = key_for(entry["network_id"], entry["address"])
        if k not in self.state["token_names"]:
            try:
                name = fetch_token_name_sync(entry["network_id"], entry["address"])
                if name: self.state["token_names"][k] = name
            except Exception as e:
                log.warning("Name lookup failed %s: %s", k, e)
        prof = self.current_profile_name()
        self.state["profiles"][prof].append(entry)
        save_state(self.state)
        self.refresh_table()

    def remove_selected(self):
        rows = sorted(set(i.row() for i in self.table.selectedIndexes()), reverse=True)
        prof = self.current_profile_name()
        for r in rows:
            if 0 <= r < len(self.state["profiles"][prof]):
                del self.state["profiles"][prof][r]
        save_state(self.state)
        self.refresh_table()

    def move_up(self):
        prof = self.current_profile_name()
        rows = sorted(set(i.row() for i in self.table.selectedIndexes()))
        if not rows: return
        for r in rows:
            if r <= 0: continue
            self.state["profiles"][prof][r-1], self.state["profiles"][prof][r] = \
                self.state["profiles"][prof][r], self.state["profiles"][prof][r-1]
        save_state(self.state)
        self.refresh_table()
        self.table.clearSelection()
        for r in [max(0, x-1) for x in rows]:
            self.table.selectRow(r)

    def move_down(self):
        prof = self.current_profile_name()
        rows = sorted(set(i.row() for i in self.table.selectedIndexes()))
        if not rows: return
        for r in reversed(rows):
            if r >= len(self.state["profiles"][prof]) - 1: continue
            self.state["profiles"][prof][r+1], self.state["profiles"][prof][r] = \
                self.state["profiles"][prof][r], self.state["profiles"][prof][r+1]
        save_state(self.state)
        self.refresh_table()
        self.table.clearSelection()
        for r in [min(len(self.state["profiles"][prof])-1, x+1) for x in rows]:
            self.table.selectRow(r)

    def _on_table_edit(self, item: QtWidgets.QTableWidgetItem):
        if item.column() != 6: return
        row = item.row()
        prof = self.current_profile_name()
        try:
            self.state["profiles"][prof][row]["custom_name"] = item.text().strip()
            save_state(self.state)
        except Exception as e:
            log.warning("Failed to save custom name: %s", e)

    def save_settings(self):
        pname = self.current_profile_name()
        ps = _ensure_profile_settings(self.state, pname)
        ps["font_px"] = int(self.font_px.value())
        ps["font_color"] = self.font_color.text().strip() or "#FFFFFF"
        ps["opacity"] = float(self.opacity.value())
        ps["click_through"] = bool(self.click_through.isChecked())
        ps["show_logo"] = bool(self.show_logo.isChecked())
        ps["use_custom_names"] = bool(self.use_custom.isChecked())
        ps["separator_text"] = self.separator_edit.text()
        ps["bold_name"] = bool(self.bold_name.isChecked())
        ps["bold_price"] = bool(self.bold_price.isChecked())
        ps["bold_changes"] = bool(self.bold_changes.isChecked())
        ps["monitor_index"] = self.monitor_combo.currentData()
        self.state["settings"]["hotkey"] = self.hotkey.text().strip() or "F5"
        save_state(self.state)
        self.settingsChanged.emit()
        QtWidgets.QMessageBox.information(self, "Saved", "Profile settings saved.")
        self.refresh_table()

    def refresh_table(self, prices: Optional[Dict[str,Dict[str,Optional[float]]]]=None):
        prof = self.current_profile_name()
        tokens = self.state["profiles"].get(prof, [])
        self.table.setRowCount(len(tokens))
        for i, t in enumerate(tokens):
            k = key_for(t["network_id"], t["address"])
            name = self.state["token_names"].get(k) or short_addr(t["address"])
            vals = prices.get(k) if prices else None
            price = price_str(vals["price"]) if vals else "—"
            h24 = pct_str(vals["h24"]) if vals else "—"
            m5  = pct_str(vals["m5"])  if vals else "—"
            cols = [name, t["network_id"], t["address"], price, h24, m5, t.get("custom_name","")]
            for col, txt in enumerate(cols):
                it = QtWidgets.QTableWidgetItem(txt)
                if col != 6:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(i, col, it)
    def on_profiles_context(self, pos: QtCore.QPoint):
        item = self.profile_list.itemAt(pos)
        if not item:
            return
        name = item.text()
        menu = QtWidgets.QMenu(self)
        act_rename = menu.addAction("Rename…")
        act_delete = menu.addAction("Delete")
        action = menu.exec(self.profile_list.mapToGlobal(pos))
        if action == act_rename:
            self._rename_profile_dialog(name)
        if action == act_delete:
            self.delete_profile(name)

    def _rename_profile_dialog(self, old_name: str):
        new_name, ok = QtWidgets.QInputDialog.getText(self, "Rename profile", "New name:", text=old_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        self.rename_profile(old_name, new_name)

    def rename_profile(self, old_name: str, new_name: str):
        # conflict check
        if new_name in self.state["profiles"]:
            QtWidgets.QMessageBox.warning(self, "Exists", "A profile with that name already exists.")
            return
        profiles = self.state["profiles"]
        if old_name not in profiles:
            return
        # move tokens list
        profiles[new_name] = profiles.pop(old_name)
        # move profile settings if present
        ps_all = self.state.get("profile_settings", {})
        if old_name in ps_all:
            ps_all[new_name] = ps_all.pop(old_name)
        # active profile
        if self.state.get("active_profile") == old_name:
            self.state["active_profile"] = new_name
        save_state(self.state)
        # refresh UI
        self.profile_list.clear()
        names = list(self.state["profiles"].keys())
        self.profile_list.addItems(names)
        self.profile_list.setCurrentRow(names.index(self.state["active_profile"]))
        self.refresh_table()
        self._load_profile_settings_into_ui()

    def delete_profile(self, name: str):
        profiles = self.state["profiles"]
        if name not in profiles:
            return
        # confirm
        ret = QtWidgets.QMessageBox.question(
            self, "Delete profile",
            f"Delete '{name}'?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
        )
        if ret != QtWidgets.QMessageBox.Yes:
            return
        # delete
        del profiles[name]
        self.state.get("profile_settings", {}).pop(name, None)
        # ensure one active profile exists
        if self.state.get("active_profile") == name:
            if profiles:
                self.state["active_profile"] = next(iter(profiles.keys()))
            else:
                self.state["profiles"] = {"New Profile": []}
                _ensure_profile_settings(self.state, "New Profile")
                self.state["active_profile"] = "New Profile"
        save_state(self.state)
        # refresh UI
        self.profile_list.clear()
        names = list(self.state["profiles"].keys())
        self.profile_list.addItems(names)
        self.profile_list.setCurrentRow(names.index(self.state["active_profile"]))
        self.refresh_table()
        self._load_profile_settings_into_ui()


# ---------- Ticker item widget ----------
class TokenItemWidget(QtWidgets.QWidget):
    def __init__(self, key: str, show_logo: bool, color: str, family: str, px: int, parent=None):
        super().__init__(parent)
        self.key = key
        self.icon = QtWidgets.QLabel()
        self.icon.setFixedSize(22, 22)
        self.icon.setScaledContents(True)
        self.text = QtWidgets.QLabel("—")
        self.text.setTextFormat(Qt.RichText)
        self.text.setMinimumHeight(28)
        self.text.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0,0,0,0); h.setSpacing(8)
        h.addWidget(self.icon); h.addWidget(self.text)
        self.set_logo_visible(show_logo)
        self.set_style(color, family, px)

    def set_logo_visible(self, on: bool):
        self.icon.setVisible(on)

    def set_style(self, color: str, family: str, px: int):
        self.text.setStyleSheet(f"color:{color}; font:{px}px \"{family}\"; background:transparent;")

    def set_text(self, s: str):
        self.text.setText(s)
        self.text.adjustSize()
        self.adjustSize()

    def set_pixmap(self, pm: Optional[QtGui.QPixmap]):
        if pm:
            self.icon.setPixmap(pm)

# ---------- Single bar per monitor ----------
class MonitorTicker(QtWidgets.QWidget):
    def __init__(self, monitor_index: int):
        super().__init__()
        self.monitor_index = monitor_index
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowIcon(APP_ICON)

        self._apply_full_width()
        self.setFixedHeight(44)

        self.container = QtWidgets.QFrame(self)
        self.track = QtWidgets.QWidget(self.container)
        self.hbox = QtWidgets.QHBoxLayout(self.track)
        self.hbox.setContentsMargins(10,6,10,6)
        self.hbox.setSpacing(28)
        self.hbox.setAlignment(Qt.AlignVCenter | Qt.AlignHCenter)

        self.items: Dict[str, TokenItemWidget] = {}
        self.order: List[str] = []

        self._offset = 0
        self._marquee = False

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._scroll_step)
        self.timer.start(16)
        self.setUpdatesEnabled(True)

        scr = self._screen_for_index(monitor_index) or QtGui.QGuiApplication.primaryScreen()
        if scr:
            scr.geometryChanged.connect(self._on_screen_change)

        self.bg_opacity = 0.7

    def _screen_for_index(self, idx: Optional[int]) -> Optional[QtGui.QScreen]:
        screens = QtGui.QGuiApplication.screens()
        if not screens or idx is None: return None
        if isinstance(idx, int):
            idx = max(0, min(idx, len(screens)-1))
            return screens[idx]
        return None

    def _apply_full_width(self):
        scr = self._screen_for_index(self.monitor_index) or QtGui.QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else QtCore.QRect(0,0,1920,1080)
        self.setGeometry(geo.x(), geo.y(), geo.width(), 44)
        self.move(geo.x(), geo.y())
        self.raise_()

    def _on_screen_change(self, *_):
        self._apply_full_width(); self._relayout()

    def set_click_through(self, enable: bool):
        try:
            hwnd = _hwnd_for_widget(self)
            if hwnd:
                _set_click_through(hwnd, bool(enable))
        except Exception as e:
            log.warning("click-through set failed: %s", e)

    def resizeEvent(self, _):
        self._relayout()

    def _relayout(self):
        self.container.setGeometry(0, 0, self.width(), self.height())
        self._layout_track()
        self._update_marquee_state()

    def _layout_track(self):
        tw = max(self.hbox.sizeHint().width(), 1)
        self.track.resize(tw, self.container.height())
        if not self._marquee:
            self.track.move((self.container.width() - tw)//2, 0)
        else:
            x = - (self._offset % tw)
            self.track.move(x, 0)

    def _update_marquee_state(self):
        too_many = len(self.order) > 10
        wider = self.hbox.sizeHint().width() > (self.container.width() - 20)
        req = bool(too_many or wider)
        if not req and self._marquee:
            self._offset = 0
        self._marquee = req
        self.hbox.setAlignment(Qt.AlignVCenter | (Qt.AlignLeft if self._marquee else Qt.AlignHCenter))

    def _scroll_step(self):
        if not self._marquee: return
        self._offset += 1
        self._layout_track()

    def set_opacity_from_profiles(self, opacities: List[float]):
        self.bg_opacity = max(opacities) if opacities else 0.7
        self.update()

    def set_initial_items(self, items: List[Dict[str,Any]]):
        while (child := self.hbox.takeAt(0)) is not None:
            w = child.widget()
            if w:
                w.setParent(None); w.deleteLater()
        self.items.clear(); self.order = []
        for it in items:
            key = it["key"]
            w = TokenItemWidget(key, it["show_logo"], it["color"], it["family"], it["px"])
            w.set_text(it["text"])
            if it.get("pixmap"): w.set_pixmap(it["pixmap"])
            self.items[key] = w; self.order.append(key)
            self.hbox.addWidget(w)
        self.hbox.invalidate(); self.track.adjustSize(); self.container.update(); self.update()
        self._layout_track(); self._update_marquee_state()
        QtWidgets.QApplication.processEvents()

    def update_items(self, items: List[Dict[str,Any]]):
        incoming_keys = [it["key"] for it in items]
        for it in items:
            key = it["key"]
            if key not in self.items:
                w = TokenItemWidget(key, it["show_logo"], it["color"], it["family"], it["px"])
                w.set_text(it["text"])
                if it.get("pixmap"): w.set_pixmap(it["pixmap"])
                self.items[key] = w; self.order.append(key); self.hbox.addWidget(w)
        for key in list(self.order):
            if key not in incoming_keys:
                w = self.items.pop(key, None)
                self.order.remove(key)
                if w:
                    w.setParent(None); w.deleteLater()
        as_dict = {it["key"]: it for it in items}
        self.order = [it["key"] for it in items]
        for i in reversed(range(self.hbox.count())):
            self.hbox.takeAt(i)
        for key in self.order:
            w = self.items.get(key)
            if not w: continue
            it = as_dict[key]
            w.set_logo_visible(it["show_logo"])
            w.set_style(it["color"], it["family"], it["px"])
            w.set_text(it["text"])
            if it.get("pixmap"):
                w.set_pixmap(it["pixmap"])
            self.hbox.addWidget(w)

        self.hbox.invalidate(); self.track.adjustSize(); self.container.update(); self.update()
        self._layout_track(); self._update_marquee_state()
        QtWidgets.QApplication.processEvents()

    def paintEvent(self, e: QtGui.QPaintEvent):
        p = QtGui.QPainter(self)
        p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing)
        bg_alpha = int(self.bg_opacity * 255)
        p.fillRect(self.rect(), QtGui.QColor(0,0,0,bg_alpha))
        p.end()
        super().paintEvent(e)

# ---------- Controller ----------
class Controller(QtCore.QObject):
    requestCycle = QtCore.Signal()

    def __init__(self, app: QtWidgets.QApplication):
        super().__init__()
        self.qtapp = app
        self.networks = load_networks()
        self.state = load_state()
        self.dashboard = Dashboard(self.state, self.networks)

        self.session: Optional[aiohttp.ClientSession] = None
        self.refresh_task: Optional[asyncio.Task] = None

        self.monitor_tickers: Dict[int, MonitorTicker] = {}
        self.last_results: Dict[str, Dict[str, Dict[str, Optional[float]]]] = defaultdict(dict)
        self.logo_cache: Dict[str, QtGui.QPixmap] = {}

        self.tray = QtWidgets.QSystemTrayIcon(APP_ICON)
        menu = QtWidgets.QMenu()
        menu.addAction("Open Dashboard", self.dashboard.show)
        menu.addAction("Open Logs", lambda: webbrowser.open(f'file:///{LOG_FILE.replace("\\\\","/")}'))
        menu.addAction("Pause All", self.pause_all)
        menu.addAction("Stop All", self.stop_all)
        menu.addSeparator()
        menu.addAction("Quit", self.qtapp.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_NAME} — {APP_DESC}")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.dashboard.startTracking.connect(self.start_all)
        self.dashboard.pauseTracking.connect(self.pause_all)
        self.dashboard.stopTracking.connect(self.stop_all)
        self.dashboard.settingsChanged.connect(self.apply_profile_settings_live)

        self.mouse_listener = None
        self.requestCycle.connect(self.cycle_active_profile)
        self.install_hotkey()

        self.dashboard.show()

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason):
        if reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()

    def install_hotkey(self):
        hk = (self.state["settings"].get("hotkey") or "F5").lower()
        with contextlib.suppress(Exception):
            keyboard.unhook_all_hotkeys()
        if self.mouse_listener:
            with contextlib.suppress(Exception):
                self.mouse_listener.stop()
            self.mouse_listener = None
        if hk == "mouse3":
            try:
                def on_click(x, y, button, pressed):
                    from pynput.mouse import Button
                    if pressed and button == Button.middle:
                        self.requestCycle.emit()
                self.mouse_listener = mouse.Listener(on_click=on_click)
                self.mouse_listener.start()
                log.info("Hotkey installed: mouse3")
            except Exception as e:
                log.warning("Mouse hook failed: %s", e)
        else:
            try:
                keyboard.add_hotkey(hk, lambda: self.requestCycle.emit(), suppress=False)
                log.info("Hotkey installed: %s", hk)
            except Exception as e:
                log.warning("Keyboard hook failed: %s", e)

    def cycle_active_profile(self):
        names = list(self.state["profiles"].keys())
        if not names: return
        curr = self.state["active_profile"]
        i = names.index(curr) if curr in names else -1
        self.state["active_profile"] = names[(i+1) % len(names)]
        save_state(self.state)
        self.dashboard.profile_list.setCurrentRow(names.index(self.state["active_profile"]))
        log.info("Active profile switched to: %s", self.state["active_profile"])

    def tokens_for(self, profile: str) -> List[Dict[str,str]]:
        toks = self.state["profiles"].get(profile, [])
        for t in toks:
            t.setdefault("custom_name","")
            t["address"] = normalize_address(t["network_id"], t["address"])
        return toks

    def ps_for(self, profile: str) -> Dict[str,Any]:
        return _ensure_profile_settings(self.state, profile)

    def start_all(self):
        for tk in self.monitor_tickers.values():
            tk.hide(); tk.deleteLater()
        self.monitor_tickers.clear()

        mon_to_profiles: Dict[int, List[str]] = defaultdict(list)
        for pname in self.state["profiles"].keys():
            ps = self.ps_for(pname)
            if ps["monitor_index"] is not None and self.tokens_for(pname):
                mon_to_profiles[int(ps["monitor_index"])].append(pname)

        for mon_idx, profiles in mon_to_profiles.items():
            tk = MonitorTicker(mon_idx)
            self.monitor_tickers[mon_idx] = tk
            tk.set_opacity_from_profiles([self.ps_for(p)["opacity"] for p in profiles])
            all_click_through = all(self.ps_for(p).get("click_through", True) for p in profiles)
            tk.set_click_through(all_click_through)
            items = self._build_monitor_items(mon_idx, use_cache=True)
            tk.set_initial_items(items)
            tk.show(); tk.raise_()

        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        self.refresh_task = asyncio.create_task(self.refresh_loop())
        log.info("Tracking started for %d monitor(s).", len(self.monitor_tickers))

    def pause_all(self):
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        log.info("All tickers paused (visible).")

    def stop_all(self):
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        for tk in self.monitor_tickers.values():
            tk.hide(); tk.deleteLater()
        self.monitor_tickers.clear()
        log.info("All tickers stopped and hidden.")

    def apply_profile_settings_live(self):
        self.start_all()
        self.install_hotkey()

    def _build_monitor_items(self, mon_idx: int, use_cache: bool=False) -> List[Dict[str,Any]]:
        items: List[Dict[str,Any]] = []
        for pname in self.state["profiles"].keys():
            ps = self.ps_for(pname)
            if ps.get("monitor_index") != mon_idx: continue
            toks = self.tokens_for(pname)
            for t in toks:  # preserve profile order
                base_key = key_for(t["network_id"], t["address"])
                merged_key = f'{pname}|{base_key}'
                vals = self.last_results[pname].get(base_key, {"price":None,"h24":None,"m5":None}) if use_cache else {"price":None,"h24":None,"m5":None}
                # choose name
                name = self.state["token_names"].get(base_key) or short_addr(t["address"])
                if ps.get("use_custom_names", False) and (t.get("custom_name") or "").strip():
                    name = t["custom_name"].strip()
                text = make_item_html(
                    name, vals["price"], vals["m5"], vals["h24"],
                    ps.get("separator_text","|"),
                    ps.get("bold_name", True),
                    ps.get("bold_price", False),
                    ps.get("bold_changes", False)
                )
                pm = self.logo_cache.get(base_key) or _load_logo_from_disk(base_key)
                if pm: self.logo_cache[base_key] = pm
                items.append({
                    "key": merged_key,
                    "text": text,
                    "color": ps["font_color"],
                    "family": ps["font_family"],
                    "px": ps["font_px"],
                    "show_logo": ps.get("show_logo", False),
                    "pixmap": pm
                })
        return items

    def _ensure_session(self):
        if self.session is None or getattr(self.session, "closed", False):
            self.session = aiohttp.ClientSession(headers={
                "Accept":"application/json",
                "Cache-Control":"no-cache",
                "Pragma":"no-cache"
            })

    async def refresh_loop(self):
        self._ensure_session()
        while True:
            try:
                mon_to_profiles: Dict[int, List[str]] = defaultdict(list)
                for pname in self.state["profiles"].keys():
                    ps = self.ps_for(pname)
                    if ps.get("monitor_index") is not None and self.tokens_for(pname):
                        mon_to_profiles[int(ps["monitor_index"])].append(pname)
                if not mon_to_profiles:
                    self.dashboard.set_status("No visible profiles.")
                    await asyncio.sleep(1.0)
                    continue

                R = 0
                for pslist in mon_to_profiles.values():
                    for pname in pslist:
                        R = max(R, int(self.ps_for(pname).get("refresh_sec", 30)))
                R = max(R, 10)
                t0 = time.time()
                log.info("Refresh start | monitors=%s interval=%ss", list(mon_to_profiles.keys()), R)

                for pname in self.state["profiles"].keys():
                    ps = self.ps_for(pname)
                    if ps.get("monitor_index") is None or not self.tokens_for(pname):
                        continue
                    tokens = list(self.tokens_for(pname))
                    by_net: Dict[str, List[str]] = defaultdict(list)
                    for t in tokens:
                        by_net[t["network_id"]].append(normalize_address(t["network_id"], t["address"]))

                    all_results: Dict[str, Dict[str, Optional[float]]] = {}
                    for net, addrs in by_net.items():
                        csv = ",".join([normalize_address(net, a) for a in addrs])
                        csv_enc = QtCore.QUrl.toPercentEncoding(csv).data().decode()
                        url = GT_MULTI.format(net=net, csv=csv_enc) + f"&_ts={int(time.time())}"
                        log.info("GET multi | net=%s | n=%d", net, len(addrs))
                        try:
                            async with self.session.get(url, timeout=15) as resp:
                                if resp.status != 200:
                                    txt = await resp.text()
                                    log.warning("HTTP %s %s | %s", resp.status, url, txt[:200])
                                    continue
                                payload = await resp.json()
                        except Exception as e:
                            log.warning("Batch request failed %s: %s", net, e)
                            continue

                        pools = {inc.get("id"): inc for inc in (payload.get("included") or []) if inc.get("type")=="pool"}

                        for tok in payload.get("data") or []:
                            attrs = tok.get("attributes") or {}
                            address = normalize_address(net, attrs.get("address",""))
                            base_key = key_for(net, address)
                            price = attrs.get("price_usd")
                            try: price_f = float(price) if price is not None else None
                            except Exception: price_f = None
                            h24 = None; m5 = None
                            rel = tok.get("relationships",{}).get("top_pools",{}).get("data") or []
                            if rel:
                                pool_id = rel[0].get("id")
                                pool = pools.get(pool_id, {})
                                pattrs = (pool.get("attributes") or {})
                                chg = (pattrs.get("price_change_percentage") or {})
                                try:   h24 = float(chg.get("h24")) if chg.get("h24") is not None else None
                                except: h24 = None
                                try:   m5  = float(chg.get("m5"))  if chg.get("m5")  is not None else None
                                except: m5 = None
                            all_results[base_key] = {"price": price_f, "h24": h24, "m5": m5}

                            tname = attrs.get("name"); timg  = attrs.get("image_url")
                            if tname: self.state["token_names"][base_key] = tname
                            if ps.get("show_logo", False) and timg:
                                self.state.setdefault("token_logos", {})[base_key] = timg

                    self.last_results[pname].update(all_results)
                    if pname == self.state["active_profile"]:
                        self.dashboard.refresh_table(self.last_results[pname])

                for mon_idx, tk in list(self.monitor_tickers.items()):
                    items = self._build_monitor_items(mon_idx, use_cache=True)
                    if not tk.order: tk.set_initial_items(items)
                    else: tk.update_items(items)
                    profiles = [p for p in self.state["profiles"].keys() if self.ps_for(p).get("monitor_index")==mon_idx]
                    tk.set_opacity_from_profiles([self.ps_for(p)["opacity"] for p in profiles])
                    all_ct = all(self.ps_for(p).get("click_through", True) for p in profiles) if profiles else True
                    tk.set_click_through(all_ct)
                    want_logos = any(self.ps_for(p).get("show_logo", False) for p in profiles)
                    if want_logos:
                        toks = []
                        for p in profiles: toks.extend(self.tokens_for(p))
                        await self._fetch_missing_logos(toks, tk)

                msg = f"Refreshed monitors: {list(self.monitor_tickers.keys())}. Next in ~{R}s."
                self.dashboard.set_status(msg)
                log.info(msg)

                elapsed = time.time() - t0
                await asyncio.sleep(max(1, R - elapsed))
            except Exception as e:
                log.exception("refresh_loop error: %s", e)
                await asyncio.sleep(2)
                self._ensure_session()

    async def _fetch_missing_logos(self, tokens: List[Dict[str,str]], tk: MonitorTicker):
        self._ensure_session()
        to_fetch = []
        for t in tokens:
            base_key = key_for(t["network_id"], t["address"])
            if base_key in self.logo_cache:
                continue
            pm_disk = _load_logo_from_disk(base_key)
            if pm_disk:
                self.logo_cache[base_key] = pm_disk
                continue
            url = self.state["token_logos"].get(base_key)
            if url: to_fetch.append((base_key, url))
        if not to_fetch: return

        sem = asyncio.Semaphore(4)
        async def one(k, url):
            async with sem:
                try:
                    async with self.session.get(url, timeout=10) as r:
                        if r.status != 200: return k, None
                        data = await r.read()
                        pm = QtGui.QPixmap()
                        if pm.loadFromData(data):
                            p = _logo_file_for(k); pm.save(str(p), "PNG")
                            return k, pm
                except Exception:
                    return k, None
                return k, None
        res = await asyncio.gather(*[one(k,u) for k,u in to_fetch])
        for k, pm in res:
            if pm:
                self.logo_cache[k] = pm

# ---------- Boot ----------
def main():
    global APP_ICON
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    APP_ICON = load_app_icon()
    app.setWindowIcon(APP_ICON)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    ctrl = Controller(app)
    app.aboutToQuit.connect(loop.stop)
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
