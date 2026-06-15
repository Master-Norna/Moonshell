from __future__ import annotations

import ctypes
import logging
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRect, QSize
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QMenu,
    QSystemTrayIcon,
    QWidget,
    QApplication,
)

from .monitor import SystemMonitor, Telemetry, machine_load
from .settings import Settings
from .state import PetState
from .sprite_config import (
    MAX_DOWN_OFFSET,
    MAX_UP_OFFSET,
    OPTIONAL_SPRITES,
    REQUIRED_SPRITES,
    SPRITE_SIZE,
    SPRITE_X,
    SPRITE_Y,
    STAGE_SIZE,
)

logger = logging.getLogger(__name__)


@dataclass
class Action:
    name: str = "idle"
    until: float = 0.0
    locked: bool = False


@dataclass
class Mood:
    """The pet's slow-moving inner weather.  Every value drifts in [0, 1] and is
    nudged by many signals at once, so what you see is the *sum* of how the day
    has gone -- not a single condition firing.  Behavior is sampled from these,
    which is what makes it read as a living companion rather than a rule table.
    """
    energy: float = 0.6       # liveliness; high -> moves/plays, low -> still
    mood: float = 0.6         # valence; high -> happy/wave, low -> pout/sad
    curiosity: float = 0.3    # interest in you / the cursor
    sleepiness: float = 0.2   # drowsiness; high -> sleepy/sleep
    attention: float = 0.3    # how "switched on" to your presence it is

    def clamp(self) -> None:
        self.energy = min(1.0, max(0.0, self.energy))
        self.mood = min(1.0, max(0.0, self.mood))
        self.curiosity = min(1.0, max(0.0, self.curiosity))
        self.sleepiness = min(1.0, max(0.0, self.sleepiness))
        self.attention = min(1.0, max(0.0, self.attention))


@dataclass(frozen=True)
class StageProfile:
    """Runtime layout in Qt device-independent pixels.

    The sprite is never allowed to define the window bounds.  The window owns a
    stable transparent stage; the 48x48 sprite is inserted into a padded 72x72
    action canvas, and the action canvas is then scaled by an integer physical
    pixel multiplier.
    """

    mode: str
    window_w: int
    window_h: int
    physical_scale: int
    bottom_margin: int
    bubble_gap: int
    font_size: int
    bubble_min_w: int
    bubble_max_w: int

    @property
    def ground_y(self) -> int:
        return self.window_h - self.bottom_margin


