# Targets willow2-mod-manager v3.x, which is built on the shared `mods_base` package
# (the same one used by BL3/WL/BL4's SDKs). This replaces the older `Mods.ModMenu`
# (SDKMod/Hook/RegisterMod) API from the archived bl-sdk/PythonSDK repo; that API
# may still work through a "Legacy Compat" shim, but this is the current, supported
# style, confirmed against the mods_base source (hook.py / mod_factory.py).

from mods_base import build_mod

# Jack's game files are built from the player's own TPS / BL2 files (assets.py): what registration
# needs is built right here, before anything below opens a package; the rest in the background.
import traceback as _tb

from . import assets
try:
    _assets_note = assets.startup()
except Exception:  # noqa: BLE001
    _assets_note = "asset build failed: " + _tb.format_exc().splitlines()[-1]
import unrealsdk as _unrealsdk
_unrealsdk.logging.info(f"[Doppelganger] assets: {_assets_note}")

from . import dlcbundle  # noqa: F401  registers DLC/Doppelganger with the DLC enumerator (hooks) before anything loads our packages
from . import digijack  # noqa: F401  registers the digi-jack decay/leash hooks
from . import skills  # noqa: F401  TPS-only skill events / attributes bridged from Python (doppelskill)
from . import characterslot
from . import classmods  # noqa: F401  Jack's class mods: class binding, drop pools, save asset libraries (doppelcom)
from . import playertick  # the one POST PlayerTick hook (skills, mesh swap, wrist lasers)
from . import customs  # noqa: F401  Jack's heads and skins (doppelcustom)
try:                       # only in a development checkout: the co-op join trace behind `doppelnet`
    from . import netdiag  # noqa: F401
except ImportError:
    pass
from . import diag
from . import progression  # noqa: F401  TPS or BL2 skill progression rules (three settings)
from . import hudslot  # noqa: F401  Sponsored By's large HUD slot (doppelhud, step 1: tray injection test)
try:
    from . import storylines  # noqa: F401  his Pre-Sequel story lines as replies to BL2's dialogue (doppelstory); dev-only
except ImportError:
    storylines = None         # a release: the story lines are not shipped yet (build_release.DEV_ONLY)
try:
    from . import joindiag  # noqa: F401  co-op join diagnostics, always on (two copies on one PC); dev-only
except ImportError:
    joindiag = None

__author__ = "Isaac"
__version__ = "0.9.12"

HOOKS = (
    digijack._on_started,
    digijack._on_ended,
    digijack._on_tick,
    digijack._on_remote_clone_begin,
    digijack._on_skill_tick,
    digijack._on_deferred_skill_time,
    digijack._diag_take_damage,
    digijack._diag_died,
    digijack._suppress_retiring_projectiles,
    digijack._on_return_to_title,
    digijack._on_pre_client_travel,
    digijack._on_pre_commit_map_change,
    digijack._on_loop_play_anim,        # recall: the attack-loop callbacks that decide when the skill can end safely
    digijack._on_loop_cb_loop,
    digijack._on_loop_cb_finished,
    digijack._on_loop_cb_stop,
    *playertick.ALL_HOOKS,
    *skills.ALL_HOOKS,
    *characterslot.ALL_HOOKS,
    *dlcbundle.ALL_HOOKS,
    *classmods.ALL_HOOKS,
    *progression.ALL_HOOKS,
    *customs.ALL_HOOKS,
    *(storylines.ALL_HOOKS if storylines is not None else ()),
    *(joindiag.ALL_HOOKS if joindiag is not None else ()),
)
diag.instrument(HOOKS)      # wraps each callback with a timer before the mod enables them

try:
    _unrealsdk.logging.info(f"[Doppelganger] {__version__} on {diag.sdk_note()}")
except Exception:  # noqa: BLE001
    pass

_mod = build_mod(
    name="Doppelganger",
    description=(
        "Brings Jack the Doppelganger (BL:TPS) to BL2: a 7th character slot with Jack's "
        "body, voice, skill trees and Digi-Jack action skill. Laser Surplus fires on a grenade throw. "
        "Jack's files are built from your own Pre-Sequel install on first launch (doppelsetup status). "
        f"Assets: {_assets_note}"
    ),
    auto_enable=True,
    # build_mod() auto-discovers hooks by scanning THIS module's own namespace
    # (inspect.getmembers) for HookType instances; it does not recurse into
    # imported submodules. `from . import digijack` only puts the name
    # `digijack` (a module object) in scope, not the HookType objects living
    # inside it, so digijack's hooks were silently never enabled. Must list
    # every hook explicitly once any of them live outside this file.
    hooks=HOOKS,
    options=[digijack.make_appearance_option(), *progression.make_options(), *skills.make_options(),
             customs.make_option(), assets.make_inspire_option(), diag.make_option()],
)

# The saved settings are in now: from here on an option callback is the player's own change (progression refunds).
progression.settings_ready()
# co-op: a joined Jack's On My Mark tap and recall hold reach the host, where his cast and Digi-Jacks are (coopnet.py)
try:
    from . import coopnet as _coopnet
    if _coopnet.available() and _coopnet.NETWORK_FUNCTIONS:
        from networking import add_network_functions
        add_network_functions(_mod, list(_coopnet.NETWORK_FUNCTIONS), scan_module=False, scan_methods=False)
except Exception as _e:  # noqa: BLE001
    import unrealsdk as _unrealsdk
    _unrealsdk.logging.warning(f"[Doppelganger] co-op messages unavailable: {_e!r}")

# The Digi-Jack head lists: the saved choice only, until Jack's save load says what is unlocked.
try:
    digijack.trim_head_choices()
except Exception:  # noqa: BLE001
    pass

# On for a fresh install. mods_base enables a mod at launch only when its settings file says it was
# enabled last time, and a new install has no settings file, so the first launch would import, build
# Jack's files and then sit with every hook off (no "restart" dialog, no DLC install) until the
# player found the MODS menu. Enabling here writes the settings file, so a player who turns the mod
# off later stays off.
try:
    if _mod.settings_file is not None and not _mod.settings_file.exists() and not _mod.is_enabled:
        _mod.enable()
        _unrealsdk.logging.info("[Doppelganger] first launch: mod enabled")
except Exception:  # noqa: BLE001
    _unrealsdk.logging.error("[Doppelganger] first-launch enable failed: " + _tb.format_exc().splitlines()[-1])

# Registers the 7th character slot (WillowGlobals/WillowDownloadableContentManager
# arrays) right away. Per LJBreeze's own notes, if this runs before the engine
# singletons exist the very first character-select build will simply miss it and
# self-correct next time that screen is entered; see characterslot.py for details.
# The engine's package cache only knows files present at startup: a roster package written this
# launch cannot be loaded by name, and a build that failed leaves nothing to register. Both cases
# keep the slot off for the whole launch (characterslot.files_ready_this_launch, checked on every
# registration path), and the assets code shows the "restart" / "could not be built" dialog.
# A mod the player turned off in MODS is off: no slot (its Jack saves read as unloadable until it is
# back on, as with the mod removed), and the hooks that would re-register it are off with the mod.
if _mod.is_enabled:
    characterslot.register_doppelganger_slot()
else:
    _unrealsdk.logging.info("[Doppelganger] disabled in the MODS menu; the character slot stays off")
characterslot._import_phase = False
