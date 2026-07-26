from __future__ import annotations

import ctypes
import html
import logging
import math
import os
import random
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEvent,
    QUrl,
    Qt,
    QTimer,
    QPoint,
    QPointF,
    QRect,
    QSize,
    QStandardPaths,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygon,
    QRegion,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QApplication,
)

from .logging_setup import close_logging
from .moon_phase import MoonPhase, calculate_moon_phase
from .paths import DATA_DIR, clear_known_local_data
from .monitor import SystemMonitor, Telemetry, machine_load
from .settings import Settings
from .state import PetState
from .version import APP_NAME, APP_VERSION, PROJECT_URL
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

    The rendering model keeps a 96x96 sprite inside a padded 144x144 action
    stage instead of sizing the window around each pose. This is the structural
    fix for head clipping. A jump/hover may move inside the padded
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
    STAGE_CACHE_LIMIT = 96   # cap standard-size pixmaps at roughly 32 MiB
    MAX_FOCUS_SECONDS = 90 * 60

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
        "look_side_flip": "look_side_flip",
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

    def __init__(
        self,
        settings: Settings,
        root: Path,
        *,
        state_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._settings_dirty = False
        self._settings_save_failed = False
        self._state_save_failed = False
        self.root = root
        self._state_path = state_path
        self._shutdown_done = False
        self._discard_data_on_shutdown = False
        self._runtime_active = settings.enabled
        self.assets_dir = root / "assets" / "moonshell"

        self.sprite_images: dict[str, QImage] = {}
        self._stage_cache: OrderedDict[
            tuple[str, int, int, int], QPixmap
        ] = OrderedDict()
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
        self.last_idle_action = time.monotonic()
        self._next_idle_gap = random.uniform(4.0, 9.0)
        self.last_hover = 0.0
        self.collapsed = False
        self.dragging = False
        self.drag_started = False
        self.drag_start_global = QPoint()
        self.debug_bounds = False
        self._last_bubble_rect: Optional[QRect] = None
        self._last_render_sig: Optional[tuple] = None
        self._extents_cache: dict[str, Optional[tuple[int, int, int, int]]] = {}
        self._input_mask_signature: Optional[tuple] = None

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
        self._last_phys_t = time.monotonic()

        # cursor attentiveness + petting
        self._cursor_was_near = False
        self._last_glance = 0.0
        self._cursor_speed = 0.0       # px/s, how fast the cursor moved last tick
        self._last_pet_t = 0.0
        self._pet_count = 0
        self._gift_day = -1            # tm_yday of the last "first pat" gift
        self._teleport_target: Optional[int] = None

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
        self._last_brain_t = time.monotonic()
        self._react_last: dict[str, float] = {}
        self._slept_tonight = False
        self._greeted_morning_day = -1
        self._dusk_day = -1
        self._next_moon_phase_check = 0.0

        # ----- cross-session continuity: carry mood forward, remember you -----
        self._state = (
            PetState.load(self._state_path)
            if self._state_path is not None
            else PetState.load()
        )
        self._state_needs_initial_save = False
        self._gift_clock_guarded_day = ""
        self._gift_clock_notice_shown = False
        self._gift_save_failed = False
        today_key = time.strftime("%Y-%m-%d")
        if not self._state.first_seen_date:
            self._state.first_seen_date = today_key
            self._state_needs_initial_save = True
        elif self._state.first_seen_date > today_key:
            # A temporary future system clock must not leave companionship
            # metadata permanently ahead of the user's real calendar.
            self._state.first_seen_date = today_key
            self._state_needs_initial_save = True
        if self._state.last_gift_date > today_key:
            # Guard today against a duplicate, but repair the poisoned future
            # marker so tomorrow's legitimate gift becomes claimable again.
            self._state.last_gift_date = today_key
            self._gift_clock_guarded_day = today_key
            self._state_needs_initial_save = True
        if self._state.normalize_focus_today():
            self._state_needs_initial_save = True

        focus_remaining = self._state.focus_until - time.time()
        if 0.0 < focus_remaining <= self.MAX_FOCUS_SECONDS:
            self._focus_deadline = time.monotonic() + focus_remaining
            if self._state.focus_planned_minutes <= 0:
                # Old state files only stored the deadline. Preserve a sensible
                # completion duration when migrating an in-progress session.
                self._state.focus_planned_minutes = max(
                    1,
                    min(
                        90,
                        int(math.ceil(focus_remaining / 60.0)),
                    ),
                )
                self._state_needs_initial_save = True
        else:
            self._focus_deadline = 0.0
            if (
                self._state.focus_until
                or self._state.focus_planned_minutes
            ):
                self._state.focus_until = 0.0
                self._state.focus_planned_minutes = 0
                self._state_needs_initial_save = True
        self._focus_completed_pending = False

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
        if self._state.last_gift_date == today_key:
            self._gift_day = time.localtime().tm_yday
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
        self._vram_stale_samples = 0

        self.profile = self._profile_for_mode(self.settings.size_mode)

        self._configure_window()
        self._apply_size(persist=False)
        self._build_tray()
        self._exit_for_missing_tray = (
            not self._tray_available and not self._runtime_active
        )
        if self._exit_for_missing_tray:
            # "Disabled" is an explicit durable choice. If Explorer offers no
            # tray entry, preserve that choice and leave cleanly instead of
            # silently turning the companion back on or becoming unreachable.
            logger.warning(
                "System tray unavailable while companion is disabled; exiting"
            )

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(140)
        self.anim_timer.timeout.connect(self._on_anim)
        if self._runtime_active:
            self.anim_timer.start()

        # Smooth 60fps loop, only running while the pet is airborne.
        self.phys_timer = QTimer(self)
        self.phys_timer.setInterval(16)
        self.phys_timer.timeout.connect(self._on_physics)

        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(900)
        self.hover_timer.timeout.connect(self._hover_ready)

        self._teleport_timer = QTimer(self)
        self._teleport_timer.setSingleShot(True)
        self._teleport_timer.setInterval(520)
        self._teleport_timer.timeout.connect(self._finish_teleport)

        self._mask_timer = QTimer(self)
        self._mask_timer.setSingleShot(True)
        self._mask_timer.setInterval(0)
        self._mask_timer.timeout.connect(self._update_input_mask)

        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._complete_focus)

        self._focus_status_timer = QTimer(self)
        self._focus_status_timer.setInterval(30000)
        self._focus_status_timer.timeout.connect(self._refresh_tray_status)

        # Hidden windows keep one very low-frequency safety check alive. If
        # Explorer's tray disappears after the pet was hidden, recall it before
        # it can become an unreachable background process.
        self._tray_watchdog = QTimer(self)
        self._tray_watchdog.setInterval(5000)
        self._tray_watchdog.timeout.connect(self._check_hidden_tray)
        self._tray_missing_checks = 0
        if not self._runtime_active:
            self._tray_watchdog.start()

        # Persist mood + "last seen" so it survives restarts (crash-safe autosave).
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(60000)
        self.state_timer.timeout.connect(self._save_state)
        if self._runtime_active:
            self.state_timer.start()

        # Resolution/DPI changes arrive as a burst of signals. A restartable
        # single-shot timer performs one final revalidation after the burst.
        self._display_timer = QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.setInterval(200)
        self._display_timer.timeout.connect(self._revalidate_position)

        self.monitor = SystemMonitor(
            self,
            active=self._runtime_active and self.settings.system_awareness,
        )
        self.monitor.telemetry.connect(self._on_telemetry)

        clip = QApplication.clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._on_clipboard)
        self._last_clip_react = 0.0

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

        self._connect_screen_signals()
        self._arm_focus_timers()

        self._snap_to_taskbar(initial=True)
        if self._runtime_active:
            self.show()
        else:
            self.hide()

        pose, line = self._startup_greeting()
        greeting_seconds = 3.6 if self._startup_absence < 0 else 1.8
        restoring_focus = self._focus_active
        self._set_action(pose, greeting_seconds, force=restoring_focus)
        self.say(
            line,
            duration=max(2.0, min(3.8, greeting_seconds + 0.4)),
            force=restoring_focus,
        )
        self._was_parked = self._is_parked()
        self._refresh_tray_status()
        if self._state_needs_initial_save:
            self._save_state()
        if self._exit_for_missing_tray:
            QTimer.singleShot(0, self._resolve_disabled_startup_without_tray)
        elif not self._runtime_active:
            QTimer.singleShot(0, self._notify_disabled_startup)

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
        if "look_side" in self.sprite_images:
            self.sprite_images["look_side_flip"] = self._flip_h(self.sprite_images["look_side"])

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
        """Use the canonical body pose for edge clamps and bubble placement.

        Effect poses intentionally reach toward the canvas edges. Including them
        here makes the ordinary idle body float away from the screen edge.
        """
        idle = self.sprite_images.get("idle")
        idle_ext = self._alpha_extents(idle) if idle is not None else None
        if idle_ext is not None:
            left, top, right, bottom = idle_ext
            self._content_left = left
            self._content_right = self.SPRITE_SIZE - 1 - right
            self._content_top = top

            # The gap below idle's feet is transparent stage padding.
            foot_edge = bottom + 1
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
        self.setWindowTitle(APP_NAME)
        self.setAccessibleName("月壳游灵桌面陪伴")
        self.setAccessibleDescription(
            "可点击摸头、拖动位置，并通过右键或通知区域菜单操作的桌面月灵"
        )
        # setWindowFlags recreates the native window and drops its mask.
        self._input_mask_signature = None
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
        self._input_mask_signature = None
        if persist:
            self._snap_to_taskbar(initial=False)
        self.update()

    def _current_dpr(self) -> float:
        screen = self._window_screen()
        if screen is None:
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        return self._screen_dpr(screen)

    @staticmethod
    def _screen_dpr(screen) -> float:
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
        src_y_offset = max(self.MAX_UP_OFFSET, min(self.MAX_DOWN_OFFSET, src_y_offset))
        dpr_key = int(round(dpr * 1000))
        key = (name, dpr_key, src_x_offset, src_y_offset)
        cached = self._stage_cache.get(key)
        if cached is not None:
            self._stage_cache.move_to_end(key)
            return cached

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
        if len(self._stage_cache) > self.STAGE_CACHE_LIMIT:
            self._stage_cache.popitem(last=False)
        return pix

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self._tray_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        icon_path = self.root / "assets" / "branding" / "moonshell.ico"
        self._app_icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        if self._app_icon.isNull():
            self._app_icon = self._make_app_icon()
        self.tray.setIcon(self._app_icon)
        self.setWindowIcon(self._app_icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(self._app_icon)
        self.tray.setToolTip("月壳游灵")

        # QSystemTrayIcon does not own its context menu. Parent it explicitly so
        # repeated window creation (tests/restarts) cannot leave native menus alive.
        menu = QMenu(self)
        self.tray_menu = menu

        self.status_action = QAction("", self)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        self.memory_action = QAction("", self)
        self.memory_action.setEnabled(False)
        menu.addAction(self.memory_action)
        self.act_companion_journal = QAction("陪伴手账", self)
        self.act_companion_journal.triggered.connect(
            self._show_companion_journal
        )
        menu.addAction(self.act_companion_journal)
        self.act_today_gift = QAction("查看今天的月光", self)
        self.act_today_gift.triggered.connect(self._show_today_gift)
        menu.addAction(self.act_today_gift)
        menu.addSeparator()

        self.act_enabled = QAction("显示桌面月灵（重启后保持）", self, checkable=True)
        self.act_enabled.setChecked(self.settings.enabled)
        self.act_enabled.triggered.connect(self._toggle_enabled)
        menu.addAction(self.act_enabled)

        self.act_visibility = QAction("", self)
        self.act_visibility.triggered.connect(self._toggle_visibility)
        menu.addAction(self.act_visibility)

        self.act_top = QAction("始终置顶", self, checkable=True)
        self.act_top.setChecked(self.settings.always_on_top)
        self.act_top.triggered.connect(self._toggle_top)
        menu.addAction(self.act_top)

        # Activity dial: merges the old "自由活动" + "安静模式" toggles into one
        # high/low intensity.  high = strolls + talks + livelier; low = stays put,
        # quiet, calmer.
        act_menu = menu.addMenu("活动强度")
        self.activity_menu = act_menu
        self.act_lively = QAction("活泼 · 会散步和说话", self, checkable=True)
        self.act_calm = QAction("沉静 · 原地少打扰", self, checkable=True)
        self.activity_group = QActionGroup(self)
        self.activity_group.setExclusive(True)
        self.activity_group.addAction(self.act_lively)
        self.activity_group.addAction(self.act_calm)
        self.act_lively.setChecked(self.settings.activity == "high")
        self.act_calm.setChecked(self.settings.activity == "low")
        self.act_lively.triggered.connect(
            lambda checked: checked and self._set_activity("high")
        )
        self.act_calm.triggered.connect(
            lambda checked: checked and self._set_activity("low")
        )
        act_menu.addAction(self.act_lively)
        act_menu.addAction(self.act_calm)

        focus_menu = menu.addMenu("专注陪伴")
        self.focus_menu = focus_menu
        self.act_focus_25 = QAction("专注 25 分钟", self)
        self.act_focus_50 = QAction("专注 50 分钟", self)
        self.act_focus_90 = QAction("专注 90 分钟", self)
        self.act_focus_end = QAction("结束专注", self)
        self.act_focus_25.triggered.connect(lambda: self._start_focus(25))
        self.act_focus_50.triggered.connect(lambda: self._start_focus(50))
        self.act_focus_90.triggered.connect(lambda: self._start_focus(90))
        self.act_focus_end.triggered.connect(self._cancel_focus)
        focus_menu.addAction(self.act_focus_25)
        focus_menu.addAction(self.act_focus_50)
        focus_menu.addAction(self.act_focus_90)
        focus_menu.addSeparator()
        focus_menu.addAction(self.act_focus_end)

        size_menu = menu.addMenu("尺寸")
        self.size_menu = size_menu
        self.act_small = QAction("紧凑尺寸", self, checkable=True)
        self.act_standard = QAction("标准尺寸", self, checkable=True)
        self.size_group = QActionGroup(self)
        self.size_group.setExclusive(True)
        self.size_group.addAction(self.act_small)
        self.size_group.addAction(self.act_standard)
        self.act_small.setChecked(self.settings.size_mode != "standard")
        self.act_standard.setChecked(self.settings.size_mode == "standard")
        self.act_small.triggered.connect(
            lambda checked: checked and self._set_size_mode("small")
        )
        self.act_standard.triggered.connect(
            lambda checked: checked and self._set_size_mode("standard")
        )
        size_menu.addAction(self.act_small)
        size_menu.addAction(self.act_standard)

        awareness_menu = menu.addMenu("感知与隐私")
        self.awareness_menu = awareness_menu
        self.act_system_awareness = QAction("感知设备忙闲", self, checkable=True)
        self.act_system_awareness.setChecked(self.settings.system_awareness)
        self.act_system_awareness.triggered.connect(self._toggle_system_awareness)
        awareness_menu.addAction(self.act_system_awareness)

        self.act_clipboard = QAction("回应复制动作", self, checkable=True)
        self.act_clipboard.setChecked(self.settings.clipboard_reactions)
        self.act_clipboard.triggered.connect(self._toggle_clipboard_reactions)
        awareness_menu.addAction(self.act_clipboard)
        awareness_menu.addSeparator()
        self.act_privacy_details = QAction("查看完整隐私说明…", self)
        self.act_privacy_details.triggered.connect(self._show_about)
        awareness_menu.addAction(self.act_privacy_details)

        menu.addSeparator()
        self.act_help = QAction("快速使用提示", self)
        self.act_help.triggered.connect(self._show_usage_tip)
        menu.addAction(self.act_help)

        self.act_about = QAction("使用与隐私 · 关于", self)
        self.act_about.triggered.connect(self._show_about)
        menu.addAction(self.act_about)

        self.act_recall = QAction("唤回到主屏幕", self)
        self.act_recall.triggered.connect(self._recall_pet)
        menu.addAction(self.act_recall)

        if os.environ.get("MOONSHELL_DEBUG") == "1":
            advanced_menu = menu.addMenu("开发者选项")
            debug = QAction("显示调试边界", self, checkable=True)
            debug.setChecked(self.debug_bounds)
            debug.triggered.connect(self._toggle_debug_bounds)
            advanced_menu.addAction(debug)

        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        menu.aboutToShow.connect(self._refresh_tray_status)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        # Qt will automatically add a visible icon if Explorer's tray appears
        # later, so show it even when the initial availability probe is false.
        self.tray.show()
        if not self._tray_available:
            logger.warning("System tray is not currently available")

    def _make_app_icon(self) -> QIcon:
        """Build a crisp multi-size icon from the visible idle character.

        Cropping the transparent 96px canvas first makes the pet legible in the
        16px Windows tray instead of shrinking it into a tiny gold dot.
        """
        image = self.sprite_images["idle"]
        extents = self._alpha_extents(image)
        if extents is None:
            return QIcon(QPixmap.fromImage(image))
        left, top, right, bottom = extents
        crop = image.copy(left, top, right - left + 1, bottom - top + 1)

        icon = QIcon()
        for size in (16, 20, 24, 32, 48, 64):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            target = max(1, size - 2)
            scaled = crop.scaled(
                QSize(target, target),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawImage(
                QPoint((size - scaled.width()) // 2, (size - scaled.height()) // 2),
                scaled,
            )
            painter.end()
            icon.addPixmap(pixmap)
        return icon

    # ---------- positioning ----------
    def _screen_by_name(self, name: Optional[str]):
        if not name:
            return None
        for screen in QApplication.screens():
            if screen.name() == name:
                return screen
        return None

    def _window_screen(self):
        # Geometry is authoritative while crossing between monitors. The native
        # windowHandle().screen() signal can lag one mouse-move behind, which used
        # to clamp a drag to the monitor it started on.
        screen = QApplication.screenAt(self.geometry().center())
        if screen is not None:
            return screen
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else None
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen

    def _window_screen_available(self) -> QRect:
        screen = self._window_screen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1040)

    def _rest_y(self, ag: Optional[QRect] = None) -> int:
        """Window-top Y where the pet stands on the taskbar, on its own screen."""
        ag = ag or self._window_screen_available()
        y = (ag.bottom() + 1) - self.profile.ground_y
        return max(ag.top() + 8, min(y, ag.bottom() - self.height() + self.profile.bottom_margin))

    def _sprite_insets(self, dpr: Optional[float] = None) -> tuple[float, float]:
        """Logical px from each window edge to the visible character's edges."""
        dpr = self._current_dpr() if dpr is None else max(1.0, dpr)
        stage_size = self._stage_logical_size(dpr)
        stage_x = (self.width() - stage_size) / 2.0
        scale = stage_size / self.STAGE_SIZE
        left_inset = stage_x + (self.SPRITE_X + self._content_left) * scale
        right_edge = self.SPRITE_X + (self.SPRITE_SIZE - self._content_right)
        right_inset = self.width() - (stage_x + right_edge * scale)
        return left_inset, right_inset

    def _x_bounds(
        self,
        ag: Optional[QRect] = None,
        dpr: Optional[float] = None,
    ) -> tuple[int, int]:
        # Clamp the *visible* character to the screen edge, letting the window's
        # transparent padding hang off-screen.  Otherwise that padding acts as an
        # invisible wall that stops the pet well short of the left/right edges.
        ag = ag or self._window_screen_available()
        left_inset, right_inset = self._sprite_insets(dpr)
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
        if screen is None:
            screen = self._window_screen()
        ag = screen.availableGeometry() if screen is not None else self._window_screen_available()
        min_x, max_x = self._x_bounds(ag, self._screen_dpr(screen))
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

    def _save_position(self, *, persist: bool = True) -> None:
        min_x, max_x = self._x_bounds()
        new_x = self.x()
        new_ratio = (
            (self.x() - min_x) / (max_x - min_x) if max_x > min_x else 0.5
        )
        screen = self._window_screen()
        new_screen_name = screen.name() if screen is not None else None
        changed = (
            self.settings.x != new_x
            or self.settings.x_ratio != new_ratio
            or self.settings.screen_name != new_screen_name
        )
        self.settings.x = new_x
        self.settings.x_ratio = new_ratio
        self.settings.screen_name = new_screen_name
        if changed:
            self._settings_dirty = True
        if persist and self._settings_dirty:
            self._persist_settings()

    def _notify_persistence_failure(self, message: str) -> None:
        if self._shutdown_done:
            return
        if self._runtime_active and self.isVisible():
            self._set_action("curious", 3.4, force=True)
            self.say(message, 4.6, force=True)
        elif hasattr(self, "tray") and self._probe_tray_available():
            try:
                self.tray.showMessage(
                    "MoonShell 数据未保存",
                    message,
                    QSystemTrayIcon.MessageIcon.Warning,
                    7000,
                )
            except Exception:
                pass
        self._refresh_tray_status()

    def _persist_settings(self, *, notify_failure: bool = True) -> bool:
        """Keep a transient profile error from breaking a UI event or shutdown."""
        was_failed = self._settings_save_failed
        try:
            self.settings.save()
            self._settings_dirty = False
            self._settings_save_failed = False
            if was_failed:
                self._refresh_tray_status()
            return True
        except Exception as exc:
            self._settings_dirty = True
            self._settings_save_failed = True
            logger.warning("Could not save settings: %s", exc)
            if notify_failure and not was_failed:
                self._notify_persistence_failure(
                    "设置仅在本次运行中生效，未能保存到本地。"
                )
            return False

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
        self._display_timer.start()

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
        self._update_input_mask()

    def _render_signature(self) -> tuple:
        """Everything the next paint depends on. If this hasn't changed since the
        last animation tick, repainting would redraw identical pixels."""
        bubble = (
            self.message
            if (self.message and time.monotonic() <= self.message_until)
            else ""
        )
        return (*self._current_render_spec(), bubble, self.debug_bounds)

    def _update_if_dirty(self) -> None:
        """Repaint only when the visible frame actually changed.

        The animation timer ticks ~7x/s for the pet's whole lifetime; during a
        held pose or between breath steps the frame is often identical, and for
        an always-running app those skipped repaints are real CPU saved."""
        sig = self._render_signature()
        if sig != self._last_render_sig:
            self._last_render_sig = sig
            self.update()

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
        now = time.monotonic()
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

        # The curious artwork has three extra transparent-stage pixels below its
        # feet compared with the grounded idle pose; compensate at render time.
        if name == "curious":
            src_y_offset -= 3

        return name, src_x_offset, src_y_offset

    def _current_sprite_name(self) -> str:
        now = time.monotonic()
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
        if 76 <= phase <= 77:
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
        if not self.message or time.monotonic() > self.message_until:
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
        ag = self._window_screen_available()
        min_bx = ag.left() - self.x() + 2
        max_bx = ag.right() + 1 - self.x() - bw - 2
        if min_bx <= max_bx:
            bx = max(min_bx, min(bx, max_bx))

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
        painter.setBrush(QColor(79, 104, 190, 255))
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
        painter.setBrush(QColor(79, 104, 190, 255))
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

    def _pose_extents(self, name: str) -> Optional[tuple[int, int, int, int]]:
        """Alpha bbox of a pose, cached -- the per-pixel scan is pure Python and
        far too slow to repeat on every hit test."""
        if name not in self._extents_cache:
            image = self.sprite_images.get(name)
            self._extents_cache[name] = (
                self._alpha_extents(image) if image is not None else None
            )
        return self._extents_cache[name]

    def _interactive_body_rect(self) -> Optional[QRect]:
        """Logical hit rectangle shared by event filtering and the native mask."""
        dpr = self._current_dpr()
        stage_size = self._stage_logical_size(dpr)
        scale = stage_size / self.STAGE_SIZE
        if scale <= 0:
            return None
        stage_x = (self.width() - stage_size) / 2.0
        stage_y = self.profile.ground_y - stage_size + self._foot_inset * scale
        name, src_x_offset, src_y_offset = self._current_render_spec()
        extents = self._pose_extents(name if name in self.sprite_images else "idle")
        if extents is None:
            return None
        left, top, right, bottom = extents
        # Use the pose's visible bounding box instead of exact alpha pixels.
        # Pixel-level hit testing made holes between limbs click-through, so a
        # drag could fail depending on the exact pixel pressed.
        grab_pad = 4
        hit_left = math.floor(
            stage_x + (self.SPRITE_X + src_x_offset + left - grab_pad) * scale
        )
        hit_top = math.floor(
            stage_y + (self.SPRITE_Y + src_y_offset + top - grab_pad) * scale
        )
        hit_right = math.ceil(
            stage_x
            + (self.SPRITE_X + src_x_offset + right + 1 + grab_pad) * scale
        )
        hit_bottom = math.ceil(
            stage_y
            + (self.SPRITE_Y + src_y_offset + bottom + 1 + grab_pad) * scale
        )
        return QRect(
            hit_left,
            hit_top,
            max(1, hit_right - hit_left),
            max(1, hit_bottom - hit_top),
        ).intersected(self.rect())

    def _active_bubble_hit_rect(self) -> Optional[QRect]:
        if (
            self._last_bubble_rect is None
            or not self.message
            or time.monotonic() > self.message_until
        ):
            return None
        return self._last_bubble_rect.adjusted(-2, -2, 2, 10).intersected(
            self.rect()
        )

    def _is_interactive_point(self, point: QPoint) -> bool:
        """Only the visible pet and speech bubble should consume desktop input."""
        bubble_hit = self._active_bubble_hit_rect()
        if bubble_hit is not None and bubble_hit.contains(point):
            return True
        body_hit = self._interactive_body_rect()
        return body_hit is not None and body_hit.contains(point)

    def _update_input_mask(self) -> None:
        """Keep visible content clickable and transparent padding click-through.

        A permanent QRegion has no cursor-entry race: Windows never routes input
        to padding outside the mask, while the pet body is interactive on the
        very first press. During a grab or throw, retain the full window region
        so native mouse capture cannot be clipped mid-gesture.
        """
        if not self.isVisible():
            return
        bubble_hit = self._active_bubble_hit_rect()
        body_hit = self._interactive_body_rect()
        full_window = self.debug_bounds or self.held or self.dragging or self.falling
        signature = (
            self.size().width(),
            self.size().height(),
            full_window,
            body_hit.getRect() if body_hit is not None else None,
            bubble_hit.getRect() if bubble_hit is not None else None,
        )
        if signature == self._input_mask_signature:
            return

        region = QRegion(self.rect()) if full_window else QRegion()
        if not full_window and body_hit is not None:
            region = region.united(QRegion(body_hit))
        if not full_window and bubble_hit is not None:
            region = region.united(QRegion(bubble_hit))
        if region.isEmpty():
            # Required art should always yield a body rectangle. Fail open for
            # input if a platform reports incomplete geometry, so the pet never
            # becomes visible but unreachable.
            region = QRegion(self.rect())
        try:
            self._input_mask_signature = signature
            self.setMask(region)
        except Exception as exc:
            self._input_mask_signature = None
            logger.debug("Could not update companion input mask: %s", exc)

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
    def _focus_active(self) -> bool:
        return self._focus_deadline > time.monotonic()

    def _focus_remaining_seconds(self) -> float:
        return max(0.0, self._focus_deadline - time.monotonic())

    @property
    def _lively(self) -> bool:
        """High activity: strolls, talks, livelier beats."""
        return self.settings.activity == "high" and not self._focus_active

    @property
    def _quiet(self) -> bool:
        """Low activity: stays put, keeps speech to itself."""
        return self.settings.activity == "low" or self._focus_active

    @property
    def _is_night(self) -> bool:
        """Late night (23:00-05:59): the only window for night-flavored beats --
        riding the crescent moon, fully bedding down to sleep, moon/night lines.
        Single source of truth so these never leak into broad daylight."""
        return self._hour >= 23 or self._hour < 6

    # ---------- focus companion ----------
    def _arm_focus_timers(self) -> None:
        remaining = self._focus_remaining_seconds()
        if remaining <= 0.0:
            self._focus_timer.stop()
            self._focus_status_timer.stop()
            return
        self._focus_timer.start(max(1, int(math.ceil(remaining * 1000.0))))
        if self._runtime_active:
            self._focus_status_timer.start()

    def _start_focus(self, minutes: int) -> None:
        if self._focus_active:
            self.say("这一段还在进行，先陪你完成它。", 3.0, force=True)
            return
        self._focus_completed_pending = False
        minutes = max(1, min(90, int(minutes)))
        settings_saved = True
        if not self.settings.enabled:
            self.settings.enabled = True
            self.act_enabled.setChecked(True)
            settings_saved = self._persist_settings(notify_failure=False)
        self._set_runtime_active(True)

        seconds = minutes * 60
        self._focus_deadline = time.monotonic() + seconds
        self._state.focus_until = time.time() + seconds
        self._state.focus_planned_minutes = minutes
        self.walking = False
        self.walk_target_x = None
        self._teleport_target = None
        self._teleport_timer.stop()
        if self._resource_busy:
            self._leave_resource_state()
        self._busy_samples = self._memory_samples = self._vram_samples = 0
        self._arm_focus_timers()
        state_saved = self._save_state(notify_failure=False)

        pose = "read" if "read" in self.sprite_images else "sit"
        self._set_action(pose, 3.0, force=True)
        if state_saved:
            line = f"好，陪你专注 {minutes} 分钟。"
        else:
            line = "专注已开始，但无法保存；退出后不会继续这次计时。"
        self.say(line, 4.2 if not state_saved else 3.4, force=True)
        self._refresh_tray_status()
        if not settings_saved:
            self._notify_persistence_failure(
                "显示偏好仅在本次运行中生效，未能保存到本地。"
            )

    def _finish_focus(self, *, completed: bool) -> None:
        was_focusing = self._focus_deadline > 0.0 or self._state.focus_until > 0.0
        planned_minutes = self._state.focus_planned_minutes
        if completed and was_focusing:
            if planned_minutes <= 0:
                planned_minutes = max(
                    1,
                    min(
                        90,
                        int(
                            math.ceil(
                                self._focus_remaining_seconds() / 60.0
                            )
                        ),
                    ),
                )
            self._state.record_focus_completion(planned_minutes)
        self._focus_deadline = 0.0
        self._state.focus_until = 0.0
        self._state.focus_planned_minutes = 0
        self._focus_timer.stop()
        self._focus_status_timer.stop()
        if not was_focusing:
            self._refresh_tray_status()
            return

        state_saved = self._save_state(notify_failure=False)
        if completed:
            self._focus_completed_pending = True
            if self._runtime_active:
                pose = "star" if "star" in self.sprite_images else "happy"
                self._set_action(pose, 3.2, force=True)
                line = (
                    f"这一段完成啦，手账记下了 {planned_minutes} 分钟。"
                    if state_saved
                    else "这一段完成啦；但陪伴记录暂时无法保存。"
                )
                self.say(line, 4.2 if not state_saved else 3.6, force=True)
            try:
                if self._probe_tray_available():
                    self.tray.showMessage(
                        "专注结束",
                        (
                            f"手账已记下这次 {planned_minutes} 分钟专注。"
                            if state_saved
                            else "这一段完成啦，但陪伴记录暂时无法保存。"
                        ),
                        QSystemTrayIcon.MessageIcon.Information,
                        5000,
                    )
            except Exception:
                pass
        else:
            self._focus_completed_pending = False
        if not completed and self._runtime_active:
            self._set_action(
                "sit" if "sit" in self.sprite_images else "idle",
                1.8,
                force=True,
            )
            line = (
                "好，专注陪伴结束啦。"
                if state_saved
                else "本次专注已结束，但结束状态暂时无法保存。"
            )
            self.say(line, 4.0 if not state_saved else 2.2, force=True)
        self._refresh_tray_status()

    def _complete_focus(self) -> None:
        self._finish_focus(completed=True)

    def _cancel_focus(self) -> None:
        self._finish_focus(completed=False)

    # ---------- state/actions ----------
    def say(self, msg: str, duration: float = 3.0, force: bool = False) -> None:
        if self._quiet and not force:
            return
        self.message = msg
        self.message_until = time.monotonic() + duration
        self.update()

    def _set_action(
        self,
        action: str,
        seconds: float = 2.2,
        message: Optional[str] = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
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
        # A stop request crosses threads asynchronously. Drop any queued sample
        # after the user disables awareness or temporarily hides the pet.
        if not self._runtime_active or not self.settings.system_awareness:
            return
        age = time.monotonic() - t.captured_at
        if age > 10.0:
            logger.debug("Dropped stale telemetry sample (age %.1fs)", age)
            return
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

        if self._focus_active:
            if self._resource_busy:
                self._leave_resource_state()
            self._busy_samples = self._memory_samples = self._vram_samples = 0
            self._refresh_tray_status()
            return

        # Require several consecutive busy samples, then react deterministically.
        # This stays calm during spikes while making a real sustained load visible.
        # CPU is sampled every 2s. Require four genuinely high readings so a
        # launch spike or foreground app switch does not look like distress.
        if not t.cpu_sampled:
            self._busy_samples = 0
        elif t.cpu >= 85:
            self._busy_samples += 1
        elif t.cpu < 70:
            self._busy_samples = 0
        else:
            self._busy_samples = max(0, self._busy_samples - 1)
        # Memory pressure is only noteworthy near exhaustion, not merely because
        # a large app keeps a healthy working set.
        if not t.mem_sampled:
            self._memory_samples = 0
        elif t.mem >= 96:
            self._memory_samples += 1
        elif t.mem < 92:
            self._memory_samples = 0
        else:
            self._memory_samples = max(0, self._memory_samples - 1)
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
            else:
                self._vram_samples = max(0, self._vram_samples - 1)
        self._update_resource_state(t)
        if self._batt is not None and not self._plugged and self._batt <= 18:
            self._react(
                "sad",
                2.2,
                "battery",
                240,
                lines=("唔…有点没力气了。",),
            )

        self._refresh_tray_status()
        if self._telemetry_samples % 15 == 0:
            logger.debug(
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
        if self._vram_samples >= 2 and t.gpu_sampled:
            candidates.append((3, "vram", "surprised", "呼…有点忙不过来了。"))

        if candidates:
            priority, kind, pose, text = max(candidates)
            if not self._resource_busy or priority > self._resource_alert_priority:
                self._enter_resource_state(kind, pose, text)

        if self._resource_alert_kind == "memory":
            recovered = t.mem < 90
        elif self._resource_alert_kind == "load":
            recovered = t.cpu < 60
        elif self._resource_alert_kind == "vram":
            # Allocated VRAM can remain high after work stops. Recovery is based
            # on fresh compute activity, not whether an app releases its cache.
            # If fresh GPU sampling disappears, don't let stale readings block
            # future reactions forever.
            if t.gpu_sampled:
                self._vram_stale_samples = 0
                recovered = t.gpu is None or t.gpu < 25
            else:
                self._vram_stale_samples += 1
                recovered = self._vram_stale_samples >= 6 and t.cpu < 60
        else:
            recovered = True
        if self._resource_busy and recovered:
            self._resource_recovery_samples += 1
            if self._resource_recovery_samples >= 3:
                self._leave_resource_state()
        else:
            self._resource_recovery_samples = 0

    def _enter_resource_state(self, kind: str, pose: str, text: str) -> None:
        now = time.monotonic()
        self._resource_busy = True
        self._resource_alert_kind = kind
        self._resource_alert_priority = {"memory": 1, "load": 2, "vram": 3}.get(kind, 1)
        self._resource_alert_pose = pose
        self._resource_alert_text = text
        self._resource_alert_until = now + 4.0
        self._resource_recovery_samples = 0
        self._vram_stale_samples = 0
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
        self._vram_stale_samples = 0
        if self._resource_message_active and time.monotonic() <= self.message_until:
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
        if self._focus_active:
            minutes = max(
                1,
                int(math.ceil(self._focus_remaining_seconds() / 60.0)),
            )
            return "read", f"我还在，继续安静陪你专注。还剩约 {minutes} 分钟。"
        if absence < 0:
            return "wave", "初次见面。点点或拖动我；右键或托盘可打开设置与隐私。"
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

    def _save_state(self, *, notify_failure: bool = True) -> bool:
        if self._settings_dirty:
            self._persist_settings()
        self._state.energy = self.mood.energy
        self._state.mood = self.mood.mood
        self._state.sleepiness = self.mood.sleepiness
        was_failed = self._state_save_failed
        if self._state_path is None:
            result = self._state.save()
        else:
            result = self._state.save(self._state_path)
        saved = result is not False
        self._state_save_failed = not saved
        if saved and was_failed:
            self._refresh_tray_status()
        elif not saved and notify_failure and not was_failed:
            self._notify_persistence_failure(
                "陪伴记忆暂时无法保存；重启后可能无法延续。"
            )
        return saved

    def _on_clipboard(self) -> None:
        if (
            not self._runtime_active
            or not self.settings.clipboard_reactions
            or self._focus_active
        ):
            return
        now = time.monotonic()
        if now - self._last_clip_react < 20:
            return
        clip = QApplication.clipboard()
        try:
            # Only inspect the advertised MIME type. The pet reacts to the act of
            # copying and never needs to read or retain clipboard contents.
            mime = clip.mimeData() if clip is not None else None
            if mime is None or not mime.hasText():
                return
        except Exception:
            return
        self._last_clip_react = now
        self.mood.attention = min(1.0, self.mood.attention + 0.15)
        self._react("notify", 0.7, "copy", 20)  # a quiet "noticed that"

    def _update_brain(self, now: float) -> None:
        dt = max(0.0, min(0.5, now - self._last_brain_t))
        self._last_brain_t = now
        if self.frame % 50 == 0:
            # Time-of-day behavior must keep working when hardware awareness is
            # disabled and no telemetry snapshots are arriving.
            self._hour = time.localtime().tm_hour

        cp = QCursor.pos()
        speed = math.hypot(cp.x() - self._cursor_pos.x(),
                           cp.y() - self._cursor_pos.y()) / max(dt, 1e-3)
        self._cursor_pos = cp
        self._cursor_speed = speed
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
        now = time.monotonic()
        if self._focus_active and not force:
            return False
        if self.held or self.dragging or self.falling:
            return False
        if not force and self.walking:
            return False
        if not force and self.action.until > now and self.action.locked:
            return False
        last_reaction = self._react_last.get(kind)
        if last_reaction is not None and now - last_reaction < cooldown:
            return False
        self._react_last[kind] = now
        self.last_idle_action = now
        msg = None
        if lines and not self._quiet and (force or random.random() < 0.4):
            msg = random.choice(lines)
        self._set_action(pose, secs, msg, force=force)
        return True

    def _maybe_moon_phase_reaction(self) -> bool:
        """Play a quiet, once-per-event beat near the four principal phases."""
        if self._quiet:
            return False
        now = time.monotonic()
        if now < self._next_moon_phase_check:
            return False
        phase = calculate_moon_phase()
        # The lunar state changes slowly. Avoid doing even this small
        # calculation on every 140 ms animation frame.
        self._next_moon_phase_check = now + 30 * 60
        event_key = phase.principal_event_key(within_days=0.75)
        if (
            event_key is None
            or event_key == self._state.last_moon_event_key
        ):
            return False
        pose, line = {
            0: (
                "sleepy",
                "今天接近新月。月光藏起来休息了。",
            ),
            2: (
                "hover",
                "今天接近上弦月，月光正慢慢长起来。",
            ),
            4: (
                "moon" if "moon" in self.sprite_images else "star",
                "今天接近满月。抬头的时候，也许会想起我。",
            ),
            6: (
                "sit",
                "今天接近下弦月。月光正慢慢收拢。",
            ),
        }[phase.index]
        if pose not in self.sprite_images:
            pose = "idle"
        reacted = self._react(
            pose,
            2.8,
            "moon_phase",
            6 * 3600,
        )
        if not reacted:
            self._next_moon_phase_check = now + 5 * 60
            return False

        self.say(line, 4.2)
        self._next_moon_phase_check = now + 6 * 3600
        previous_key = self._state.last_moon_event_key
        self._state.last_moon_event_key = event_key
        if not self._save_state(notify_failure=False):
            # Do not consume the event when its marker could not be persisted.
            self._state.last_moon_event_key = previous_key
        return True

    def _maybe_life_reactions(self, now: float) -> bool:
        """The handful of crisp, companion-y moments worth noticing explicitly."""
        if self._focus_active:
            return False
        hour = self._hour
        night = self._is_night

        # you just came back to the keyboard -> a quick peek
        if self._prev_idle_sec > 20 and self._idle_sec < 2.0:
            self.mood.attention = min(1.0, self.mood.attention + 0.3)
            return self._react("peek", 1.3, "return", 12)

        # deep night, settled in -> drifts off (once per night)
        if night and not self._slept_tonight and self.mood.sleepiness > 0.7 and self._idle_sec > 8:
            reacted = self._react(
                "sleep",
                3.6,
                "night",
                300,
                lines=("夜深了…我先眯一会儿。", "困了呢，晚安。"),
            )
            if reacted:
                self._slept_tonight = True
            return reacted
        if not night:
            self._slept_tonight = False

        # morning hello, once a day
        today = time.localtime().tm_yday
        if 6 <= hour < 10 and self._greeted_morning_day != today and self._idle_sec < 30:
            reacted = self._react(
                "wave",
                1.8,
                "morning",
                300,
                lines=("早呀。", "天亮啦。"),
            )
            if reacted:
                self._greeted_morning_day = today
            return reacted

        # dusk wind-down, once a day -- a calm "evening's coming" beat
        if 18 <= hour < 21 and self._dusk_day != today and self._idle_sec < 60:
            reacted = self._react(
                "hover",
                1.8,
                "dusk",
                300,
                lines=("天要黑了呢。", "黄昏了，慢下来吧。"),
            )
            if reacted:
                self._dusk_day = today
            return reacted

        if self._maybe_moon_phase_reaction():
            return True

        # left alone a long time -> nods off
        if self._idle_sec > 100 and self.mood.sleepiness > 0.5:
            return self._react("sleepy", 2.8, "drowsy", 45)

        # been present a long unbroken stretch -> a gentle "stretch" nudge
        if self._active_streak > 50 * 60:
            reacted = self._react(
                "hover",
                2.0,
                "stretch",
                600,
                lines=("坐好久了，伸个懒腰吧。",),
            )
            if reacted:
                self._active_streak = 0.0
            return reacted

        return False

    def _on_anim(self) -> None:
        self.frame += 1
        if not self.isVisible():   # disabled / hidden -> let the brain idle too
            return
        now = time.monotonic()
        self._update_brain(now)
        self._update_input_mask()

        # While picked up, thrown, or actively dragged, the physics loop / mouse
        # owns the position; just keep the dangle animation ticking.
        if self.held or self.falling or self.dragging:
            self._update_if_dirty()
            return

        if self.walking:
            self._step_walk()
            self._update_if_dirty()
            return

        busy = self.action.until > now
        idle_ok = not busy and not self.collapsed and self.isVisible()
        if idle_ok:
            if not self._maybe_life_reactions(now):
                if now - self.last_idle_action > self._next_idle_gap:
                    self._begin_idle_beat(now)
                else:
                    self._maybe_notice_cursor(now)
        self._update_if_dirty()

    def _maybe_notice_cursor(self, now: float) -> None:
        """Perk up when the cursor approaches the sprite itself (not the window)."""
        if self._focus_active:
            return
        cp = QCursor.pos()
        center = self._character_center_global()
        near = math.hypot(cp.x() - center.x(), cp.y() - center.y()) < self.NEAR_RADIUS
        if near and not self._cursor_was_near and now - self._last_glance > 6.0:
            self._last_glance = now
            self.last_idle_action = now  # don't immediately stack another beat
            self.mood.attention = min(1.0, self.mood.attention + 0.2)
            if (self._cursor_speed > 900 and "hide" in self.sprite_images
                    and self.mood.sleepiness < 0.85 and random.random() < 0.7):
                # the cursor rushed straight at it -> a startled duck-down; it
                # peeks back out on its own once the beat passes
                self.mood.energy = min(1.0, self.mood.energy + 0.05)
                line = random.choice(("哇…吓我一跳。", "唔？！")) if random.random() < 0.5 else None
                self._set_action("hide", 1.5, line)
            elif self.mood.sleepiness >= 0.78:
                # too sleepy to perk up -- just a drowsy half-peek, stays settled.
                self._set_action("peek" if "peek" in self.sprite_images else "sleepy", 1.0)
            elif (abs(cp.x() - center.x()) > abs(cp.y() - center.y())
                  and "look_side" in self.sprite_images):
                # cursor coming in from a side -> turn the head and watch it
                self.mood.curiosity = min(1.0, self.mood.curiosity + 0.2)
                self._set_action(self._side_glance_pose(cp.x(), center.x()), seconds=1.2)
            else:
                self.mood.curiosity = min(1.0, self.mood.curiosity + 0.2)
                self._set_action("curious", seconds=1.1)
        self._cursor_was_near = near

    def _character_center_global(self) -> QPoint:
        """Center of the currently visible pose, in global logical coordinates."""
        dpr = self._current_dpr()
        stage_size = self._stage_logical_size(dpr)
        scale = stage_size / self.STAGE_SIZE
        stage_x = (self.width() - stage_size) / 2.0
        stage_y = self.profile.ground_y - stage_size + self._foot_inset * scale
        name, x_offset, y_offset = self._current_render_spec()
        extents = self._pose_extents(name if name in self.sprite_images else "idle")
        if extents is None:
            return self.geometry().center()
        left, top, right, bottom = extents
        local = QPoint(
            round(
                stage_x
                + (self.SPRITE_X + x_offset + (left + right + 1) / 2.0) * scale
            ),
            round(
                stage_y
                + (self.SPRITE_Y + y_offset + (top + bottom + 1) / 2.0) * scale
            ),
        )
        return self.mapToGlobal(local)

    def _side_glance_pose(self, cursor_x: int, center_x: int) -> str:
        if cursor_x < center_x and "look_side_flip" in self.sprite_images:
            return "look_side_flip"
        return "look_side"

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

        if self._focus_active:
            # A deliberately narrow, low-motion whitelist: it remains present
            # without turning the focus timer into another source of distraction.
            focus_pool = [
                (name, weight)
                for name, weight in (
                    ("read", 3.0),
                    ("write", 1.4),
                    ("sit", 1.8),
                    ("blink", 0.7),
                )
                if name in self.sprite_images
            ]
            kind = random.choices(
                [name for name, _ in focus_pool],
                [weight for _, weight in focus_pool],
            )[0]
            self._set_action(
                kind,
                seconds=0.24 if kind == "blink" else random.uniform(2.4, 4.0),
            )
            self._next_idle_gap = random.uniform(20.0, 45.0)
            return

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
            secs = 0.24 if kind == "blink" else random.uniform(3.0, 5.0)
            self._set_action(kind, seconds=secs)
            self._next_idle_gap = random.uniform(6.0, 12.0)
            return

        drowsy = m.sleepiness >= 0.55

        # Strolling needs real liveliness; a drowsy or parked pet stays put.  When
        # you nestle it into a screen corner it settles there and won't wander off
        # across your work -- low activity does the same globally.
        can_wander = self._lively and not self._is_parked()
        if not drowsy and can_wander:
            # Cursor attention is expressed through glances, not locomotion.  If
            # movement targets the cursor, the pet appears to chase horizontal
            # mouse motion instead of living on the desktop.
            #
            # Rarely -- a bit more often in high spirits or at night -- it skips
            # walking altogether and blinks across the taskbar in a swirl of
            # light.  Kept scarce so the spell stays a small delight.
            tele_p = 0.05 + 0.10 * m.mood * m.energy + (0.08 if night else 0.0)
            if random.random() < tele_p and self._start_teleport():
                self._next_idle_gap = random.uniform(10.0, 18.0)
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
            secs = 0.24
        elif kind in ("sleep", "sleepy", "yawn"):
            secs = random.uniform(2.2, 3.6)
        elif kind in ("read", "write", "magic", "twirl", "moon", "flame",
                      "star"):
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
        min_x, max_x = self._wander_x_bounds()
        if max_x <= min_x:
            return False
        target = random.randint(min_x, max_x)
        if abs(target - self.x()) < 48:  # too short to be worth animating
            return False
        self.walk_target_x = target
        self.walk_dir = 1 if target > self.x() else -1
        self._walk_dist = 0.0
        self._walk_pos_f = float(self.x())
        # Bursting with energy, a long stroll sometimes becomes an eager little
        # run -- the dash art in play again without ever chasing the cursor.
        self._dashing = (
            "dash" in self.sprite_images
            and self.mood.energy > 0.7
            and abs(target - self.x()) > 150
            and random.random() < 0.35
        )
        self.walking = True
        return True

    def _start_teleport(self) -> bool:
        """Blink across the taskbar in a swirl of light instead of walking.

        A moon spirit doesn't always bother with feet: it vanishes here, and a
        beat later reappears over there.  Rare, and only for hops long enough
        that walking would be a trek."""
        if "teleport" not in self.sprite_images:
            return False
        min_x, max_x = self._wander_x_bounds()
        if max_x <= min_x:
            return False
        target = random.randint(min_x, max_x)
        if abs(target - self.x()) < 140:  # short hop: walking reads better
            return False
        self._teleport_target = target
        self._set_action("teleport", 1.0, force=True)  # vanish beat at the origin
        self._teleport_timer.start()
        return True

    def _finish_teleport(self) -> None:
        target, self._teleport_target = self._teleport_target, None
        if target is None:
            return
        if self.held or self.dragging or self.falling or not self.isVisible():
            return  # grabbed mid-spell -- stay where the hand put us
        min_x, max_x = self._x_bounds()
        self.move(max(min_x, min(target, max_x)), self._rest_y())
        self._save_position(persist=False)
        line = random.choice(("……嗖。", "抄了个近道。")) if random.random() < 0.3 else None
        self._set_action("teleport", 0.8, line, force=True)  # reappearance beat
        self.last_idle_action = time.monotonic()
        self._was_parked = self._is_parked()
        self.update()

    def _start_walk_toward(self, cursor_x: int) -> bool:
        """Amble so the pet ends up roughly under the cursor (clamped on-screen)."""
        min_x, max_x = self._wander_x_bounds()
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

    def _wander_x_bounds(self) -> tuple[int, int]:
        """Keep autonomous movement out of the user-controlled parking zones."""
        min_x, max_x = self._x_bounds()
        return min_x + self.PARK_ZONE + 1, max_x - self.PARK_ZONE - 1

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
        self._save_position(persist=False)
        self._check_park_transition()
        self.last_idle_action = time.monotonic()
        self._next_idle_gap = random.uniform(5.0, 11.0)

    # ---------- pick-up / throw physics ----------
    def _on_physics(self) -> None:
        if not self.falling or not self.isVisible():
            self.falling = False
            self.phys_timer.stop()
            return

        now = time.monotonic()
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
        self.last_idle_action = time.monotonic()
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

    def _award_daily_gift(self) -> tuple[str, str, float] | None:
        """Claim today's moonlight once and return its presentation."""

        today_key = time.strftime("%Y-%m-%d")
        last_gift = self._state.last_gift_date
        if last_gift and today_key < last_gift:
            self._state.last_gift_date = today_key
            self._gift_clock_guarded_day = today_key
            self._gift_clock_notice_shown = False
            self._save_state()
            return None
        if last_gift and today_key == last_gift:
            return None

        self._gift_day = time.localtime().tm_yday
        self._state.last_gift_date = today_key
        self._state.moon_tokens = min(1_000_000, self._state.moon_tokens + 1)
        if self._state.moon_tokens % 7 == 0:
            crystal_number = self._state.moon_tokens // 7
            pose = "crystal" if "crystal" in self.sprite_images else "star"
            line = (
                f"第 {crystal_number} 颗星晶凝成啦！"
                f"月光已经有 {self._state.moon_tokens} 枚。"
            )
        else:
            pose = "gift" if "gift" in self.sprite_images else "happy"
            line = random.choice(("给你留了一枚月光。", "喏，今天的月光。"))
        return pose, line, 2.4

    def _claim_daily_gift(self) -> tuple[str, str, float] | None:
        """Award only when the updated memory was durably written."""

        self._gift_save_failed = False
        previous = (
            self._state.last_gift_date,
            self._state.moon_tokens,
            self._gift_day,
        )
        gift = self._award_daily_gift()
        if gift is None:
            return None
        if self._save_state(notify_failure=False):
            return gift

        (
            self._state.last_gift_date,
            self._state.moon_tokens,
            self._gift_day,
        ) = previous
        self._gift_save_failed = True
        self._refresh_tray_status()
        return None

    def _on_pet(self) -> None:
        """A tap with no drag = a head pat; repeated pats escalate the reaction."""
        had_completed_focus = self._focus_completed_pending
        self._focus_completed_pending = False
        now = time.monotonic()
        if now - self._last_pet_t < 2.5:
            self._pet_count += 1
        else:
            self._pet_count = 1
        self._last_pet_t = now

        secs = 1.1
        gift_awarded = False
        daily_gift = self._claim_daily_gift()
        if daily_gift is not None:
            # First pat of the day: it's been saving a little something for you.
            gift_awarded = True
            pose, line, secs = daily_gift
        elif self._gift_save_failed:
            pose = "curious"
            line = "这枚月光还没存好，暂时没有领取；请检查数据目录权限后重试。"
            secs = 3.6
        elif (
            self._gift_clock_guarded_day == time.strftime("%Y-%m-%d")
            and not self._gift_clock_notice_shown
        ):
            self._gift_clock_notice_shown = True
            pose = "curious"
            line = "检测到系统日期曾在未来；今天不重复发放，明天会恢复。"
            secs = 3.2
        elif self._pet_count >= 6 and "twirl" in self.sprite_images:
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
        if gift_awarded:
            # The claim was saved before it was presented, preventing both a
            # restart duplicate and a false success on a read-only data path.
            self._refresh_tray_status()
        elif had_completed_focus:
            self._refresh_tray_status()

    # ---------- interaction ----------
    def event(self, event) -> bool:  # type: ignore[override]
        if (
            event.type()
            in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate)
            and getattr(self, "dragging", False)
        ):
            self._cancel_drag()
        return super().event(event)

    def _cancel_drag(self) -> None:
        """Recover a consistent state if Windows/Qt takes mouse capture away."""
        self.dragging = False
        self.held = False
        self._drag_history.clear()
        self._shake_count = 0
        self._shake_sign = 0
        self.falling = False
        self.phys_timer.stop()
        if self.isVisible():
            self.move(self.x(), self._rest_y())
            self._save_position()
        self.update()

    def showEvent(self, event) -> None:  # type: ignore[override]
        # Native window recreation drops the region mask and DWM hints.
        self._input_mask_signature = None
        self._disable_dwm_frame()
        super().showEvent(event)
        self._mask_timer.start()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        if time.monotonic() - self.last_hover > 8:
            self.hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.hover_timer.stop()
        super().leaveEvent(event)

    def _hover_ready(self) -> None:
        # Only react when the cursor is actually on the character, not merely
        # inside the window's transparent padding.
        if not self._is_interactive_point(self.mapFromGlobal(QCursor.pos())):
            return
        self.last_hover = time.monotonic()
        self._set_action("curious", seconds=1.3, message=None)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._is_interactive_point(event.position().toPoint()):
            event.ignore()  # click landed on empty padding, not on the pet
            return
        self.walking = False
        self.walk_target_x = None
        if event.button() == Qt.MouseButton.LeftButton:
            # Direct interaction cancels a pending delayed teleport. Otherwise a
            # quick tap could finish before the callback and still blink the pet
            # away from under the user's hand.
            self._teleport_target = None
            self._teleport_timer.stop()
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
            now = time.monotonic()
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
            now = time.monotonic()
            self._drag_history.append((now, gp.x(), gp.y()))
            cutoff = now - 0.12
            self._drag_history = [sample for sample in self._drag_history if sample[0] >= cutoff]
            # count rapid left/right reversals -> a "shake"
            if abs(step.x()) > 5:
                s = 1 if step.x() > 0 else -1
                if self._shake_sign and s != self._shake_sign:
                    self._shake_count += 1
                self._shake_sign = s

            target_screen = QApplication.screenAt(gp) or self._window_screen()
            ag = (
                target_screen.availableGeometry()
                if target_screen is not None
                else self._window_screen_available()
            )
            min_x, max_x = self._x_bounds(ag, self._screen_dpr(target_screen))
            top = ag.top() + 8
            rest = self._rest_y(ag)
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
            released_at = time.monotonic()
            cutoff = released_at - 0.12
            self._drag_history = [
                sample for sample in self._drag_history if sample[0] >= cutoff
            ]
            # A fast move followed by a pause is a placement, not a throw. Do not
            # reuse an old velocity sample when the pointer stopped before release.
            recent_motion = (
                bool(self._drag_history)
                and released_at - self._drag_history[-1][0] <= 0.08
            )
            if recent_motion and len(self._drag_history) >= 2:
                first = self._drag_history[0]
                last = self._drag_history[-1]
                elapsed = max(0.001, last[0] - first[0])
                self._throw_vx = (last[1] - first[1]) / elapsed * 0.016
                self._throw_vy = (last[2] - first[2]) / elapsed * 0.016
            else:
                self._throw_vx = self._throw_vy = 0.0
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
                self._last_phys_t = time.monotonic()
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
                self.last_idle_action = time.monotonic()
            self.update()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._is_interactive_point(event.position().toPoint()):
            event.ignore()
            return
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
    def _notify_disabled_startup(self) -> None:
        if self._shutdown_done or self._runtime_active:
            return
        try:
            if self._probe_tray_available():
                self.tray.showMessage(
                    "MoonShell 当前已停用",
                    "单击此通知或托盘图标即可重新显示月灵。",
                    QSystemTrayIcon.MessageIcon.Information,
                    7000,
                )
        except Exception:
            pass

    def _resolve_disabled_startup_without_tray(self) -> None:
        if self._shutdown_done or self._runtime_active:
            return
        answer = QMessageBox.question(
            self,
            "MoonShell 当前已停用",
            "MoonShell 记住了“停用”选择，但系统托盘目前不可用，"
            "因此没有隐藏入口可以恢复。\n\n现在显示月灵吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._quit()
            return

        self._exit_for_missing_tray = False
        self.settings.enabled = True
        self.act_enabled.setChecked(True)
        settings_saved = self._persist_settings(notify_failure=False)
        self._set_runtime_active(True, snap=True)
        self._set_action("wave", 2.4, force=True)
        self.say("我回来啦。右键月灵仍可打开设置和退出。", 3.8, force=True)
        if not settings_saved:
            self._notify_persistence_failure(
                "显示偏好仅在本次运行中生效，未能保存到本地。"
            )

    def _probe_tray_available(self) -> bool:
        try:
            available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:
            available = self._tray_available
        if available and not self._tray_available:
            self.tray.show()
        self._tray_available = available
        return available

    def _check_hidden_tray(self) -> None:
        if self._runtime_active or self._shutdown_done:
            self._tray_watchdog.stop()
            self._tray_missing_checks = 0
            return
        if self._probe_tray_available():
            self._tray_missing_checks = 0
            return
        self._tray_missing_checks += 1
        if self._tray_missing_checks < 2:
            return
        self._tray_missing_checks = 0
        if not self.settings.enabled:
            logger.warning(
                "Tray disappeared while companion is disabled; exiting cleanly"
            )
            self._quit()
            return
        logger.warning("Tray disappeared while temporarily hidden; recalling the pet")
        self._recall_pet(acknowledge_completion=False)

    def _set_runtime_active(self, active: bool, *, snap: bool = False) -> None:
        """Start/stop visible runtime work without changing the saved preference."""
        self._runtime_active = active
        self.monitor.set_active(active and self.settings.system_awareness)
        if active:
            self._tray_watchdog.stop()
            self._tray_missing_checks = 0
            self._last_brain_t = time.monotonic()
            self._cursor_pos = QCursor.pos()
            if not self.anim_timer.isActive():
                self.anim_timer.start()
            if not self.state_timer.isActive():
                self.state_timer.start()
            if self._focus_active:
                self._arm_focus_timers()
            if snap:
                self._snap_to_taskbar(initial=False)
            self.show()
        else:
            self._tray_missing_checks = 0
            if not self._tray_watchdog.isActive():
                self._tray_watchdog.start()
            self.anim_timer.stop()
            self.state_timer.stop()
            self.hover_timer.stop()
            self.phys_timer.stop()
            self._focus_status_timer.stop()
            self.walking = False
            self.walk_target_x = None
            self._teleport_target = None
            self._teleport_timer.stop()
            self.falling = False
            self.dragging = False
            self.held = False
            self._drag_history.clear()
            self.hide()
        self._refresh_tray_status()

    def _toggle_enabled(self, checked: bool) -> None:
        if not checked and not self._probe_tray_available():
            self.settings.enabled = True
            self.act_enabled.setChecked(True)
            self._set_runtime_active(True)
            self.say("系统托盘暂不可用，请从菜单选择“退出”。", 3.6, force=True)
            return
        self.settings.enabled = checked
        settings_saved = self._persist_settings(notify_failure=False)
        self.act_enabled.setChecked(checked)
        if checked:
            self._set_runtime_active(True, snap=True)
        else:
            self._set_runtime_active(False)
            self._finish_focus(completed=False)
        if not settings_saved:
            self._notify_persistence_failure(
                "显示偏好仅在本次运行中生效，重启后可能恢复原来的状态。"
            )

    def _toggle_visibility(self) -> None:
        if self._runtime_active:
            if not self._probe_tray_available():
                self.say("系统托盘暂不可用，暂时不能隐藏我。", 3.2, force=True)
                return
            self._set_runtime_active(False)
        else:
            self._recall_pet()

    def _recall_pet(self, *, acknowledge_completion: bool = True) -> None:
        """Recover every transient state and place the pet on the primary screen."""
        completed_focus = self._focus_completed_pending
        if acknowledge_completion:
            self._focus_completed_pending = False
        settings_saved = True
        if not self.settings.enabled:
            self.settings.enabled = True
            self.act_enabled.setChecked(True)
            settings_saved = self._persist_settings(notify_failure=False)

        self.collapsed = False
        self.dragging = False
        self.drag_started = False
        self.held = False
        self.falling = False
        self.walking = False
        self.walk_target_x = None
        self._drag_history.clear()
        self._teleport_target = None
        self.phys_timer.stop()
        self._teleport_timer.stop()

        screen = QApplication.primaryScreen()
        if screen is not None:
            ag = screen.availableGeometry()
            min_x, max_x = self._x_bounds(ag, self._screen_dpr(screen))
            self.move(max(min_x, max_x - 50), self._rest_y(ag))
        else:
            self._snap_to_taskbar(initial=False, reset_x=True)

        self._set_runtime_active(True)
        self._save_position()
        self.raise_()
        if completed_focus:
            pose, line = "star", "刚才那一段专注完成啦。"
        elif self._focus_active:
            pose, line = "read", "我在，继续陪你专注。"
        else:
            pose, line = "wave", "我在这里。"
        self._set_action(pose, 1.8, force=True)
        self.say(line, 2.2, force=True)
        if not settings_saved:
            self._notify_persistence_failure(
                "显示偏好仅在本次运行中生效，未能保存到本地。"
            )

    def _show_usage_tip(self) -> None:
        settings_saved = True
        if not self._runtime_active:
            if not self.settings.enabled:
                self.settings.enabled = True
                self.act_enabled.setChecked(True)
                settings_saved = self._persist_settings(notify_failure=False)
            self._set_runtime_active(True, snap=True)
        self._set_action("wave", 3.4, force=True)
        self.say(
            "点点我可以摸头，按住可拖动；右键有设置，点托盘可唤回。",
            5.0,
            force=True,
        )
        if not settings_saved:
            self._notify_persistence_failure(
                "显示偏好仅在本次运行中生效，未能保存到本地。"
            )

    @staticmethod
    def _moon_phase_phrase(phase: MoonPhase) -> str:
        return (
            "月光暂时藏起来休息，新的循环正要开始。"
            if phase.index == 0
            else "一弯细细的月光，正在一点点长大。"
            if phase.index == 1
            else "月光已经走到半途，今晚也在安静地亮着。"
            if phase.index == 2
            else "月面渐渐明亮，像一盏慢慢点亮的小灯。"
            if phase.index == 3
            else "今晚的月光最饱满，适合留下一点共同记忆。"
            if phase.index == 4
            else "月光开始收拢，夜色也跟着柔和下来。"
            if phase.index == 5
            else "月亮走过下弦，今晚的光比前几天安静一些。"
            if phase.index == 6
            else "这一轮月光快要睡着了，休息也算认真生活。"
        )

    def _companion_journal_html(self, phase: MoonPhase) -> str:
        if self._state.normalize_focus_today():
            self._save_state(notify_failure=False)
        local_day = time.localtime()
        date_text = (
            f"{local_day.tm_year}年{local_day.tm_mon}月{local_day.tm_mday}日"
        )
        days = self._state.companionship_days()
        tokens = self._state.moon_tokens
        crystals = tokens // 7
        until_crystal = 7 - (tokens % 7)
        today_minutes = self._state.focus_today_minutes
        sessions = self._state.focus_sessions_completed
        total_minutes = self._state.focus_minutes_completed
        focus_today = f"今日专注 <b>{today_minutes}</b> 分钟。"
        return (
            """
            <style>
              body { line-height: 1.45; }
              h2 { margin: 2px 0 4px 0; }
              h3 { margin: 14px 0 4px 0; }
              p { margin: 4px 0; }
              .muted { color: #707789; }
              .memory {
                background: #f4f1ff;
                border-radius: 8px;
                padding: 8px;
              }
            </style>
            <h2>%s %s</h2>
            <p class="muted">%s · 月龄约 %.1f 天 · 亮面约 %d%%</p>
            <p>%s</p>
            <div class="memory">
              <p>相识第 <b>%d</b> 天</p>
              <p>月光 <b>%d</b> 枚 · 星晶 <b>%d</b> 颗 ·
                 下一颗还差 <b>%d</b> 枚月光</p>
            </div>
            <h3>一起专注过的时间</h3>
            <p>%s</p>
            <p>累计完成 <b>%d</b> 段 · <b>%d</b> 分钟</p>
            <p class="muted">这里不计算连续打卡，也不会因为哪天没打开而扣掉什么。</p>
            <h3>关于月相</h3>
            <p class="muted">月相在本地近似计算，不读取定位、不联网；
               它只负责陪伴氛围，不作为天文观测数据。</p>
            <p class="muted">“保存今日卡片”会在本机生成 PNG；
               卡片会包含日期、相识天数、月光、星晶和今日专注。
               MoonShell 不主动上传；文件是否同步取决于保存位置和系统设置。</p>
            """
            % (
                phase.emoji,
                phase.name,
                date_text,
                phase.age_days,
                round(phase.illumination * 100),
                self._moon_phase_phrase(phase),
                days,
                tokens,
                crystals,
                until_crystal,
                focus_today,
                sessions,
                total_minutes,
            )
        )

    @staticmethod
    def _daily_card_font(pixel_size: int, *, bold: bool = False) -> QFont:
        families = set(QFontDatabase.families())
        family = next(
            (
                candidate
                for candidate in (
                    "Microsoft YaHei UI",
                    "Microsoft YaHei",
                    "DengXian",
                    "SimSun",
                )
                if candidate in families
            ),
            "",
        )
        if not family:
            app = QApplication.instance()
            family = app.font().family() if app is not None else "Sans Serif"
        font = QFont(family)
        font.setPixelSize(pixel_size)
        font.setBold(bold)
        return font

    @staticmethod
    def _draw_daily_card_moon(
        painter: QPainter,
        rect: QRect,
        phase: MoonPhase,
    ) -> None:
        """Draw an eight-phase glyph without depending on an emoji font."""
        light = QColor("#ffd86f")
        shadow = QColor("#161936")
        halo = QColor(255, 216, 111, 35)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(rect.adjusted(-10, -10, 10, 10))

        disc = QPainterPath()
        disc.addEllipse(
            float(rect.x()),
            float(rect.y()),
            float(rect.width()),
            float(rect.height()),
        )
        painter.setClipPath(disc)
        painter.fillPath(disc, QBrush(light))
        painter.setBrush(shadow)
        index = phase.index
        if index == 0:
            painter.fillPath(disc, QBrush(shadow))
        elif index == 1:
            painter.drawEllipse(rect.translated(-rect.width() // 3, 0))
        elif index == 2:
            painter.fillRect(
                QRect(
                    rect.left(),
                    rect.top(),
                    rect.width() // 2,
                    rect.height(),
                ),
                shadow,
            )
        elif index == 3:
            painter.drawEllipse(
                QRect(
                    rect.left() - rect.width() // 4,
                    rect.top(),
                    rect.width() // 2,
                    rect.height(),
                )
            )
        elif index == 5:
            painter.drawEllipse(
                QRect(
                    rect.center().x() + rect.width() // 4,
                    rect.top(),
                    rect.width() // 2,
                    rect.height(),
                )
            )
        elif index == 6:
            painter.fillRect(
                QRect(
                    rect.center().x(),
                    rect.top(),
                    rect.width() // 2 + 1,
                    rect.height(),
                ),
                shadow,
            )
        elif index == 7:
            painter.drawEllipse(rect.translated(rect.width() // 3, 0))
        painter.restore()

        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#ffe9a7"), 3))
        painter.drawEllipse(rect)
        painter.restore()

    def _build_daily_card(self, phase: MoonPhase | None = None) -> QImage:
        """Render a shareable 1080px memory card without network or user data."""
        phase = calculate_moon_phase() if phase is None else phase
        self._state.normalize_focus_today()
        days = self._state.companionship_days()
        tokens = self._state.moon_tokens
        crystals = tokens // 7
        today_minutes = self._state.focus_today_minutes
        local_day = time.localtime()
        date_text = (
            f"{local_day.tm_year}年{local_day.tm_mon}月{local_day.tm_mday}日"
        )

        card = QImage(1080, 1080, QImage.Format.Format_ARGB32)
        background = QLinearGradient(0, 0, 1080, 1080)
        background.setColorAt(0.0, QColor("#090a1b"))
        background.setColorAt(0.55, QColor("#171b46"))
        background.setColorAt(1.0, QColor("#291d52"))
        card.fill(QColor("#090a1b"))
        painter = QPainter(card)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(card.rect(), QBrush(background))

        # Deterministic geometry makes every day's card feel a little different
        # and still changes in headless tests where no system fonts are loaded.
        seed = (
            local_day.tm_year * 10_000
            + local_day.tm_mon * 100
            + local_day.tm_mday
            + days * 17
            + phase.index * 101
        )
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(64):
            x = 24 + ((seed * 37 + index * 149 + index * index * 3) % 1032)
            y = 24 + ((seed * 19 + index * 83 + index * index * 7) % 990)
            radius = 2 + ((seed + index * 11) % 4)
            alpha = 90 + ((seed + index * 29) % 150)
            painter.setBrush(QColor(255, 222, 135, alpha))
            painter.drawEllipse(QPoint(x, y), radius, radius)

        painter.setBrush(QColor(40, 42, 83, 235))
        painter.setPen(QPen(QColor(247, 221, 139, 120), 2))
        painter.drawRoundedRect(QRect(580, 270, 430, 455), 34, 34)

        title_font = self._daily_card_font(54, bold=True)
        painter.setFont(title_font)
        painter.setPen(QColor("#fff3c7"))
        painter.drawText(
            QRect(70, 58, 760, 78),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "今日月灵",
        )

        body_font = self._daily_card_font(29)
        painter.setFont(body_font)
        painter.setPen(QColor("#cdd2ff"))
        painter.drawText(72, 178, date_text)
        self._draw_daily_card_moon(
            painter,
            QRect(840, 76, 54, 54),
            phase,
        )
        painter.drawText(
            QRect(905, 70, 105, 80),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            phase.name,
        )
        painter.setPen(QColor("#989fca"))
        painter.drawText(
            QRect(660, 150, 350, 55),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"亮面约 {round(phase.illumination * 100)}%",
        )

        poses = (
            "sleepy",
            "curious",
            "read",
            "magic",
            "moon",
            "star",
            "sit",
            "sleep",
        )
        pose = poses[phase.index]
        sprite = self.sprite_images.get(
            pose,
            self.sprite_images.get("idle", QImage()),
        )
        if not sprite.isNull():
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform,
                False,
            )
            painter.drawImage(QRect(50, 220, 510, 510), sprite)

        stat_font = self._daily_card_font(34, bold=True)
        painter.setFont(stat_font)
        painter.setPen(QColor("#fff6da"))
        painter.drawText(630, 345, f"相识第 {days} 天")
        painter.drawText(630, 445, f"月光 {tokens} 枚")
        painter.drawText(630, 545, f"星晶 {crystals} 颗")
        painter.drawText(630, 645, f"今日专注 {today_minutes} 分钟")

        # Small geometric memory bars remain legible as decoration and ensure
        # each local value has a visual effect even without a CJK font.
        painter.setPen(Qt.PenStyle.NoPen)
        memory_values = (
            (days, 390, QColor("#ffd56f")),
            (tokens, 490, QColor("#aeb8ff")),
            (crystals, 590, QColor("#f3a8ff")),
            (today_minutes, 690, QColor("#8de1d2")),
        )
        for value, y, color in memory_values:
            width = 24 + min(310, int(math.log1p(max(0, value)) * 66))
            painter.setBrush(QColor(255, 255, 255, 28))
            painter.drawRoundedRect(QRect(630, y, 320, 10), 5, 5)
            painter.setBrush(color)
            painter.drawRoundedRect(QRect(630, y, width, 10), 5, 5)

        quote_font = self._daily_card_font(34)
        painter.setFont(quote_font)
        painter.setPen(QColor("#f4eaff"))
        painter.drawText(
            QRect(90, 795, 900, 95),
            Qt.AlignmentFlag.AlignCenter
            | Qt.TextFlag.TextWordWrap,
            self._moon_phase_phrase(phase),
        )

        footer_font = self._daily_card_font(22)
        painter.setFont(footer_font)
        painter.setPen(QColor("#8f96bd"))
        painter.drawText(
            QRect(70, 995, 940, 38),
            Qt.AlignmentFlag.AlignCenter,
            "MoonShell Spirit · 完全本地生成",
        )
        painter.end()
        return card

    def _save_daily_card(self) -> bool:
        picture_locations = QStandardPaths.standardLocations(
            QStandardPaths.StandardLocation.PicturesLocation
        )
        start_dir = (
            Path(picture_locations[0])
            if picture_locations
            else Path.home()
        )
        default_name = f"MoonShell-{time.strftime('%Y-%m-%d')}.png"
        selected, _selected_filter = QFileDialog.getSaveFileName(
            getattr(self, "_companion_journal_dialog", self),
            "保存今日月灵卡片",
            str(start_dir / default_name),
            "PNG 图片 (*.png)",
        )
        if not selected:
            return False

        target = Path(selected)
        path_was_normalized = target.suffix.lower() != ".png"
        if target.suffix.lower() != ".png":
            # Append instead of replacing an unexpected suffix. Replacing
            # `name.jpg` with `name.png` after the native dialog returns can
            # silently target a different, existing file that the dialog never
            # asked permission to overwrite.
            target = Path(f"{target}.png")
        dialog_parent = getattr(
            self,
            "_companion_journal_dialog",
            self,
        )
        if path_was_normalized and target.exists():
            answer = QMessageBox.question(
                dialog_parent,
                "文件已经存在",
                f"最终 PNG 路径已经存在：\n{target}\n\n要覆盖它吗？",
                (
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                ),
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.moonshell.tmp"
        )
        try:
            image = self._build_daily_card()
            if not image.save(str(temporary), "PNG"):
                raise OSError("Qt could not encode the PNG image")
            os.replace(temporary, target)
        except (OSError, RuntimeError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("Could not save daily MoonShell card: %s", exc)
            QMessageBox.warning(
                dialog_parent,
                "卡片没有保存",
                (
                    f"无法写入所选位置：\n{target}\n\n"
                    "请换一个位置，或检查目标文件夹权限。"
                ),
            )
            return False

        QMessageBox.information(
            dialog_parent,
            "今日卡片已保存",
            f"已保存到：\n{target}",
        )
        return True

    def _show_companion_journal(self) -> None:
        phase = calculate_moon_phase()
        existing = getattr(self, "_companion_journal_dialog", None)
        if existing is not None:
            self._companion_journal_heading.setText(
                f"{phase.emoji} 陪伴手账 · {phase.name}"
            )
            self._companion_journal_details.setHtml(
                self._companion_journal_html(phase)
            )
            self._fit_companion_journal_to_current_work_area(existing)
            existing.show()
            self._fit_companion_journal_to_current_work_area(existing)
            existing.raise_()
            existing.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"陪伴手账 · {APP_NAME}")
        dialog.setWindowIcon(self._app_icon)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        dialog.setAccessibleName("MoonShell 陪伴手账")
        self._companion_journal_dialog = dialog

        layout = QVBoxLayout(dialog)
        heading = QLabel(f"{phase.emoji} 陪伴手账 · {phase.name}", dialog)
        heading.setAccessibleName("当前月相与陪伴手账")
        font = heading.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        self._companion_journal_heading = heading

        details = QTextBrowser(dialog)
        details.setMinimumSize(0, 0)
        details.setAccessibleName(
            "相识天数、月光、星晶与专注完成记录"
        )
        details.setHtml(self._companion_journal_html(phase))
        layout.addWidget(details, 1)
        self._companion_journal_details = details

        save_card_button = QPushButton("保存今日卡片…", dialog)
        save_card_button.setAccessibleName(
            "保存今日卡片到本地 PNG 图片"
        )
        save_card_button.clicked.connect(self._save_daily_card)
        layout.addWidget(save_card_button)
        self._save_card_button = save_card_button

        close_button = QPushButton("合上手账", dialog)
        close_button.setDefault(True)
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button)

        self._fit_companion_journal_to_current_work_area(dialog)
        dialog.show()
        self._fit_companion_journal_to_current_work_area(dialog)
        dialog.raise_()
        dialog.activateWindow()

    def _fit_companion_journal_to_current_work_area(
        self,
        dialog: QDialog | None = None,
    ) -> None:
        dialog = (
            getattr(self, "_companion_journal_dialog", None)
            if dialog is None
            else dialog
        )
        if dialog is None:
            return
        available = self._window_screen_available()
        if available.width() <= 0 or available.height() <= 0:
            return

        margin_x = min(16, max(0, (available.width() - 1) // 2))
        margin_y = min(16, max(0, (available.height() - 1) // 2))
        maximum_width = max(1, available.width() - margin_x * 2)
        maximum_height = max(1, available.height() - margin_y * 2)
        minimum_width = min(maximum_width, 260)
        minimum_height = min(maximum_height, 260)
        dialog.setMinimumSize(1, 1)
        dialog.setMaximumSize(maximum_width, maximum_height)
        dialog.setMinimumSize(minimum_width, minimum_height)
        dialog.resize(
            min(460, maximum_width),
            min(520, maximum_height),
        )
        dialog.move(
            available.left()
            + max(0, (available.width() - dialog.width()) // 2),
            available.top()
            + max(0, (available.height() - dialog.height()) // 2),
        )

    def _show_about(self) -> None:
        existing = getattr(self, "_about_dialog", None)
        if existing is not None:
            self._fit_about_dialog_to_current_work_area(existing)
            existing.show()
            self._fit_about_dialog_to_current_work_area(existing)
            existing.raise_()
            existing.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"使用与隐私 · {APP_NAME}")
        dialog.setWindowIcon(self._app_icon)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._about_dialog = dialog

        layout = QVBoxLayout(dialog)
        heading = QLabel(f"{APP_NAME}  {APP_VERSION}", dialog)
        heading.setAccessibleName(f"{APP_NAME}，版本 {APP_VERSION}")
        font = heading.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        summary = QLabel("一只在本地陪伴你的像素月灵", dialog)
        summary.setAccessibleName("一只在本地陪伴你的像素月灵")
        layout.addWidget(summary)

        details = QTextBrowser(dialog)
        details.setMinimumSize(0, 0)
        details.setOpenExternalLinks(True)
        details.setAccessibleName("MoonShell 使用方法、隐私说明与本地数据位置")
        details.setHtml(
            """
            <h3>怎么和月灵相处</h3>
            <ul>
              <li>单击月灵可以摸摸它；按住拖动可以搬家，快速松手可以抛接。</li>
              <li>右键月灵或托盘图标可调整活动强度、尺寸和感知开关。</li>
              <li>单击托盘图标可随时唤回；Alt+F4 只在托盘可用时暂时隐藏。</li>
              <li>键盘可按 Win+B 进入通知区域，用方向键选中 MoonShell，
                  再按 Shift+F10（或菜单键）打开菜单。</li>
              <li>“显示桌面月灵（重启后保持）”会记住显示偏好，但不包含开机自启。</li>
              <li>“专注陪伴”会安静 25、50 或 90 分钟；临时隐藏仍会计时。
                  退出期间不会提醒，截止前重新启动可继续；完成后会写进陪伴手账，
                  要取消请选“结束专注”。</li>
              <li>每天可领取一枚月光，没有断签惩罚；每七枚会凝成一颗星晶。</li>
              <li>“陪伴手账”会显示相识天数、月光、星晶、专注记录与离线近似月相；
                  不计算连续打卡，也不会因为没打开而惩罚你。手账还可以用现有角色与
                  当天记录在本地生成一张“今日月灵卡片”。</li>
            </ul>
            <h3>隐私与感知</h3>
            <ul>
              <li><b>核心陪伴完全本地运行；除非你主动点击“项目主页与反馈”，
                  MoonShell 不主动联网，也不上传数据。</b></li>
              <li>设备感知默认开启，只读取 CPU、内存、电量和最后输入间隔等本机状态；
                  若系统提供 NVIDIA 的 <code>nvidia-smi</code>，也会尽力读取 GPU
                  与显存占用。光标靠近只触发即时表情。</li>
              <li>“回应复制动作”默认关闭。开启后只判断剪贴板是否声明为文本类型，
                  不读取、不记录剪贴板正文。</li>
              <li>设备感知和复制回应都能在“感知与隐私”菜单关闭；隐藏时也会停止这两项。
                  光标靠近只做即时判断且不记录。</li>
            </ul>
            <h3>本地数据</h3>
            <p>设置、月光、陪伴手账、专注状态和诊断日志只保存在：</p>
            <p><code>%s</code></p>
            <p>“清除全部本地数据”会删除设置、陪伴记忆、月光和日志，然后退出。
               下次启动会像第一次见面一样重新开始。</p>
            <h3>许可</h3>
            <p>MoonShell 以 MIT 许可证发布；随包依赖的许可见
               <code>THIRD_PARTY_NOTICES.md</code>。</p>
            """
            % html.escape(str(DATA_DIR))
        )
        layout.addWidget(details, 1)

        buttons = QGridLayout()
        open_data = QPushButton("打开数据目录", dialog)
        open_data.setAccessibleName("打开 MoonShell 本地数据目录")
        open_data.clicked.connect(self._open_data_directory)
        buttons.addWidget(open_data, 0, 0)

        clear_data = QPushButton("清除全部本地数据…", dialog)
        clear_data.setAccessibleName("清除 MoonShell 全部本地数据")
        clear_data.clicked.connect(self._confirm_clear_local_data)
        buttons.addWidget(clear_data, 0, 1)

        project_page = QPushButton("项目主页与反馈", dialog)
        project_page.setAccessibleName("打开 MoonShell 项目主页与问题反馈")
        project_page.clicked.connect(self._open_project_page)
        buttons.addWidget(project_page, 1, 0)

        close_button = QPushButton("关闭", dialog)
        close_button.clicked.connect(dialog.close)
        close_button.setDefault(True)
        buttons.addWidget(close_button, 1, 1)
        layout.addLayout(buttons)
        self._about_buttons = (
            open_data,
            clear_data,
            project_page,
            close_button,
        )

        self._fit_about_dialog_to_current_work_area(dialog)
        dialog.show()
        self._fit_about_dialog_to_current_work_area(dialog)
        dialog.raise_()
        dialog.activateWindow()

    def _fit_about_dialog_to_current_work_area(
        self,
        dialog: QDialog | None = None,
    ) -> None:
        dialog = (
            getattr(self, "_about_dialog", None)
            if dialog is None
            else dialog
        )
        if dialog is None:
            return
        available = self._window_screen_available()
        if available.width() <= 0 or available.height() <= 0:
            return

        # Refit on every open, not only on construction. A user can close the
        # dialog on a large monitor and reopen it from a compact high-DPI one.
        margin_x = min(16, max(0, (available.width() - 1) // 2))
        margin_y = min(16, max(0, (available.height() - 1) // 2))
        maximum_width = max(1, available.width() - margin_x * 2)
        maximum_height = max(1, available.height() - margin_y * 2)
        target_width = min(620, maximum_width)
        target_height = min(600, maximum_height)
        minimum_width = min(
            maximum_width,
            max(240, min(420, maximum_width)),
        )
        minimum_height = min(
            maximum_height,
            max(240, min(340, maximum_height)),
        )

        # Temporarily clear the old minimum before lowering the maximum; Qt
        # otherwise preserves the larger cross-monitor constraint.
        dialog.setMinimumSize(1, 1)
        dialog.setMaximumSize(maximum_width, maximum_height)
        dialog.setMinimumSize(minimum_width, minimum_height)
        dialog.resize(target_width, target_height)

        buttons = getattr(self, "_about_buttons", ())
        if len(buttons) == 4:
            compact = maximum_width < 360
            labels = (
                ("打开目录", "清除数据…", "项目与反馈", "关闭")
                if compact
                else (
                    "打开数据目录",
                    "清除全部本地数据…",
                    "项目主页与反馈",
                    "关闭",
                )
            )
            for button, label in zip(buttons, labels):
                button.setText(label)

        x = available.left() + max(
            0,
            (available.width() - dialog.width()) // 2,
        )
        y = available.top() + max(
            0,
            (available.height() - dialog.height()) // 2,
        )
        dialog.move(x, y)

    def _open_data_directory(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))
        except OSError as exc:
            opened = False
            logger.warning("Could not open data directory %s: %s", DATA_DIR, exc)
        if not opened:
            QMessageBox.warning(
                self,
                "无法打开数据目录",
                f"请在文件管理器中手动打开：\n{DATA_DIR}",
            )

    def _open_project_page(self) -> None:
        if not QDesktopServices.openUrl(QUrl(PROJECT_URL)):
            QMessageBox.warning(
                self,
                "无法打开项目主页",
                f"请在浏览器中手动打开：\n{PROJECT_URL}",
            )

    def _confirm_clear_local_data(self) -> None:
        answer = QMessageBox.warning(
            self,
            "清除全部本地数据",
            "这会删除设置、陪伴记忆、月光、未完成的专注和诊断日志，"
            "然后退出 MoonShell。\n\n此操作无法撤销，确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._discard_data_on_shutdown = True
        self._shutdown()
        close_logging()
        failures = clear_known_local_data(include_legacy=True)
        if failures:
            paths = "\n".join(str(path) for path, _ in failures[:4])
            QMessageBox.critical(
                self,
                "部分数据无法清除",
                f"这些文件可能正被其他程序占用，请稍后手动删除：\n{paths}",
            )
        QApplication.quit()

    def _show_today_gift(self) -> None:
        self._focus_completed_pending = False
        if not self._runtime_active:
            self._recall_pet()

        today_key = time.strftime("%Y-%m-%d")
        gift = self._claim_daily_gift()
        if gift is not None:
            pose, line, seconds = gift
            self._set_action(pose, seconds, force=True)
            self.say(line, 3.8, force=True)
            self._refresh_tray_status()
            return

        if self._gift_save_failed:
            self._set_action("curious", 3.6, force=True)
            self.say(
                "这枚月光还没存好，暂时没有领取；请检查数据目录权限后重试。",
                4.8,
                force=True,
            )
            self._refresh_tray_status()
            return

        if self._gift_clock_guarded_day == today_key:
            self._gift_clock_notice_shown = True
            self._set_action("curious", 3.2, force=True)
            self.say(
                "检测到系统日期曾在未来；今天不重复发放，明天会恢复。",
                4.2,
                force=True,
            )
            self._refresh_tray_status()
            return

        if self._state.last_gift_date != today_key:
            self._set_action("curious", 2.4, force=True)
            self.say("系统日期早于上次记录，今天的月光先替你保留着。", 3.8, force=True)
            return

        milestone = self._state.moon_tokens > 0 and self._state.moon_tokens % 7 == 0
        pose = "crystal" if milestone else "gift"
        self._set_action(pose, 2.6, force=True)
        if milestone:
            line = (
                f"第 {self._state.moon_tokens // 7} 颗星晶在这里。"
                f"已经收集 {self._state.moon_tokens} 枚月光啦。"
            )
        else:
            line = f"今天的月光在这里。已经有 {self._state.moon_tokens} 枚啦。"
        self.say(line, 3.8, force=True)

    def _toggle_top(self, checked: bool) -> None:
        self.settings.always_on_top = checked
        settings_saved = self._persist_settings(notify_failure=False)
        was_active = self._runtime_active
        self.hide()
        self._configure_window()
        if was_active:
            self.show()
        if not settings_saved:
            self._notify_persistence_failure(
                "置顶设置仅在本次运行中生效，未能保存到本地。"
            )

    def _set_activity(self, level: str) -> None:
        self.settings.activity = level
        settings_saved = self._persist_settings(notify_failure=False)
        self.act_lively.setChecked(level == "high")
        self.act_calm.setChecked(level == "low")
        if level == "low":
            self.walking = False
            self.walk_target_x = None
            self.update()
            self.say("好，我安静待着。", 1.8, force=True)
        else:
            self.say("嗯，我活动活动～", 1.6)
        self._refresh_tray_status()
        if not settings_saved:
            self._notify_persistence_failure(
                "活动强度仅在本次运行中生效，未能保存到本地。"
            )

    def _toggle_system_awareness(self, checked: bool) -> None:
        self.settings.system_awareness = checked
        settings_saved = self._persist_settings(notify_failure=False)
        self.monitor.set_active(self._runtime_active and checked)
        if not checked:
            self._load = 0.0
            self._cpu = self._mem = 0.0
            self._gpu = self._gpu_memory = None
            self._batt = self._plugged = None
            if self._resource_busy:
                self._leave_resource_state()
            self._busy_samples = self._memory_samples = self._vram_samples = 0
        self._refresh_tray_status()
        if not settings_saved:
            self._notify_persistence_failure(
                "设备感知开关仅在本次运行中生效，未能保存到本地。"
            )

    def _toggle_clipboard_reactions(self, checked: bool) -> None:
        self.settings.clipboard_reactions = checked
        settings_saved = self._persist_settings(notify_failure=False)
        if checked and self._runtime_active:
            self._set_action("notify", 1.8, force=True)
            self.say("只回应复制动作，不会读取或保存内容。", 3.6, force=True)
        if not settings_saved:
            self._notify_persistence_failure(
                "复制回应开关仅在本次运行中生效，未能保存到本地。"
            )

    def _set_size_mode(self, mode: str) -> None:
        self.settings.size_mode = mode
        self._settings_dirty = True
        self.act_small.setChecked(mode != "standard")
        self.act_standard.setChecked(mode == "standard")
        self._apply_size(persist=True)

    def _toggle_debug_bounds(self, checked: bool) -> None:
        self.debug_bounds = checked
        self.update()

    def _refresh_tray_status(self) -> None:
        if not hasattr(self, "status_action"):
            return
        self._probe_tray_available()
        days = self._state.companionship_days()
        tokens = self._state.moon_tokens
        if not self.settings.enabled:
            state_text = "已停用"
        elif not self._runtime_active:
            state_text = "暂时躲起来了"
        elif self._focus_active:
            minutes = max(1, int(math.ceil(self._focus_remaining_seconds() / 60.0)))
            state_text = f"专注中 · 还剩 {minutes} 分钟"
        elif self._focus_completed_pending:
            state_text = "刚完成一段专注"
        elif self._resource_busy:
            state_text = "正在替你留意忙碌"
        else:
            state_text = self._mood_phrase()
        if self._settings_save_failed or self._state_save_failed:
            state_text = f"{state_text} · 数据未保存"
        self.status_action.setText(f"状态 · {state_text}")
        crystals = tokens // 7
        until_crystal = 7 - (tokens % 7)
        self.memory_action.setText(
            f"相识第 {days} 天 · 月光 {tokens} 枚 · "
            f"星晶 {crystals} 颗 · 下颗还差 {until_crystal} 枚"
        )
        phase = calculate_moon_phase()
        self.act_companion_journal.setText(
            f"陪伴手账 · {phase.emoji} {phase.name}…"
        )
        self.tray.setToolTip(
            f"月壳游灵 · 相识第 {days} 天 · 月光 {tokens} 枚 · "
            f"星晶 {crystals} 颗 · {phase.name} · {state_text}"
        )
        self.act_enabled.setChecked(self.settings.enabled)
        self.act_enabled.setEnabled(self._tray_available)
        self.act_visibility.setEnabled(self._tray_available)
        if self._runtime_active:
            self.act_visibility.setText("这次先隐藏月灵")
        elif self.settings.enabled:
            self.act_visibility.setText("显示月灵")
        else:
            self.act_visibility.setText("显示并启用月灵")
        self.act_focus_end.setEnabled(self._focus_active)
        if self._focus_active:
            minutes = max(1, int(math.ceil(self._focus_remaining_seconds() / 60.0)))
            self.act_focus_end.setText(f"结束专注 · 还剩 {minutes} 分钟")
        else:
            self.act_focus_end.setText("结束专注")
        for action in (self.act_focus_25, self.act_focus_50, self.act_focus_90):
            action.setEnabled(not self._focus_active)
        today_key = time.strftime("%Y-%m-%d")
        if self._gift_clock_guarded_day == today_key:
            self.act_today_gift.setText("月光明天恢复")
        elif self._state.last_gift_date == today_key:
            self.act_today_gift.setText("重看今天的月光")
        else:
            self.act_today_gift.setText("领取今日月光")
        self.act_today_gift.setEnabled(True)
        self.act_system_awareness.setChecked(self.settings.system_awareness)
        self.act_clipboard.setChecked(self.settings.clipboard_reactions)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._recall_pet()

    def _on_tray_message_clicked(self) -> None:
        if not self._focus_completed_pending:
            self._recall_pet()
            return
        if not self._runtime_active:
            self._recall_pet()
            return

        self._focus_completed_pending = False
        self.raise_()
        self.activateWindow()
        self._set_action("star", 3.0, force=True)
        self.say("这一段完成啦。起来活动一下吧。", 3.6, force=True)
        self._refresh_tray_status()

    def activate_from_second_instance(self) -> None:
        self._recall_pet()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Alt+F4 is a reversible session hide. It must not silently change the
        # saved "enabled" preference or make the next launch stay invisible.
        if not self._shutdown_done:
            if not self._probe_tray_available():
                self._shutdown()
                event.accept()
                QApplication.quit()
                return
            self._set_runtime_active(False)
            event.ignore()
            return
        super().closeEvent(event)

    def _shutdown(self) -> None:
        """Idempotent cleanup for tray quit, app.quit(), logout, and test teardown."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        for timer in (
            self.anim_timer,
            self.phys_timer,
            self.hover_timer,
            self.state_timer,
            self._display_timer,
            self._teleport_timer,
            self._mask_timer,
            self._focus_timer,
            self._focus_status_timer,
            self._tray_watchdog,
        ):
            timer.stop()
        try:
            if not self._discard_data_on_shutdown:
                self._save_position()
                self._save_state()
        finally:
            try:
                self.monitor.shutdown()
            finally:
                self.tray.hide()

    def _quit(self) -> None:
        self._shutdown()
        QApplication.quit()


# Keep the old imported class name in main.py working without changing callers.
PixelPetWindow = SpritePetWindow