class SpritePetWindow(QWidget):
    """Moon-shell spirit desktop pet.

    v14 changes the rendering model from "a scaled 48x48 sprite in a tight
    window" to "a 48x48 sprite inside a padded 72x72 action stage."  This is the
    structural fix for head clipping.  A jump/hover may move inside the padded
    action stage, but the stage itself stays inside a larger transparent window.
    """

    # HD art: 96x96 master frames in a 144x144 padded stage.  At "small" the
    # sprite is drawn 1:1 (no upscaling -> pixel-perfect); "standard" is a clean
    # 2x.  Keeps the same on-screen footprint as the old 48/72 layout.
    SPRITE_SIZE = SPRITE_SIZE
    STAGE_SIZE = STAGE_SIZE
    SPRITE_X = SPRITE_X
    SPRITE_Y = SPRITE_Y
    MAX_UP_OFFSET = MAX_UP_OFFSET
    MAX_DOWN_OFFSET = MAX_DOWN_OFFSET

    # pick-up / throw physics (units: pixels per 16ms physics tick)
    GRAVITY = 1.6
    AIR_FRICTION = 0.985     # horizontal damping while airborne
    WALL_BOUNCE = 0.5        # energy kept after hitting a side wall
    BOUNCE_DAMPING = 0.42    # energy kept after a ground bounce
    MIN_BOUNCE_SPEED = 6.0   # below this we settle instead of bouncing
    MAX_BOUNCES = 3
    THROW_SCALE = 0.55       # mouse delta -> launch velocity
    MAX_THROW = 42.0         # clamp launch speed
    NEAR_RADIUS = 96         # cursor "notices me" distance to the sprite center
    EDGE_GAP = 2             # how close the *visible* sprite may get to a screen edge
    PARK_ZONE = 40           # within this of a screen edge, the pet "parks" and rests
    EDGE_PEEK_ZONE = 12      # hard against an edge -> it peeks over the side

    # ----- procedural "aliveness" motion (source-space px / frames) -----
    # Walk legs + body bob are driven by distance travelled, not a timer, so the
    # feet plant on the ground as it passes instead of sliding like a decal.
    WALK_STRIDE_PX = 16      # logical px per full 2-frame gait cycle (x physical_scale)
    WALK_BOB = 3             # vertical body rise between footfalls
    WALK_PULSE = 0.5         # how much the glide speeds up at push-off vs footfall
    BREATH_PERIOD = 28       # frames per resting breath (~3.9s at 140ms)
    BREATH_AMP = 2           # resting breath rise
    SETTLE_T = 0.26          # seconds of "arriving into the pose" pop
    SETTLE_AMP = 3           # height of that entry pop
    _BREATH_POSES = ("idle", "sit", "sleepy", "sleep", "blink")

    SPRITE_MAP = {
        "idle": "idle",
        "blink": "blink",
        "happy": "happy",
        "curious": "curious",
        "sleepy": "sleepy",
        "sleep": "sleep",
        "night": "sleep",
        "peek": "peek",
        "edge": "peek",
        "collapsed": "peek",
        "edge_idle": "peek",
        "notify": "notify",
        "hot": "notify",
        "memory": "notify",
        "code": "notify",
        "battery": "sad",
        "groom": "blink",
        "hover": "hover",
        "bounce": "happy",
        # expanded expressive actions
        "wave": "wave",
        "shy": "shy",
        "pout": "pout",
        "sad": "sad",
        "excited": "excited",
        "love": "love",
        "surprised": "surprised",
        "dizzy": "dizzy",
        "sit": "sit",
        "read": "read",
        "magic": "magic",
        "flame": "flame",
        "twirl": "twirl",
        "moon": "moon",
        "star": "star",
        "dash": "dash",
        "poof": "poof",
        # second expression batch
        "wink": "wink",
        "look_side": "look_side",
        "write": "write",
        "yawn": "yawn",
        "teleport": "teleport",
        "question": "question",
        "hide": "hide",
        "gift": "gift",
        "crystal": "crystal",
        "walk_right_4": "walk_right_4",
        "walk_left_4": "walk_left_4",
        "walk_right_1": "walk_right_1",
        "walk_right_2": "walk_right_2",
        "walk_left_1": "walk_left_1",
        "walk_left_2": "walk_left_2",
    }

    def __init__(self, settings: Settings, root: Path) -> None:
        super().__init__()
        self.settings = settings
        self.root = root
        self.assets_dir = root / "assets" / "moonshell"

        self.sprite_images: dict[str, QImage] = {}
        self._stage_cache: dict[tuple[str, int, int, int], QPixmap] = {}
        self._load_sprites()

        # Where the actual drawn character sits inside each 96px sprite, so the
        # window's transparent padding never becomes an invisible "air wall" and
        # the speech bubble can hug the real head.  (sprite-local pixels)
        self._content_left = 8
        self._content_right = 8
        self._content_top = 6
        # Empty stage rows below the feet (stage-px); used to seat the pet on the
        # taskbar instead of letting that padding float it up.
        self._foot_inset = 20
        self._compute_content_extents()

        self.frame = 0
        self.action = Action("idle", 0, False)
        self.message = ""
        self.message_until = 0.0
        self.last_idle_action = time.time()
        self._next_idle_gap = random.uniform(4.0, 9.0)
        self.last_hover = 0.0
        self.collapsed = False
        self.dragging = False
        self.drag_started = False
        self.drag_start_global = QPoint()
        self.debug_bounds = False
        self._last_bubble_rect: Optional[QRect] = None

        # autonomous strolling along the taskbar
        self.walking = False
        self.walk_dir = 1
        self.walk_target_x: Optional[int] = None
        self._walk_dist = 0.0          # ground covered this stroll, drives the gait
        self._walk_pos_f = 0.0         # float x so the step-pulse can sub-pixel glide
        self._dashing = False          # this stroll is an eager run (uses dash art)
        self._was_parked = False       # tracks parking so we only react on entering
        self._action_start = 0.0       # when the current beat began (entry "settle")

        # pick-up / throw physics
        self.held = False
        self.falling = False
        self.fall_vx = 0.0
        self.fall_vy = 0.0
        self.bounces = 0
        self._fall_peak = 0.0
        self._shaken = False
        self._shake_sign = 0
        self._shake_count = 0
        self._drag_window_origin = QPoint()
        self._prev_drag_gp = QPoint()
        self._throw_vx = 0.0
        self._throw_vy = 0.0
        self._drag_history: list[tuple[float, int, int]] = []
        self._last_phys_t = time.perf_counter()

        # cursor attentiveness + petting
        self._cursor_was_near = False
        self._last_glance = 0.0
        self._last_pet_t = 0.0
        self._pet_count = 0

        # ----- the "living companion" brain -----
        self.mood = Mood()
        self._cpu = 0.0
        self._mem = 0.0
        self._gpu: Optional[float] = None
        self._gpu_memory: Optional[float] = None
        self._load = 0.0              # smoothed 0..1 machine busyness (cpu+gpu+mem)
        self._batt: Optional[float] = None
        self._plugged: Optional[bool] = None
        self._hour = time.localtime().tm_hour
        self._idle_sec = 0.0
        self._prev_idle_sec = 0.0
        self._cursor_pos = QCursor.pos()
        self._active_streak = 0.0     # seconds you've been continuously present
        self._last_brain_t = time.time()
        self._react_last: dict[str, float] = {}
        self._slept_tonight = False
        self._greeted_morning_day = -1
        self._dusk_day = -1

        # ----- cross-session continuity: carry mood forward, remember you -----
        self._state = PetState.load()
        self._startup_absence = self._state.absence_seconds()
        if self._startup_absence < 0 or self._startup_absence > 6 * 3600:
            # first launch ever, or away a long time -> wake near baseline, but
            # honor the hour so a 3am start still feels sleepy
            self.mood.sleepiness = 0.7 if self._is_night else 0.25
        else:
            # picking up where we left off
            self.mood.energy = self._state.energy
            self.mood.mood = self._state.mood
            # a short gap relaxes drowsiness a touch; a longer one more so
            self.mood.sleepiness = max(0.0, self._state.sleepiness - self._startup_absence / 36000.0)
        self.mood.clamp()
        self._busy_samples = 0
        self._memory_samples = 0
        self._vram_samples = 0
        self._telemetry_samples = 0
        self._last_telemetry: Optional[Telemetry] = None
        self._resource_alert_until = 0.0
        self._resource_alert_pose = "notify"
        self._resource_alert_text = ""
        self._resource_alert_kind = ""
        self._resource_alert_priority = 0
        self._resource_busy = False
        self._resource_recovery_samples = 0
        self._resource_message_active = False

        self.profile = self._profile_for_mode(self.settings.size_mode)

        self._configure_window()
        self._apply_size(persist=False)
        self._build_tray()

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(140)
        self.anim_timer.timeout.connect(self._on_anim)
        self.anim_timer.start()

        # Smooth 60fps loop, only running while the pet is airborne.
        self.phys_timer = QTimer(self)
        self.phys_timer.setInterval(16)
        self.phys_timer.timeout.connect(self._on_physics)

        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(900)
        self.hover_timer.timeout.connect(self._hover_ready)

        # Persist mood + "last seen" so it survives restarts (crash-safe autosave).
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(60000)
        self.state_timer.timeout.connect(self._save_state)
        self.state_timer.start()

        self.monitor = SystemMonitor(self)
        self.monitor.telemetry.connect(self._on_telemetry)
        self.monitor.set_active(self.settings.enabled)

        clip = QApplication.clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._on_clipboard)
        self._last_clip_react = 0.0

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_state)  # persist mood on any exit

        self._connect_screen_signals()

        self._snap_to_taskbar(initial=True)
        if self.settings.enabled:
            self.show()
        else:
            self.hide()

        pose, line = self._startup_greeting()
        self._set_action(pose, 1.8, line)
        self._was_parked = self._is_parked()

    # ---------- setup ----------
    # Core poses must exist (the pet can't run without them).
    REQUIRED_SPRITES = REQUIRED_SPRITES
    # Extra expressive poses -- loaded if present, silently skipped if not, so the
    # action set can grow without ever breaking startup.
    OPTIONAL_SPRITES = OPTIONAL_SPRITES

    def _load_sprites(self) -> None:
        for name in self.REQUIRED_SPRITES:
            path = self.assets_dir / f"{name}.png"
            img = QImage(str(path))
            if img.isNull():
                raise RuntimeError(f"无法加载角色资源: {path}")
            if img.width() != self.SPRITE_SIZE or img.height() != self.SPRITE_SIZE:
                raise RuntimeError(
                    f"角色资源必须是 {self.SPRITE_SIZE}x{self.SPRITE_SIZE}: "
                    f"{path} 当前为 {img.width()}x{img.height()}"
                )
            self.sprite_images[name] = img.convertToFormat(QImage.Format.Format_ARGB32)

        for name in self.OPTIONAL_SPRITES:
            path = self.assets_dir / f"{name}.png"
            img = QImage(str(path))
            if img.isNull() or img.width() != self.SPRITE_SIZE or img.height() != self.SPRITE_SIZE:
                continue
            self.sprite_images[name] = img.convertToFormat(QImage.Format.Format_ARGB32)

        # Optional 3rd "passing" walk frame: supply just one facing and the other
        # is mirrored automatically (same trick as walk_left = flipped walk_right).
        for have, want in (("walk_right_3", "walk_left_3"), ("walk_left_3", "walk_right_3")):
            if have in self.sprite_images and want not in self.sprite_images:
                self.sprite_images[want] = self._flip_h(self.sprite_images[have])

        # The dash art faces one way (trail behind); keep a mirrored copy so the
        # pet can dash either direction.
        if "dash" in self.sprite_images:
            self.sprite_images["dash_flip"] = self._flip_h(self.sprite_images["dash"])

    @staticmethod
    def _flip_h(img: QImage) -> QImage:
        try:  # Qt 6.9+ API; fall back on older PySide6
            return img.flipped(Qt.Orientation.Horizontal)
        except AttributeError:
            return img.mirrored(True, False)

    @staticmethod
    def _alpha_extents(img: QImage) -> Optional[tuple[int, int, int, int]]:
        """Inclusive (left, top, right, bottom) of non-transparent pixels."""
        w, h = img.width(), img.height()
        bpl = img.bytesPerLine()
        data = bytes(img.constBits())
        left, top, right, bottom = w, h, -1, -1
        for y in range(h):
            base = y * bpl
            for x in range(w):
                if data[base + (x << 2) + 3]:
                    if x < left:
                        left = x
                    if x > right:
                        right = x
                    if y < top:
                        top = y
                    bottom = y
        if right < 0:
            return None
        return left, top, right, bottom

    def _compute_content_extents(self) -> None:
        """Tightest character box across every pose, so the visible sprite (not
        the padded window) decides edge clamps and the bubble anchor."""
        left = top = self.SPRITE_SIZE
        right_margin = self.SPRITE_SIZE
        for img in self.sprite_images.values():
            ext = self._alpha_extents(img)
            if ext is None:
                continue
            l, t, r, _b = ext
            left = min(left, l)
            top = min(top, t)
            right_margin = min(right_margin, self.SPRITE_SIZE - 1 - r)
        if left < self.SPRITE_SIZE:
            self._content_left = left
            self._content_right = right_margin
            self._content_top = top

        # Seat height comes from the canonical standing pose (idle): the gap from
        # the feet to the bottom of the padded stage is dead space to cut.
        idle = self.sprite_images.get("idle")
        idle_ext = self._alpha_extents(idle) if idle is not None else None
        if idle_ext is not None:
            foot_edge = idle_ext[3] + 1  # one row below the lowest opaque pixel
            self._foot_inset = max(0, self.STAGE_SIZE - self.SPRITE_Y - foot_edge)

    def _configure_window(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if hasattr(Qt.WindowType, "NoDropShadowWindowHint"):
            flags |= Qt.WindowType.NoDropShadowWindowHint
        if self.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self._disable_dwm_frame()

    def _disable_dwm_frame(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_DONOTROUND = 1
            value = ctypes.c_int(DWMWCP_DONOTROUND)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value)
            )

            DWMWA_BORDER_COLOR = 34
            DWMWA_COLOR_NONE = 0xFFFFFFFE
            border = ctypes.c_uint(DWMWA_COLOR_NONE)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border), ctypes.sizeof(border)
            )
        except Exception:
            pass

    def _profile_for_mode(self, mode: str) -> StageProfile:
        # Keep "small" and "standard" as persisted values for compatibility with
        # older settings.json files.
        if mode == "standard":
            # 96px sprite at 2x physical -> 144px stage * 2 = 288 logical stage.
            return StageProfile(
                mode="standard",
                window_w=380,
                window_h=430,
                physical_scale=2,
                bottom_margin=28,
                bubble_gap=8,
                font_size=11,
                bubble_min_w=120,
                bubble_max_w=300,
            )
        # 96px sprite at 1x physical -> drawn pixel-perfect; 144 logical stage,
        # same on-screen footprint as the previous 48/72 "small" mode.
        return StageProfile(
            mode="small",
            window_w=240,
            window_h=280,
            physical_scale=1,
            bottom_margin=24,
            bubble_gap=6,
            font_size=9,
            bubble_min_w=92,
            bubble_max_w=200,
        )

    def _apply_size(self, persist: bool = True) -> None:
        self.profile = self._profile_for_mode(self.settings.size_mode)
        self.setFixedSize(self.profile.window_w, self.profile.window_h)
        self._stage_cache.clear()
        if persist:
            self.settings.save()
            self._snap_to_taskbar(initial=False)
        self.update()

    def _current_dpr(self) -> float:
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else None
        if screen is None:
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        try:
            return max(1.0, float(screen.devicePixelRatio())) if screen else 1.0
        except Exception:
            return 1.0

    def _stage_logical_size(self, dpr: float) -> float:
        return self.STAGE_SIZE * self.profile.physical_scale / dpr

    def _snap_to_device_grid(self, value: float, dpr: float) -> float:
        if dpr <= 0:
            return round(value)
        return round(value * dpr) / dpr

    def _build_stage_pixmap(self, name: str, dpr: float, src_x_offset: int, src_y_offset: int) -> QPixmap:
        if name not in self.sprite_images:  # optional pose missing -> safe fallback
            name = "idle"
        dpr_key = int(round(dpr * 1000))
        key = (name, dpr_key, src_x_offset, src_y_offset)
        cached = self._stage_cache.get(key)
        if cached is not None:
            return cached

        src_y_offset = max(self.MAX_UP_OFFSET, min(self.MAX_DOWN_OFFSET, src_y_offset))
        stage = QImage(self.STAGE_SIZE, self.STAGE_SIZE, QImage.Format.Format_ARGB32)
        stage.fill(Qt.GlobalColor.transparent)

        p = QPainter(stage)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(
            QPoint(self.SPRITE_X + src_x_offset, self.SPRITE_Y + src_y_offset),
            self.sprite_images[name],
        )
        p.end()

        physical_px = self.STAGE_SIZE * self.profile.physical_scale
        scaled = stage.scaled(
            QSize(physical_px, physical_px),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        pix = QPixmap.fromImage(scaled)
        pix.setDevicePixelRatio(dpr)
        self._stage_cache[key] = pix
        return pix

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._make_tray_icon())
        self.tray.setToolTip("像素桌宠 · 月壳游灵 v14 stage")

        menu = QMenu()
        self.act_enabled = QAction("启用桌宠", self, checkable=True)
        self.act_enabled.setChecked(self.settings.enabled)
        self.act_enabled.triggered.connect(self._toggle_enabled)
        menu.addAction(self.act_enabled)

        self.act_top = QAction("始终置顶", self, checkable=True)
        self.act_top.setChecked(self.settings.always_on_top)
        self.act_top.triggered.connect(self._toggle_top)
        menu.addAction(self.act_top)

        # Activity dial: merges the old "自由活动" + "安静模式" toggles into one
        # high/low intensity.  high = strolls + talks + livelier; low = stays put,
        # quiet, calmer.
        act_menu = menu.addMenu("活动强度")
        self.act_lively = QAction("高 · 活泼", self, checkable=True)
        self.act_calm = QAction("低 · 沉静", self, checkable=True)
        self.act_lively.setChecked(self.settings.activity == "high")
        self.act_calm.setChecked(self.settings.activity == "low")
        self.act_lively.triggered.connect(lambda: self._set_activity("high"))
        self.act_calm.triggered.connect(lambda: self._set_activity("low"))
        act_menu.addAction(self.act_lively)
        act_menu.addAction(self.act_calm)

        size_menu = menu.addMenu("尺寸")
        self.act_small = QAction("紧凑尺寸", self, checkable=True)
        self.act_standard = QAction("标准尺寸", self, checkable=True)
        self.act_small.setChecked(self.settings.size_mode != "standard")
        self.act_standard.setChecked(self.settings.size_mode == "standard")
        self.act_small.triggered.connect(lambda: self._set_size_mode("small"))
        self.act_standard.triggered.connect(lambda: self._set_size_mode("standard"))
        size_menu.addAction(self.act_small)
        size_menu.addAction(self.act_standard)

        menu.addSeparator()
        debug = QAction("显示调试边界", self, checkable=True)
        debug.setChecked(self.debug_bounds)
        debug.triggered.connect(self._toggle_debug_bounds)
        menu.addAction(debug)

        reset = QAction("重置位置", self)
        reset.triggered.connect(lambda: self._snap_to_taskbar(initial=True, reset_x=True))
        menu.addAction(reset)

        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _make_tray_icon(self) -> QIcon:
        pix = QPixmap(64, 64)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        icon_img = self.sprite_images["idle"].scaled(
            QSize(56, 56),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        p.drawPixmap(QPoint(4, 4), QPixmap.fromImage(icon_img))
        p.end()
        return QIcon(pix)

    # ---------- positioning ----------
    def _screen_by_name(self, name: Optional[str]):
        if not name:
            return None
        for screen in QApplication.screens():
            if screen.name() == name:
                return screen
        return None

    def _window_screen(self):
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else None
        if screen is None:
            screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        return screen

    def _window_screen_available(self) -> QRect:
        screen = self._window_screen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1040)

    def _rest_y(self, ag: Optional[QRect] = None) -> int:
        """Window-top Y where the pet stands on the taskbar, on its own screen."""
        ag = ag or self._window_screen_available()
        y = (ag.bottom() + 1) - self.profile.ground_y
        return max(ag.top() + 8, min(y, ag.bottom() - self.height() + self.profile.bottom_margin))

    def _sprite_insets(self) -> tuple[float, float]:
        """Logical px from each window edge to the visible character's edges."""
        dpr = self._current_dpr()
        stage_size = self._stage_logical_size(dpr)
        stage_x = (self.width() - stage_size) / 2.0
        scale = stage_size / self.STAGE_SIZE
        left_inset = stage_x + (self.SPRITE_X + self._content_left) * scale
        right_edge = self.SPRITE_X + (self.SPRITE_SIZE - self._content_right)
        right_inset = self.width() - (stage_x + right_edge * scale)
        return left_inset, right_inset

    def _x_bounds(self, ag: Optional[QRect] = None) -> tuple[int, int]:
        # Clamp the *visible* character to the screen edge, letting the window's
        # transparent padding hang off-screen.  Otherwise that padding acts as an
        # invisible wall that stops the pet well short of the left/right edges.
        ag = ag or self._window_screen_available()
        left_inset, right_inset = self._sprite_insets()
        min_x = int(round(ag.left() + self.EDGE_GAP - left_inset))
        max_x = int(round(ag.right() - self.EDGE_GAP - self.width() + right_inset))
        if max_x < min_x:
            mid = (ag.left() + ag.right() - self.width()) // 2
            return mid, mid
        return min_x, max_x

    def _is_parked(self) -> bool:
        """True when you've tucked it into a screen corner -- it settles and rests
        there instead of wandering off across whatever you're doing."""
        min_x, max_x = self._x_bounds()
        if max_x <= min_x:
            return False
        return self.x() <= min_x + self.PARK_ZONE or self.x() >= max_x - self.PARK_ZONE

    def _at_screen_edge(self) -> bool:
        """Hard against the very left/right screen edge -- close enough that it
        peeks over the side instead of just sitting in the corner."""
        min_x, max_x = self._x_bounds()
        if max_x <= min_x:
            return False
        return self.x() <= min_x + self.EDGE_PEEK_ZONE or self.x() >= max_x - self.EDGE_PEEK_ZONE

    def _check_park_transition(self) -> None:
        """React once when it newly settles into a corner you placed it in:
        peek over the side if it's right at the edge, otherwise just nestle in."""
        parked = self._is_parked()
        if parked and not self._was_parked and not self.falling:
            if self._at_screen_edge() and "peek" in self.sprite_images:
                self._set_action("peek", 1.8, random.choice(self.EDGE_PEEK_LINES))
            else:
                pose = "sit" if "sit" in self.sprite_images else "idle"
                self._set_action(pose, 1.8, random.choice(("我在这儿待着就好。", "这个角落不错。")))
        self._was_parked = parked

    def _snap_to_taskbar(self, initial: bool = False, reset_x: bool = False) -> None:
        screen = self._screen_by_name(self.settings.screen_name) if initial else None
        ag = screen.availableGeometry() if screen is not None else self._window_screen_available()
        min_x, max_x = self._x_bounds(ag)
        if reset_x:
            x = max_x - 50
        elif initial and self.settings.x_ratio is not None:
            x = int(round(min_x + (max_x - min_x) * self.settings.x_ratio))
        elif self.settings.x is None:
            x = max_x - 50
        else:
            x = self.settings.x
        x = max(min_x, min(x, max_x))
        y = self._rest_y(ag)
        self.move(x, y)
        if not initial:
            self._save_position()

    def _save_position(self) -> None:
        min_x, max_x = self._x_bounds()
        self.settings.x = self.x()
        self.settings.x_ratio = (
            (self.x() - min_x) / (max_x - min_x) if max_x > min_x else 0.5
        )
        screen = self._window_screen()
        self.settings.screen_name = screen.name() if screen is not None else None
        self.settings.save()

    # ---------- display / multi-monitor compatibility ----------
    def _connect_screen_signals(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        for sig in ("screenAdded", "screenRemoved", "primaryScreenChanged"):
            try:
                getattr(app, sig).connect(self._on_display_changed)
            except Exception:
                pass
        try:
            app.screenAdded.connect(self._connect_one_screen)
        except Exception:
            pass
        for screen in app.screens():
            self._connect_one_screen(screen)
        wh = self.windowHandle()
        if wh is not None:
            try:
                wh.screenChanged.connect(self._on_display_changed)
            except Exception:
                pass

    def _connect_one_screen(self, screen) -> None:
        for sig in ("availableGeometryChanged", "geometryChanged",
                    "logicalDotsPerInchChanged"):
            try:
                getattr(screen, sig).connect(self._on_display_changed)
            except Exception:
                pass

    def _on_display_changed(self, *args) -> None:
        # Coalesce the burst of signals a resolution / monitor switch emits.
        QTimer.singleShot(200, self._revalidate_position)

    def _revalidate_position(self) -> None:
        """Keep the pet on a real screen after a resolution / monitor / DPI change."""
        if self.held or self.dragging or self.falling or not self.isVisible():
            return
        self._stage_cache.clear()
        min_x, max_x = self._x_bounds()
        x = max(min_x, min(self.x(), max_x))
        y = self._rest_y()
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        self._save_position()
        self.update()

    # ---------- painting ----------
    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        dpr = self._current_dpr()
        stage_size = self._stage_logical_size(dpr)
        scale = stage_size / self.STAGE_SIZE
        stage_x = self._snap_to_device_grid((self.width() - stage_size) / 2.0, dpr)
        # Drop the stage by its dead bottom padding so the feet sit on the taskbar
        # instead of floating; the empty rows below slide harmlessly off-screen.
        stage_y = self._snap_to_device_grid(
            self.profile.ground_y - stage_size + self._foot_inset * scale, dpr
        )

        sprite_name, src_x_offset, src_y_offset = self._current_render_spec()
        stage_pix = self._build_stage_pixmap(sprite_name, dpr, src_x_offset, src_y_offset)

        bubble_rect = self._paint_bubble(painter, stage_x, stage_y, stage_size, src_y_offset)
        self._last_bubble_rect = bubble_rect
        painter.drawPixmap(QPointF(stage_x, stage_y), stage_pix)

        if self.debug_bounds:
            self._paint_debug_bounds(painter, stage_x, stage_y, stage_size, bubble_rect)

        painter.end()

    def _gait_phase(self) -> float:
        """0..1 position within one 2-frame walk cycle, keyed to distance walked."""
        stride = max(1.0, self.WALK_STRIDE_PX * self.profile.physical_scale)
        return (self._walk_dist / stride) % 1.0

    def _walk_frame(self) -> str:
        if self._dashing and "dash" in self.sprite_images:
            # dash art runs left (trail streams out behind to the right); mirror it
            # for rightward travel.  (Swap the two if the trail points wrong.)
            return "dash" if self.walk_dir < 0 else "dash_flip"
        # The art's apparent facing is mirrored from its file label (the moon
        # trails *behind* the heading), so map movement -> the opposite set.
        base = "walk_left" if self.walk_dir > 0 else "walk_right"
        phase = self._gait_phase()
        # Use however many walk frames exist (walk_{dir}_1..N), keyed to distance:
        #   4+ -> step evenly through the full cycle (current art is a 4-frame loop)
        #   3  -> contact -> passing -> contact -> passing
        #   2  -> the original contact swap
        n = 0
        while f"{base}_{n + 1}" in self.sprite_images:
            n += 1
        if n >= 4:
            return f"{base}_{int(phase * n) % n + 1}"
        if n == 3:
            return f"{base}_{('1', '3', '2', '3')[int(phase * 4) % 4]}"
        return f"{base}_{1 if phase < 0.5 else 2}"

    def _breath_offset(self, name: str) -> int:
        """A smooth, continuous breath: a slow eased rise from the grounded pose.

        Sleeping breathes a touch deeper and slower so resting really reads as
        resting, not a frozen frame."""
        period = self.BREATH_PERIOD
        amp = self.BREATH_AMP
        if name in ("sleepy", "sleep"):
            period = int(period * 1.4)
            amp += 1
        phase = (self.frame % period) / period
        return -round(amp * (0.5 - 0.5 * math.cos(2 * math.pi * phase)))

    def _current_render_spec(self) -> tuple[str, int, int]:
        name = self._current_sprite_name()
        now = time.time()
        src_x_offset = 0
        src_y_offset = 0

        if self.held:
            # gentle dangle while picked up
            src_x_offset = 1 if (self.frame // 2) % 2 == 0 else -1
            src_y_offset = -4
        elif self.falling:
            src_y_offset = -4
        elif self.walking:
            # Body rises between footfalls and plants as each foot lands -- synced
            # to ground covered, so the legs grip the floor instead of sliding.
            phase = self._gait_phase()
            src_y_offset = -round(self.WALK_BOB * abs(math.cos(2 * math.pi * phase)))
        elif name == "hover":
            # Float inside the 144x144 stage.  Kept gentle so the floating head
            # always stays well clear of the top.
            src_y_offset = -3 if (self.frame // 5) % 2 == 0 else -5
        elif self._resource_alert_until > now:
            src_y_offset = -3 if (self.frame // 2) % 2 == 0 else 1
        elif self.action.until > now and self.action.name in {"happy", "bounce", "notify"}:
            src_y_offset = -2 if (self.frame // 3) % 2 == 0 else 0
        elif name in self._BREATH_POSES:
            # A slow, smooth breath so even standing perfectly still feels alive.
            src_y_offset = self._breath_offset(name)

        # A short "arriving into the pose" pop gives deliberate beats a transition
        # instead of a hard cut between two static frames.
        if (not self.held and not self.falling and not self.walking
                and self.action.until > now
                and name not in self._BREATH_POSES and name != "hover"
                and self.action.name not in {"happy", "bounce", "notify"}):
            age = now - self._action_start
            if 0.0 <= age < self.SETTLE_T:
                src_y_offset += -round(self.SETTLE_AMP * math.sin(math.pi * age / self.SETTLE_T))

        return name, src_x_offset, src_y_offset

    def _current_sprite_name(self) -> str:
        now = time.time()
        if self.held or self.falling:
            return "hover"
        if self._resource_alert_until > now:
            return self._resource_alert_pose
        if self.walking:
            return self._walk_frame()
        if self.action.until > now:
            return self.SPRITE_MAP.get(self.action.name, "notify")
        phase = self.frame % 140
        if self.collapsed:
            return "peek"
        if 76 <= phase <= 81:
            return "blink"
        # When it's genuinely drowsy, its very resting face goes heavy-lidded --
        # the baseline look itself shifts with mood, not just the occasional beat.
        if self.mood.sleepiness > 0.8 and "sleepy" in self.sprite_images:
            return "sleepy"
        if 116 <= phase <= 128:
            return "hover"
        return "idle"

    def _paint_bubble(
        self,
        painter: QPainter,
        stage_x: float,
        stage_y: float,
        stage_size: float,
        src_y_offset: int = 0,
    ) -> Optional[QRect]:
        if not self.message or time.time() > self.message_until:
            return None

        lines = self._wrap_cn(self.message, max_chars=12)
        font = QFont("Microsoft YaHei", self.profile.font_size)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = max(metrics.horizontalAdvance(line) for line in lines) if lines else 80
        line_h = metrics.height()

        bw = min(max(text_w + 28, self.profile.bubble_min_w), self.profile.bubble_max_w)
        bh = len(lines) * line_h + 16
        bx = int(round((self.width() - bw) / 2))

        # Anchor to the visible top of the head (inside the padded stage), not the
        # stage's transparent ceiling -- otherwise the bubble floats off in space.
        scale = stage_size / self.STAGE_SIZE
        head_top = stage_y + (self.SPRITE_Y + src_y_offset + self._content_top) * scale
        by = int(round(head_top - bh - self.profile.bubble_gap))
        # Never overlap the head; if a long message would run off the top, ride at
        # the window ceiling instead.
        by = min(by, int(round(head_top)) - bh - 2)
        by = max(2, by)

        rect = QRect(bx, by, bw, bh)
        painter.setPen(QPen(QColor("#7890ff"), 1))
        painter.setBrush(QColor(79, 104, 190, 235))
        painter.drawRoundedRect(rect, 12, 12)

        # Little tail so the bubble visibly belongs to the pet's head.
        head_x = self.width() / 2.0
        tail_cx = int(max(bx + 16, min(head_x, bx + bw - 16)))
        tail_top = by + bh
        tail = QPolygon([
            QPoint(tail_cx - 7, tail_top - 1),
            QPoint(tail_cx + 7, tail_top - 1),
            QPoint(tail_cx, tail_top + 8),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(79, 104, 190, 235))
        painter.drawPolygon(tail)
        painter.setPen(QPen(QColor("#7890ff"), 1))
        painter.drawLine(tail.at(0), tail.at(2))
        painter.drawLine(tail.at(1), tail.at(2))

        painter.setPen(QColor("#ffffff"))

        y = by + 8 + metrics.ascent()
        for line in lines:
            painter.drawText(
                QRect(bx + 14, y - metrics.ascent(), bw - 28, line_h),
                Qt.AlignmentFlag.AlignCenter,
                line,
            )
            y += line_h
        return rect

    def _paint_debug_bounds(
        self,
        painter: QPainter,
        stage_x: float,
        stage_y: float,
        stage_size: float,
        bubble_rect: Optional[QRect],
    ) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 200, 0, 220), 1))
        painter.drawRect(QRect(int(stage_x), int(stage_y), int(stage_size), int(stage_size)))
        painter.setPen(QPen(QColor(0, 220, 255, 200), 1))
        sprite_scale = stage_size / self.STAGE_SIZE
        sx = int(stage_x + self.SPRITE_X * sprite_scale)
        sy = int(stage_y + self.SPRITE_Y * sprite_scale)
        sw = int(self.SPRITE_SIZE * sprite_scale)
        painter.drawRect(QRect(sx, sy, sw, sw))
        if bubble_rect is not None:
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
            painter.drawRect(bubble_rect)

    def _is_interactive_point(self, point: QPoint) -> bool:
        """Only the visible pet and speech bubble should consume desktop input."""
        now = time.time()
        if (
            self._last_bubble_rect is not None
            and self.message
            and now <= self.message_until
        ):
            bubble_hit = self._last_bubble_rect.adjusted(-2, -2, 2, 10)
            if bubble_hit.contains(point):
                return True

        dpr = self._current_dpr()
        stage_size = self._stage_logical_size(dpr)
        scale = stage_size / self.STAGE_SIZE
        if scale <= 0:
            return False
        stage_x = (self.width() - stage_size) / 2.0
        stage_y = self.profile.ground_y - stage_size + self._foot_inset * scale
        name, src_x_offset, src_y_offset = self._current_render_spec()
        image = self.sprite_images.get(name) or self.sprite_images["idle"]
        extents = self._alpha_extents(image)
        if extents is None:
            return False
        left, top, right, bottom = extents
        # Use the pose's visible bounding box instead of exact alpha pixels.
        # Pixel-level hit testing made holes between limbs click-through, so a
        # drag could fail depending on the exact pixel pressed.
        grab_pad = 4
        hit_left = stage_x + (self.SPRITE_X + src_x_offset + left - grab_pad) * scale
        hit_top = stage_y + (self.SPRITE_Y + src_y_offset + top - grab_pad) * scale
        hit_right = stage_x + (self.SPRITE_X + src_x_offset + right + 1 + grab_pad) * scale
        hit_bottom = stage_y + (self.SPRITE_Y + src_y_offset + bottom + 1 + grab_pad) * scale
        return hit_left <= point.x() < hit_right and hit_top <= point.y() < hit_bottom

    def _wrap_cn(self, text: str, max_chars: int = 12) -> list[str]:
        text = text.strip()
        if len(text) <= max_chars:
            return [text]
        seps = "，。；、：,.!?！？ "
        lines: list[str] = []
        cur = text
        while len(cur) > max_chars and len(lines) < 2:
            cut = max_chars
            for i in range(min(len(cur) - 1, max_chars), max(4, max_chars - 5), -1):
                if cur[i] in seps:
                    cut = i + 1
                    break
            lines.append(cur[:cut].strip())
            cur = cur[cut:].strip()
        if cur:
            lines.append(cur if len(cur) <= max_chars + 2 else cur[:max_chars] + "…")
        return lines[:3]

    # ---------- activity intensity (merged wander + quiet) ----------
    @property
    def _lively(self) -> bool:
        """High activity: strolls, talks, livelier beats."""
        return self.settings.activity == "high"

    @property
    def _quiet(self) -> bool:
        """Low activity: stays put, keeps speech to itself."""
        return self.settings.activity == "low"

    @property
    def _is_night(self) -> bool:
        """Late night (23:00-05:59): the only window for night-flavored beats --
        riding the crescent moon, fully bedding down to sleep, moon/night lines.
        Single source of truth so these never leak into broad daylight."""
        return self._hour >= 23 or self._hour < 6

    # ---------- state/actions ----------
    def say(self, msg: str, duration: float = 3.0, force: bool = False) -> None:
        if self._quiet and not force:
            return
        self.message = msg
        self.message_until = time.time() + duration
        self.update()

    def _set_action(
        self,
        action: str,
        seconds: float = 2.2,
        message: Optional[str] = None,
        force: bool = False,
    ) -> None:
        now = time.time()
        if (
            not force
            and self.action.until > now
            and self.action.locked
            and action not in {"idle", "edge"}
        ):
            return
        # Stop strolling so the pet turns to face whatever just happened.
        self.walking = False
        self.walk_target_x = None
        self._action_start = now
        self.action = Action(action, now + seconds, locked=seconds >= 2.0)
        if message:
            self.say(message, duration=max(2.0, min(3.8, seconds + 0.4)))
        self.update()

    # ---------- the brain: sensing -> mood -> behavior ----------
    def _on_telemetry(self, t: Telemetry) -> None:
        self._telemetry_samples += 1
        self._last_telemetry = t
        self._cpu, self._mem, self._gpu = t.cpu, t.mem, t.gpu
        self._gpu_memory = t.gpu_memory_pct
        self._batt, self._plugged, self._hour = t.battery_pct, t.plugged, t.hour
        self._prev_idle_sec = self._idle_sec
        self._idle_sec = t.idle_seconds
        # One abstract "the machine is busy" number, vendor-agnostic: whichever of
        # CPU / GPU is hotter dominates, memory adds a floor.  Smoothed so a single
        # spike doesn't jerk the mood.
        target = machine_load(t.cpu, t.mem, t.gpu)
        self._load += (target - self._load) * 0.5

        # Require several consecutive busy samples, then react deterministically.
        # This stays calm during spikes while making a real sustained load visible.
        # CPU is sampled every 2s. Require four genuinely high readings so a
        # launch spike or foreground app switch does not look like distress.
        if t.cpu >= 85:
            self._busy_samples += 1
        elif t.cpu < 70:
            self._busy_samples = 0
        # Memory pressure is only noteworthy near exhaustion, not merely because
        # a large app keeps a healthy working set.
        if t.mem >= 96:
            self._memory_samples += 1
        elif t.mem < 92:
            self._memory_samples = 0
        # Only count fresh nvidia-smi readings. Cached GPU values previously
        # turned one brief peak into three fake "sustained" samples.
        if t.gpu_sampled:
            gpu_active = t.gpu is not None and (
                t.gpu >= 85
                or (
                    t.gpu >= 55
                    and t.gpu_memory_pct is not None
                    and t.gpu_memory_pct >= 50
                )
            )
            if gpu_active:
                self._vram_samples += 1
            elif t.gpu is None or t.gpu < 40:
                self._vram_samples = 0
        self._update_resource_state(t)
        if self._batt is not None and not self._plugged and self._batt <= 18:
            self._react(
                "sad",
                2.2,
                "battery",
                240,
                lines=("唔…有点没力气了。",),
            )

        self.tray.setToolTip(f"月壳游灵 · {self._mood_phrase()}")
        if self._telemetry_samples % 15 == 0:
            logger.info(
                "Telemetry healthy: cpu=%.1f gpu=%s vram=%s mem=%.1f idle=%.1fs",
                t.cpu,
                "n/a" if t.gpu is None else f"{t.gpu:.1f}",
                "n/a" if t.gpu_memory_pct is None else f"{t.gpu_memory_pct:.1f}",
                t.mem,
                t.idle_seconds,
            )

    def _update_resource_state(self, t: Telemetry) -> None:
        candidates: list[tuple[int, str, str, str]] = []
        if self._memory_samples >= 5:
            candidates.append((1, "memory", "notify", "唔…有点挤呢。"))
        if self._busy_samples >= 4:
            candidates.append((2, "load", "surprised", "呼…忙得有点喘了。"))
        if self._vram_samples >= 2:
            candidates.append((3, "vram", "surprised", "呼…有点忙不过来了。"))

        if not self._resource_busy and candidates:
            _, kind, pose, text = max(candidates)
            self._enter_resource_state(kind, pose, text)

        if self._resource_alert_kind == "memory":
            recovered = t.mem < 90
        elif self._resource_alert_kind == "load":
            recovered = t.cpu < 60
        elif self._resource_alert_kind == "vram":
            # Allocated VRAM can remain high after work stops. Recovery is based
            # on fresh compute activity, not whether an app releases its cache.
            recovered = bool(t.gpu_sampled and (t.gpu is None or t.gpu < 25))
        else:
            recovered = True
        if self._resource_busy and recovered:
            self._resource_recovery_samples += 1
            if self._resource_recovery_samples >= 3:
                self._leave_resource_state()
        else:
            self._resource_recovery_samples = 0

    def _enter_resource_state(self, kind: str, pose: str, text: str) -> None:
        now = time.time()
        self._resource_busy = True
        self._resource_alert_kind = kind
        self._resource_alert_priority = {"memory": 1, "load": 2, "vram": 3}.get(kind, 1)
        self._resource_alert_pose = pose
        self._resource_alert_text = text
        self._resource_alert_until = now + 4.0
        self.walking = False
        self.walk_target_x = None
        self._resource_message_active = not self._quiet
        self._set_action(pose, 4.0, text, force=True)
        logger.info("Resource state entered: kind=%s", kind)
        self.update()

    def _leave_resource_state(self) -> None:
        logger.info("Resource state recovered: last_kind=%s", self._resource_alert_kind)
        self._resource_busy = False
        self._resource_alert_until = 0.0
        self._resource_alert_kind = ""
        self._resource_alert_priority = 0
        self._resource_alert_text = ""
        self._busy_samples = 0
        self._memory_samples = 0
        self._vram_samples = 0
        self._resource_recovery_samples = 0
        if self._resource_message_active and time.time() <= self.message_until:
            self.message = ""
            self.message_until = 0.0
        self._resource_message_active = False
        self.update()

    def _mood_phrase(self) -> str:
        m = self.mood
        if m.sleepiness > 0.7:
            return "有点困了…"
        if m.energy > 0.7 and m.mood > 0.6:
            return "精神不错"
        if m.mood < 0.35:
            return "没什么精神"
        if m.curiosity > 0.6:
            return "好奇地看着你"
        return "安安静静陪着你"

    def _startup_greeting(self) -> tuple[str, str]:
        """Greet by how long we've been apart -- so a relaunch isn't a blank reset
        but feels like it remembers you."""
        absence = self._startup_absence
        hour = self._hour
        if absence < 0:
            return "wave", "初次见面，请多关照。"     # first launch ever
        if absence < 180:                              # just restarted (<3 min)
            return "idle", "嗯，我回来了。"
        if absence >= 20 * 3600:                       # a day or more away
            return ("excited" if "excited" in self.sprite_images else "happy"), \
                random.choice(("好久不见呀！", "你终于回来啦～"))
        if 5 * 3600 <= absence:                        # several hours -> time-of-day
            if 5 <= hour < 11:
                return "wave", "早呀，新的一天。"
            if 18 <= hour < 23:
                return "wave", "晚上好呀。"
        return "wave", random.choice(self.GREETINGS)

    def _save_state(self) -> None:
        self._state.energy = self.mood.energy
        self._state.mood = self.mood.mood
        self._state.sleepiness = self.mood.sleepiness
        self._state.save()

    def _on_clipboard(self) -> None:
        now = time.time()
        if now - self._last_clip_react < 20:
            return
        clip = QApplication.clipboard()
        try:
            if not (clip and clip.text()):
                return
        except Exception:
            return
        self._last_clip_react = now
        self.mood.attention = min(1.0, self.mood.attention + 0.15)
        self._react("notify", 0.7, "copy", 20)  # a quiet "noticed that"

    def _update_brain(self, now: float) -> None:
        dt = min(0.5, now - self._last_brain_t)
        self._last_brain_t = now

        cp = QCursor.pos()
        speed = math.hypot(cp.x() - self._cursor_pos.x(),
                           cp.y() - self._cursor_pos.y()) / max(dt, 1e-3)
        self._cursor_pos = cp
        active = self._idle_sec < 1.5
        if self._idle_sec > 240:       # a real break resets the "been here a while" clock
            self._active_streak = 0.0
        elif active:
            self._active_streak += dt

        m = self.mood
        hour = self._hour
        night = self._is_night

        if night:
            m.sleepiness += 0.012 * dt
        if self._idle_sec > 25:
            m.sleepiness += 0.006 * dt
        if active and not night:
            m.sleepiness -= 0.010 * dt
        if 6 <= hour < 10:           # morning: waking up
            m.sleepiness -= 0.010 * dt
            m.energy += 0.006 * dt
        if 13 <= hour < 15:          # after-lunch lull: a little drowsy
            m.sleepiness += 0.004 * dt
        if 18 <= hour < 22 and not night:  # dusk: gently winds down
            m.energy -= 0.002 * dt

        if speed > 60:
            m.curiosity += 0.030 * dt * min(2.5, speed / 300.0)
            m.attention += 0.10 * dt
        elif self._idle_sec > 6:
            m.curiosity -= 0.012 * dt
            m.attention -= 0.04 * dt

        if self._load > 0.75:          # busy machine -> slowly a bit weary/uneasy
            m.energy -= 0.006 * dt
            m.mood -= 0.004 * dt
        elif self._load < 0.25:        # calm machine -> recovers
            m.energy += 0.003 * dt
        if self._batt is not None and not self._plugged and self._batt <= 25:
            m.energy -= 0.005 * dt

        # drift toward time-aware baselines
        m.energy += (0.60 - m.energy) * 0.015 * dt
        m.mood += (0.58 - m.mood) * 0.015 * dt
        m.curiosity += (0.18 - m.curiosity) * 0.040 * dt
        m.attention += (0.22 - m.attention) * 0.060 * dt
        m.sleepiness += ((0.72 if night else 0.18) - m.sleepiness) * 0.008 * dt
        m.clamp()

    def _react(
        self,
        pose: str,
        secs: float,
        kind: str,
        cooldown: float,
        lines: tuple[str, ...] = (),
        force: bool = False,
    ) -> bool:
        """Fire a state-driven beat, rate-limited per kind and mostly silent.

        Returns True if it actually played (so the idle loop yields to it)."""
        now = time.time()
        if self.held or self.dragging or self.falling:
            return False
        if not force and self.walking:
            return False
        if not force and self.action.until > now and self.action.locked:
            return False
        if now - self._react_last.get(kind, 0) < cooldown:
            return False
        self._react_last[kind] = now
        self.last_idle_action = now
        msg = None
        if lines and not self._quiet and (force or random.random() < 0.4):
            msg = random.choice(lines)
        self._set_action(pose, secs, msg, force=force)
        return True

    def _maybe_life_reactions(self, now: float) -> bool:
        """The handful of crisp, companion-y moments worth noticing explicitly."""
        hour = self._hour
        night = self._is_night

        # you just came back to the keyboard -> a quick peek
        if self._prev_idle_sec > 20 and self._idle_sec < 2.0:
            self.mood.attention = min(1.0, self.mood.attention + 0.3)
            return self._react("peek", 1.3, "return", 12)

        # deep night, settled in -> drifts off (once per night)
        if night and not self._slept_tonight and self.mood.sleepiness > 0.7 and self._idle_sec > 8:
            self._slept_tonight = True
            return self._react("sleep", 3.6, "night", 300,
                               lines=("夜深了…我先眯一会儿。", "困了呢，晚安。"))
        if not night:
            self._slept_tonight = False

        # morning hello, once a day
        today = time.localtime().tm_yday
        if 6 <= hour < 10 and self._greeted_morning_day != today and self._idle_sec < 30:
            self._greeted_morning_day = today
            return self._react("wave", 1.8, "morning", 300, lines=("早呀。", "天亮啦。"))

        # dusk wind-down, once a day -- a calm "evening's coming" beat
        if 18 <= hour < 21 and self._dusk_day != today and self._idle_sec < 60:
            self._dusk_day = today
            return self._react("hover", 1.8, "dusk", 300, lines=("天要黑了呢。", "黄昏了，慢下来吧。"))

        # left alone a long time -> nods off
        if self._idle_sec > 100 and self.mood.sleepiness > 0.5:
            return self._react("sleepy", 2.8, "drowsy", 45)

        # been present a long unbroken stretch -> a gentle "stretch" nudge
        if self._active_streak > 50 * 60:
            self._active_streak = 0.0
            return self._react("hover", 2.0, "stretch", 600, lines=("坐好久了，伸个懒腰吧。",))

        return False

    def _on_anim(self) -> None:
        self.frame += 1
        if not self.isVisible():   # disabled / hidden -> let the brain idle too
            return
        now = time.time()
        self._update_brain(now)

        # While picked up, thrown, or actively dragged, the physics loop / mouse
        # owns the position; just keep the dangle animation ticking.
        if self.held or self.falling or self.dragging:
            self.update()
            return

        if self.walking:
            self._step_walk()
            self.update()
            return

        busy = self.action.until > now
        idle_ok = not busy and not self.collapsed and self.isVisible()
        if idle_ok:
            if not self._maybe_life_reactions(now):
                if now - self.last_idle_action > self._next_idle_gap:
                    self._begin_idle_beat(now)
                else:
                    self._maybe_notice_cursor(now)
        self.update()

    def _maybe_notice_cursor(self, now: float) -> None:
        """Perk up when the cursor approaches the sprite itself (not the window)."""
        cp = QCursor.pos()
        center = self.geometry().center()
        near = math.hypot(cp.x() - center.x(), cp.y() - center.y()) < self.NEAR_RADIUS
        if near and not self._cursor_was_near and now - self._last_glance > 6.0:
            self._last_glance = now
            self.last_idle_action = now  # don't immediately stack another beat
            self.mood.attention = min(1.0, self.mood.attention + 0.2)
            if self.mood.sleepiness >= 0.78:
                # too sleepy to perk up -- just a drowsy half-peek, stays settled.
                self._set_action("peek" if "peek" in self.sprite_images else "sleepy", 1.0)
            elif (abs(cp.x() - center.x()) > abs(cp.y() - center.y())
                  and "look_side" in self.sprite_images):
                # cursor coming in from a side -> turn the head and watch it
                self.mood.curiosity = min(1.0, self.mood.curiosity + 0.2)
                self._set_action("look_side", seconds=1.2)
            else:
                self.mood.curiosity = min(1.0, self.mood.curiosity + 0.2)
                self._set_action("curious", seconds=1.1)
        self._cursor_was_near = near

    # ---------- voice ----------
    # A quiet, dreamy little moon spirit: soft, warm, unhurried, a touch sleepy.
    # Short lines, low-key punctuation, never peppy or coach-like.
    GREETINGS = ("你来啦。", "嗯，我在的。", "今天也见到你了。", "唔…我醒着呢。")
    IDLE_LINES = (
        "在发呆呢…",
        "今天也辛苦了。",
        "我就在这儿。",
        "嗯——",
        "歇一会儿也好。",
        "陪你待一会儿。",
    )
    # Moon / nightfall lines -- only said at night, so it never talks about the
    # moonlight at 2pm.
    NIGHT_IDLE_LINES = (
        "月亮还没出来。",
        "夜色挺安静的。",
        "夜深了呢。",
    )
    EDGE_PEEK_LINES = ("外面有什么呢…", "我探头看看。", "到边边啦。", "唔，这是尽头。")
    PET_LINES_SOFT = ("唔？", "嗯？", "怎么啦。", "在的。")
    PET_LINES_MID = ("嘿嘿。", "有点痒。", "还要呀？", "唔嗯～")
    PET_LINES_LOTS = ("好啦好啦…", "蹭蹭。", "再摸…要化掉了。", "嗯，知道你喜欢我啦。")

    # ---------- idle life ----------
    def _begin_idle_beat(self, now: float) -> None:
        """Sample the next little beat from the current mood.

        Nothing here is a fixed sequence: each pose carries a weight that swells
        or shrinks with energy / mood / curiosity / sleepiness, so the same minute
        plays differently depending on how the pet (and your machine) is feeling.
        """
        self.last_idle_action = now
        m = self.mood
        night = self._is_night

        # ----- coherence gate: a sleepy pet shouldn't pop up to walk and grin -----
        # Deep in sleep it only stirs softly; it won't stroll or play until
        # something (you, the morning) actually wakes it back up.  At night it may
        # drift up to ride the crescent moon for a beat before settling again.
        if m.sleepiness >= 0.78:
            if night:
                # night: it can fully bed down (lie-down sleep) and ride the moon
                sleep_pool: list[tuple[str, float]] = [
                    ("sleep", 3.0), ("sleepy", 2.0), ("blink", 0.5), ("moon", 0.7),
                ]
            else:
                # daytime: just dozes upright -- no full night-sleep, no moon
                sleep_pool = [("sleepy", 2.5), ("sit", 1.5), ("blink", 0.6)]
            sleep_pool = [(n, w) for (n, w) in sleep_pool
                          if self.SPRITE_MAP.get(n, n) in self.sprite_images]
            kind = random.choices([n for n, _ in sleep_pool], [w for _, w in sleep_pool])[0]
            secs = 0.5 if kind == "blink" else random.uniform(3.0, 5.0)
            self._set_action(kind, seconds=secs)
            self._next_idle_gap = random.uniform(6.0, 12.0)
            return

        drowsy = m.sleepiness >= 0.55

        # Strolling needs real liveliness; a drowsy or parked pet stays put.  When
        # you nestle it into a screen corner it settles there and won't wander off
        # across your work -- low activity does the same globally.
        can_wander = self._lively and not self._is_parked()
        if not drowsy and can_wander:
            if (m.curiosity > 0.55 and m.attention > 0.40
                    and random.random() < 0.35
                    and self._start_walk_toward(QCursor.pos().x())):
                # bursting with energy -> it eagerly dashes over instead of ambling
                self._dashing = (m.energy > 0.7 and "dash" in self.sprite_images
                                 and random.random() < 0.6)
                self._next_idle_gap = random.uniform(7.0, 13.0)
                return
            walk_p = 0.5 * m.energy * (1.0 - m.sleepiness)
            if random.random() < walk_p and self._start_walk():
                self._next_idle_gap = random.uniform(8.0, 16.0)
                return

        # In-place beat.  Lively poses are scaled by wakefulness so they fade out
        # smoothly as it tires, rather than popping in at odd moments.
        wake = 1.0 - m.sleepiness
        pool: list[tuple[str, float]] = [
            ("idle", 1.0),
            ("blink", 1.2),
            ("sit", 0.6 + 1.8 * m.sleepiness),
            ("sleepy", 0.1 + 2.6 * m.sleepiness),
            ("curious", (0.3 + 2.2 * m.curiosity) * wake),
            ("hover", (0.2 + 0.8 * m.energy) * wake),
            ("happy", (0.15 + 1.6 * m.mood * m.energy) * wake),
            ("wave", (0.08 + 0.7 * m.mood * m.attention) * wake),
            ("shy", (0.12 + 0.5 * m.mood) * wake),
            ("pout", 0.04 + 0.6 * (1.0 - m.mood)),
            ("sad", 0.02 + 0.5 * (1.0 - m.mood) * (1.0 - m.energy)),
            # a playful wink when it's in good spirits and you're around
            ("wink", (0.10 + 0.6 * m.mood * m.attention) * wake),
            # a big yawn as it gets drowsy (but before it actually nods off)
            ("yawn", (0.1 + 1.4 * m.sleepiness) * (1.0 if m.sleepiness < 0.78 else 0.0)),
            # a puzzled little beat when curious but nothing's going on
            ("question", (0.05 + 0.5 * m.curiosity) * wake * (1.0 if self._idle_sec > 6 else 0.3)),
            # ----- the "cool" batch, tied to context so it never feels random -----
            # reads/writes alongside you when the machine (you) is busy and you're
            # here; little spells / spins / treasures when it's in a great mood.
            ("read", (0.1 + 1.4 * self._load) * wake * (1.0 if self._idle_sec < 8 else 0.25)),
            ("write", (0.08 + 1.2 * self._load) * wake * (1.0 if self._idle_sec < 8 else 0.25)),
            ("magic", (0.05 + 0.9 * m.mood * m.energy) * wake),
            ("twirl", (0.04 + 0.8 * m.mood * m.energy) * wake),
            ("star", (0.04 + 0.7 * m.mood * m.energy) * wake),
            ("crystal", (0.04 + 0.7 * m.mood * m.energy) * wake),
            ("gift", (0.02 + 0.5 * m.mood * m.attention) * wake),
            # riding the crescent moon is a night-only beat -- never in daylight
            ("moon", (0.5 * (0.4 + 0.8 * m.sleepiness)) if night else 0.0),
            # tucked against a screen edge -> occasionally peeks over the side
            ("peek", 1.2 if self._at_screen_edge() else 0.0),
        ]
        # Nestled in a corner, or simply a calm mood -> lean into settled poses and
        # skip the showy stuff (it's resting there, not performing).
        if self._is_parked() or self._quiet:
            calm = {"idle", "blink", "sit", "sleepy"}
            pool = [(n, w * (1.8 if n in calm else 0.35)) for (n, w) in pool]

        # drop poses whose art isn't present, then sample
        pool = [(n, w) for (n, w) in pool if w > 0 and self.SPRITE_MAP.get(n, n) in self.sprite_images]
        kind = random.choices([n for n, _ in pool], [w for _, w in pool])[0]

        if kind == "blink":
            secs = 0.5
        elif kind in ("sleep", "sleepy", "yawn"):
            secs = random.uniform(2.2, 3.6)
        elif kind in ("read", "write", "magic", "twirl", "moon", "flame",
                      "star", "crystal", "gift"):
            secs = random.uniform(2.2, 3.4)  # special beats linger to be enjoyed
        else:
            secs = random.uniform(1.1, 2.2)
        # Quieter when sleepy/low, a touch chattier when up; still mostly silent.
        # Moon/nightfall lines are only in play at night, so it never muses about
        # moonlight in the middle of the day.
        say_chance = 0.12 + 0.06 * m.mood - 0.08 * m.sleepiness
        line_pool = self.IDLE_LINES + (self.NIGHT_IDLE_LINES if night else ())
        message = random.choice(line_pool) if random.random() < say_chance else None
        self._set_action(kind, seconds=secs, message=message)
        # Sleepier -> longer gaps; calm mood / parked -> slower still.
        gap = random.uniform(5.0, 11.0) * (1.0 + 0.8 * m.sleepiness)
        if self._quiet or self._is_parked():
            gap *= 1.5
        self._next_idle_gap = gap

    def _start_walk(self) -> bool:
        min_x, max_x = self._x_bounds()
        if max_x <= min_x:
            return False
        target = random.randint(min_x, max_x)
        if abs(target - self.x()) < 48:  # too short to be worth animating
            return False
        self.walk_target_x = target
        self.walk_dir = 1 if target > self.x() else -1
        self._walk_dist = 0.0
        self._walk_pos_f = float(self.x())
        self._dashing = False
        self.walking = True
        return True

    def _start_walk_toward(self, cursor_x: int) -> bool:
        """Amble so the pet ends up roughly under the cursor (clamped on-screen)."""
        min_x, max_x = self._x_bounds()
        if max_x <= min_x:
            return False
        target = max(min_x, min(cursor_x - self.width() // 2, max_x))
        if abs(target - self.x()) < 60:  # already close enough
            return False
        self.walk_target_x = target
        self.walk_dir = 1 if target > self.x() else -1
        self._walk_dist = 0.0
        self._walk_pos_f = float(self.x())
        self._dashing = False
        self.walking = True
        return True

    def _step_walk(self) -> None:
        if self.walk_target_x is None or self.dragging or self.collapsed or not self.isVisible():
            self._end_walk()
            return
        base = max(2.0, self.profile.physical_scale * 2.0)
        if self._dashing:
            base *= 2.2  # an eager run covers ground faster
        # step-pulse: the glide speeds up as a leg swings through and eases at each
        # footfall, so the body lurches per step instead of sliding at one speed.
        factor = 1.0 + self.WALK_PULSE * math.cos(2 * math.pi * 2 * self._gait_phase())
        speed = base * factor
        remaining = self.walk_target_x - self._walk_pos_f
        if abs(remaining) <= speed:
            self._walk_dist += abs(remaining)
            self._walk_pos_f = float(self.walk_target_x)
            self.move(self.walk_target_x, self.y())
            self._end_walk()
            return
        step = speed if remaining > 0 else -speed
        self._walk_pos_f += step
        self._walk_dist += abs(step)
        self.move(int(round(self._walk_pos_f)), self.y())

    def _end_walk(self) -> None:
        self.walking = False
        self._dashing = False
        self.walk_target_x = None
        self._save_position()
        self._check_park_transition()
        self.last_idle_action = time.time()
        self._next_idle_gap = random.uniform(5.0, 11.0)

    # ---------- pick-up / throw physics ----------
    def _on_physics(self) -> None:
        if not self.falling or not self.isVisible():
            self.falling = False
            self.phys_timer.stop()
            return

        now = time.perf_counter()
        dt = max(0.008, min(0.050, now - self._last_phys_t))
        self._last_phys_t = now
        tick_scale = dt / 0.016
        rest = self._rest_y()
        min_x, max_x = self._x_bounds()

        self.fall_vy += self.GRAVITY * tick_scale
        self.fall_vx *= self.AIR_FRICTION ** tick_scale
        self._fall_peak = max(self._fall_peak, self.fall_vy)

        new_x = self.x() + self.fall_vx * tick_scale
        new_y = self.y() + self.fall_vy * tick_scale

        # bounce off the side walls
        if new_x <= min_x:
            new_x = float(min_x)
            self.fall_vx = abs(self.fall_vx) * self.WALL_BOUNCE
        elif new_x >= max_x:
            new_x = float(max_x)
            self.fall_vx = -abs(self.fall_vx) * self.WALL_BOUNCE

        if new_y >= rest:
            landed_speed = self.fall_vy
            if landed_speed > self.MIN_BOUNCE_SPEED and self.bounces < self.MAX_BOUNCES:
                self.fall_vy = -landed_speed * self.BOUNCE_DAMPING
                self.fall_vx *= 0.7
                self.bounces += 1
                self.move(int(round(new_x)), rest)
            else:
                self.move(int(round(new_x)), rest)
                self._land()
            return

        self.move(int(round(new_x)), int(round(new_y)))

    def _land(self) -> None:
        self.falling = False
        self.phys_timer.stop()
        self.fall_vx = self.fall_vy = 0.0
        self.bounces = 0
        peak, self._fall_peak = self._fall_peak, 0.0
        shaken, self._shaken = self._shaken, False
        self._save_position()
        self.last_idle_action = time.time()
        self._next_idle_gap = random.uniform(4.0, 9.0)
        # A gentle drop is a cheer; a hard slam dizzies it -- and a really rough
        # toss sometimes flares it up in a little huff of flame instead.
        if peak >= 30 and not shaken and "flame" in self.sprite_images and random.random() < 0.5:
            self._set_action("flame", seconds=2.4, message=random.choice(("呼…！", "别乱扔啦——")))
        elif shaken or peak >= 30:
            self._set_action("dizzy", seconds=1.6, message=random.choice(("唔哇…", "转晕了…")))
        elif peak >= 14:
            self._set_action("excited", seconds=1.0)
        else:
            self._set_action("happy", seconds=0.8)
        # the toss already plays a reaction; just sync the park flag so a later
        # stroll into/out of the corner still triggers correctly
        self._was_parked = self._is_parked()

    def _on_pet(self) -> None:
        """A tap with no drag = a head pat; repeated pats escalate the reaction."""
        now = time.time()
        if now - self._last_pet_t < 2.5:
            self._pet_count += 1
        else:
            self._pet_count = 1
        self._last_pet_t = now

        secs = 1.1
        if self._pet_count >= 6 and "twirl" in self.sprite_images:
            line, pose = random.choice(("嘿嘿，转个圈！", "你最好啦～")), "twirl"
            secs = 2.2  # let the happy spin play out
        elif self._pet_count >= 4:
            line, pose = random.choice(self.PET_LINES_LOTS), "love"
        elif self._pet_count >= 2:
            line, pose = random.choice(self.PET_LINES_MID), "shy"
        else:
            # first pat: usually a soft smile, sometimes a playful wink
            pose = "wink" if ("wink" in self.sprite_images and random.random() < 0.4) else "happy"
            line = random.choice(self.PET_LINES_SOFT)
        # Attention from you lifts its spirits and wakes it a little.
        self.mood.mood = min(1.0, self.mood.mood + 0.18)
        self.mood.energy = min(1.0, self.mood.energy + 0.06)
        self.mood.attention = min(1.0, self.mood.attention + 0.25)
        self.mood.sleepiness = max(0.0, self.mood.sleepiness - 0.10)
        # A direct touch from you always lands, even mid-spell or mid-nap.
        self._set_action(pose, seconds=secs, message=line, force=True)
        self._save_position()

    # ---------- interaction ----------
    def enterEvent(self, event) -> None:  # type: ignore[override]
        if time.time() - self.last_hover > 8:
            self.hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.hover_timer.stop()
        super().leaveEvent(event)

    def _hover_ready(self) -> None:
        self.last_hover = time.time()
        self._set_action("curious", seconds=1.3, message=None)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self.walking = False
        self.walk_target_x = None
        if event.button() == Qt.MouseButton.LeftButton:
            # cancel any in-flight fall and grab the pet
            self.falling = False
            self.phys_timer.stop()
            self.fall_vx = self.fall_vy = 0.0
            self.bounces = 0
            self.dragging = True
            self.drag_started = False
            self.held = True
            gp = event.globalPosition().toPoint()
            self.drag_start_global = gp
            self._prev_drag_gp = gp
            self._drag_window_origin = self.pos()
            self._throw_vx = self._throw_vy = 0.0
            now = time.perf_counter()
            self._drag_history = [(now, gp.x(), gp.y())]
            self._shake_sign = 0
            self._shake_count = 0
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self.tray.contextMenu().popup(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self.dragging:
            gp = event.globalPosition().toPoint()
            total = gp - self.drag_start_global
            if abs(total.x()) > 3 or abs(total.y()) > 3:
                self.drag_started = True
            # instantaneous velocity drives the throw on release
            step = gp - self._prev_drag_gp
            self._throw_vx = float(step.x())
            self._throw_vy = float(step.y())
            self._prev_drag_gp = gp
            now = time.perf_counter()
            self._drag_history.append((now, gp.x(), gp.y()))
            cutoff = now - 0.12
            self._drag_history = [sample for sample in self._drag_history if sample[0] >= cutoff]
            # count rapid left/right reversals -> a "shake"
            if abs(step.x()) > 5:
                s = 1 if step.x() > 0 else -1
                if self._shake_sign and s != self._shake_sign:
                    self._shake_count += 1
                self._shake_sign = s

            ag = self._window_screen_available()
            min_x, max_x = self._x_bounds()
            top = ag.top() + 8
            rest = self._rest_y()
            new_x = max(min_x, min(self._drag_window_origin.x() + total.x(), max_x))
            new_y = max(top, min(self._drag_window_origin.y() + total.y(), rest))
            self.move(new_x, new_y)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.dragging:
            self.dragging = False
            self.held = False
            shaken = self._shake_count >= 6
            self._shake_count = 0
            self._shake_sign = 0
            if len(self._drag_history) >= 2:
                first = self._drag_history[0]
                last = self._drag_history[-1]
                elapsed = max(0.001, last[0] - first[0])
                self._throw_vx = (last[1] - first[1]) / elapsed * 0.016
                self._throw_vy = (last[2] - first[2]) / elapsed * 0.016
            self._drag_history.clear()
            if not self.drag_started:
                self._on_pet()  # a tap, not a drag -> head pat
            elif self.y() < self._rest_y() - 2:
                # released in the air -> throw it back down with momentum
                clamp = lambda v: max(-self.MAX_THROW, min(self.MAX_THROW, v))
                self.fall_vx = clamp(self._throw_vx * self.THROW_SCALE)
                self.fall_vy = clamp(self._throw_vy * self.THROW_SCALE)
                self.bounces = 0
                self._fall_peak = 0.0
                self._shaken = shaken    # land dizzy if it was being jiggled
                self.falling = True
                self._last_phys_t = time.perf_counter()
                self.phys_timer.start()
            elif shaken:
                # jiggled in place -> dizzy, no fall needed
                self.move(self.x(), self._rest_y())
                self.mood.energy = min(1.0, self.mood.energy + 0.05)
                self._set_action("dizzy", 1.8, random.choice(("唔哇…", "转晕了…")))
                self._save_position()
                self._was_parked = self._is_parked()
            else:
                # dropped right at the floor -> just settle (and notice if you've
                # tucked it into a corner -> it nestles in to stay)
                self.move(self.x(), self._rest_y())
                self._save_position()
                self._check_park_transition()
                self.last_idle_action = time.time()
            self.update()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.collapsed = not self.collapsed
            if "poof" in self.sprite_images:
                # a little puff of smoke bookends the hide/return -> "啵" it vanishes
                self._set_action("poof", 0.7, force=True)
            else:
                self._set_action("peek" if self.collapsed else "idle", 1.4)
            self.say("我躲一下下。" if self.collapsed else "我回来啦。", 1.6)
        super().mouseDoubleClickEvent(event)

    # ---------- tray/menu ----------
    def _toggle_enabled(self, checked: bool) -> None:
        self.settings.enabled = checked
        self.settings.save()
        self.monitor.set_active(checked)
        if checked:
            self.show()
            self._snap_to_taskbar(initial=False)
        else:
            self.hide()

    def _toggle_top(self, checked: bool) -> None:
        self.settings.always_on_top = checked
        self.settings.save()
        self.hide()
        self._configure_window()
        if self.settings.enabled:
            self.show()

    def _set_activity(self, level: str) -> None:
        self.settings.activity = level
        self.settings.save()
        self.act_lively.setChecked(level == "high")
        self.act_calm.setChecked(level == "low")
        if level == "low":
            self.walking = False
            self.walk_target_x = None
            self.update()
            self.say("好，我安静待着。", 1.8, force=True)
        else:
            self.say("嗯，我活动活动～", 1.6)

    def _set_size_mode(self, mode: str) -> None:
        self.settings.size_mode = mode
        self.act_small.setChecked(mode != "standard")
        self.act_standard.setChecked(mode == "standard")
        self._apply_size(persist=True)

    def _toggle_debug_bounds(self, checked: bool) -> None:
        self.debug_bounds = checked
        self.update()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.act_enabled.setChecked(not self.settings.enabled)
            self._toggle_enabled(not self.settings.enabled)

    def activate_from_second_instance(self) -> None:
        if not self.settings.enabled:
            self.act_enabled.setChecked(True)
            self._toggle_enabled(True)
        else:
            self.show()
            self.raise_()
        self._set_action("wave", 1.2, "我一直在这儿呀。")

    def _quit(self) -> None:
        self._save_state()
        self.monitor.shutdown()
        self.tray.hide()
        QApplication.quit()


# Keep the old imported class name in main.py working without changing callers.
PixelPetWindow = SpritePetWindow
