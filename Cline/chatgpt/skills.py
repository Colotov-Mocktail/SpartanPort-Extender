"""
skills.py; the imported TPS skill trees, where BL2 needs a hand from Python.

Everything data-driven already runs natively after the v36 package pass:
attribute effects, kill/damage events, the clone side's custom events
(BodyDeath_DigiJack -> CloneDied on Teamwork/Commitment/Accountability/
Collaborate/Optimism/Leadership, the AI's OnKilledPawn -> Jack's Cache), Best
Foot Forward's StartTime push, the grenade-override skills. This module
bridges what TPS did in native code:

  * TPS-only skill events, raised with SkillDefinition.FireCustomEvent (it
    triggers a behavior event by name, Oz* names included):
      menu closed          -> Sponsored By, Money Is Power   (OzOnMenuClosed)
      cash up / cash down  -> Money Is Power                 (OnPickup / OnPaidCashForUse)
      Digi-Jack damaged    -> Accountability                 (OzOnFriendlyDamaged)
      action skill ended   -> Leadership                     (OzOnActionSkillEnd)
  * designer attributes TPS computed natively, written from here:
      Att_CompanyMan_HyperionGearPieces   (Company Man: equipped Hyperion gear)
      Att_EquippedWeaponAmmoFullness      (Taxation of Trade Routes: magazine fullness)
  * Skill_DigiJack_DamageBonuses activated on every clone (its Kill event feeds
    Take Their Freedom), which TPS's action skill did natively
  * Promote the Ranks: first Digi-Jack a badass, later ones roll Att_PromoteTheRanks_BadassChance

Console: `doppelskill grade <leaf>` | `attr <path>` | `set <path> <value>` |
`fire <leaf> <event>` | `active` | `gear`.
"""
import math
import json
import os
import random
import time
import traceback

import unrealsdk
from mods_base import get_pc, hook
from unrealsdk.commands import add_command
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from .characterslot import is_doppelganger, is_doppelganger_fast
from . import rooting
from . import diag

TAG = "[Doppelganger]"

GROUPS = {
    "Hero": "Quince_Doppel_Hero.Skills.",
    "GreaterGood": "Quince_Doppel_GreaterGood.Skills.",
    "FreeEnterprise": "Quince_Doppel_FreeEnterprise.Skills.",
}
SKILLS = {
    "BestFootForward": "Hero", "Bolster": "Hero", "HeroPose": "Hero", "Inspire": "Hero", "JacksCache": "Hero",
    "JustCompensation": "Hero", "LeanOnMe": "Hero", "OnMyMark": "Hero", "Persistence": "Hero", "PromotetheRanks": "Hero",
    "Resolute": "Hero", "TakeTheirFreedom": "Hero", "YouHaveMyShield": "Hero",
    "Accountability": "GreaterGood", "Believe": "GreaterGood", "Collaborate": "GreaterGood", "Commitment": "GreaterGood",
    "Delegation": "GreaterGood", "Diversify": "GreaterGood", "Integrity": "GreaterGood", "Leadership": "GreaterGood",
    "Optimism": "GreaterGood", "Potential": "GreaterGood", "Synergy": "GreaterGood", "Teamwork": "GreaterGood", "Winning": "GreaterGood",
    "AbsoluteAdvantage": "FreeEnterprise", "CompanyMan": "FreeEnterprise", "CompoundInterest": "FreeEnterprise",
    "HighFrequencyTrading": "FreeEnterprise", "Incentives": "FreeEnterprise", "LaserSurplus": "FreeEnterprise",
    "MarginalBenefit": "FreeEnterprise", "Merger": "FreeEnterprise", "MoneyIsPower": "FreeEnterprise",
    "SponsoredBy": "FreeEnterprise", "SupplyAndDemand": "FreeEnterprise", "TaxationofTradeRoutes": "FreeEnterprise",
}
ATT_BADASS_CHANCE = "Quince_Doppel_Hero.Misc.Att_PromoteTheRanks_BadassChance"
ATT_HYPERION_PIECES = "Quince_Doppel_FreeEnterprise.Misc.Att_CompanyMan_HyperionGearPieces"
ATT_AMMO_FULLNESS = "Quince_Doppel_FreeEnterprise.Misc.Att_EquippedWeaponAmmoFullness"
CLONE_DAMAGE_BONUS_SKILL = "GD_Quince_JackClone.Skills.Skill_DigiJack_DamageBonuses"
BADASS_CHANCE_FALLBACK = 0.33

GEAR_REFRESH_SECONDS = 2.0
AMMO_REFRESH_SECONDS = 0.25
DAMAGE_EVENT_MIN_GAP = 0.0         # Accountability: one event per TakeDamage, like TPS (pellets and DoT ticks each count; the graph caps at 50)

_logged: set = set()
_gear_dirty = True
_gear_last = None     # (pawn path, Hyperion pieces) last written
_gear_next_check = 0.0
_ammo_last = None     # (pawn path, fullness) last written
_ammo_next_check = 0.0
_cash_last: int | None = None
_last_damage_event = 0.0


def _log_once(key: str, message: str, always: bool = False) -> None:
    """Once per key. `always` puts it in the log whether or not diagnostic logging is on, for the
    handful of lines a bug report cannot be read without."""
    if key not in _logged:
        _logged.add(key)
        (unrealsdk.logging.info if always else diag.info)(f"{TAG} {message}")


def _pc() -> UObject | None:
    try:
        return get_pc()
    except Exception:
        return None

# --- co-op: the Jack an event belongs to (phase 4, 2026-10-06) -----------------------------------------------------------
# Skills run on the host. Hooks that took the host's local player as "the" Jack dropped a joined Jack's kills, pickups,
# weapon swaps and allies. These find the Jack from the event itself.
def _player_controller_of(x) -> UObject | None:
    """A Jack player's controller from a controller or a pawn (a vehicle's driver too); None for anything else."""
    try:
        if x is None:
            return None
        if "PlayerController" in str(x.Class.Name):
            return x if _ours(x) else None
        ctrl = getattr(x, "Controller", None)
        if ctrl is not None and "PlayerController" in str(ctrl.Class.Name) and _ours(ctrl):
            return ctrl
    except Exception:  # noqa: BLE001
        pass
    return None


def _on_server() -> bool:
    pc = _pc()
    try:
        return pc is not None and int(pc.WorldInfo.NetMode) != 3
    except Exception:  # noqa: BLE001
        return True


def _jack_controllers() -> list:
    """Every Jack player's controller this machine can drive skills for: all of them on the host (from the player
    list), the local one on a client."""
    local = _pc()
    if not _on_server():
        return [local] if local is not None and _ours(local) else []
    out = []
    try:
        for pri in local.WorldInfo.GRI.PRIArray:
            ctrl = getattr(pri, "Owner", None) if pri is not None else None
            if ctrl is not None and "PlayerController" in str(ctrl.Class.Name) and _ours(ctrl):
                out.append(ctrl)
    except Exception:  # noqa: BLE001
        pass
    if not out and local is not None and _ours(local):
        out = [local]
    return out


_jack_addrs: set = set()
_jack_addrs_next = 0.0


def _is_jack_controller(x) -> bool:
    """A Jack player's controller, by address from a set refreshed twice a second (this runs on every hit in the game;
    the property reads of a full check per hit were the cost the clone-amp hook was reordered to avoid)."""
    global _jack_addrs, _jack_addrs_next
    now = time.monotonic()
    if now >= _jack_addrs_next:
        _jack_addrs_next = now + 0.5
        try:
            _jack_addrs = {c._get_address() for c in _jack_controllers()}
        except Exception:  # noqa: BLE001
            _jack_addrs = set()
    try:
        return x._get_address() in _jack_addrs
    except Exception:  # noqa: BLE001
        return False


def _cast_pc() -> UObject | None:
    """The Jack whose Digi-Jacks these are: the last caster's controller, else the local player.

    In co-op a joined player's cast runs on the host (digijack records his controller), and his clones are spawned
    there. The clone-side skill reads used the host's own controller, so a joined Jack's clones got the HOST's grades -
    You Have My Shield shields from the host's points, the host's badass chance (co-op test, 2026-10-06). Found by
    path at each use, never held: a player who leaves takes his controller with him. Kept after the cast ends, for the
    clone deaths and death novas that land after it."""
    try:
        from . import digijack
        path = digijack._pc_path
        if path:
            owner = unrealsdk.find_object("WillowPlayerController", path)
            if owner is not None:
                return owner
    except Exception:  # noqa: BLE001
        pass
    return _pc()

def _ours(pc: UObject | None) -> bool:
    try:
        return pc is not None and is_doppelganger(pc)
    except Exception:
        return False


def _ours_fast(pc: UObject | None) -> bool:
    """The per-frame tick's identity check (memoised half a second, see characterslot)."""
    try:
        return pc is not None and is_doppelganger_fast(pc)
    except Exception:
        return False


def skill_path(leaf: str) -> str:
    if "." in leaf:
        return leaf
    group = SKILLS.get(leaf)
    if group is None:
        # helper skills (_Active/_Feedback) share their parent's group
        base = leaf.split("_")[0]
        group = SKILLS.get(base, "Hero")
    return GROUPS[group] + leaf


def skill_def(leaf: str) -> UObject | None:
    try:
        return unrealsdk.find_object("SkillDefinition", skill_path(leaf))
    except Exception:
        _log_once("def:" + leaf, f"skill definition not found: {skill_path(leaf)}")
        return None


# A grade is a definition lookup, an attribute evaluation and, for an unskilled skill, a GetSkillGrade call (0.1-0.25 ms),
# and it is asked for from dozens of places, several of them every frame or on every Digi-Jack death (doppelperf,
# 2026-10-01). Grades change only when points are spent or gear changes, so an answer is kept for half a second.
GRADE_CACHE_SECONDS = 0.5
_grade_cache: dict = {}


def grade(leaf: str, pc: UObject | None = None) -> int:
    """Effective grade of a skill: the tree's points plus class-mod points (0 when none / not our
    character). The live Skill's `Grade` attribute already carries the class-mod modifier stack;
    `GetSkillGrade` is the tree's points only, so a "+4 Bolster" class mod did nothing to the clone
    health the Python side computes (2026-09-15)."""
    pc = pc or _pc()
    if pc is None:
        return 0
    try:
        key = (leaf, pc._get_address())
    except Exception:  # noqa: BLE001
        return _grade_now(leaf, pc)
    now = time.monotonic()
    hit = _grade_cache.get(key)
    if hit is not None and now < hit[0]:
        return hit[1]
    value = _grade_now(leaf, pc)
    if len(_grade_cache) > 256:
        _grade_cache.clear()
    _grade_cache[key] = (now + GRADE_CACHE_SECONDS, value)
    return value


def _grade_now(leaf: str, pc: UObject) -> int:
    d = skill_def(leaf)
    if pc is None or d is None:
        return 0
    live = effective_grade(leaf, pc)
    if live:
        return live
    try:
        g = pc.GetSkillGrade(Definition=d)
        if isinstance(g, tuple):
            g = g[0]
        return int(g or 0)
    except Exception:
        _log_once("grade:" + leaf, f"GetSkillGrade({leaf}) failed: " + traceback.format_exc().splitlines()[-1])
        return 0


def effective_grade(leaf: str, pc: UObject | None = None) -> int:
    """The skill's grade with class-mod points: the SkillGradeModifiers attribute resolves to the live
    Skill's `Grade` property (its value resolver reads it through the skill context), which is the
    tree's points plus the class mod's; it is NOT an add-on (adding it to the tree's points double
    counted: a 5/5 Bolster read as 10 -> 21 decay ticks instead of 18, 2026-09-15). 0 when the skill
    has no live instance."""
    pc = pc or _pc()
    tree = SKILLS.get(leaf)
    if pc is None or tree is None or leaf not in BOOSTABLE:
        # only the class-mod skills have a SkillGradeModifiers attribute; asking for the others (Money Is Power,
        # on the first cash change or menu close) failed two object lookups per check and logged "attribute not found"
        return 0
    v = attr_value(f"Quince_Doppel_{tree}.SkillGradeModifiers.{leaf}", pc)
    return int(round(v)) if v else 0


# the skills a class mod can carry points for: the ones TPS gave a SkillGradeModifiers attribute (checked against
# GD_Doppelganger_Streaming_SF's exports, 2026-10-06: these 25; Optimism was missing here, and the Chronicler of
# Pandora carries it)
BOOSTABLE = ("BestFootForward", "Bolster", "HeroPose", "JustCompensation", "LeanOnMe", "Persistence", "Resolute", "TakeTheirFreedom",
             "Accountability", "Believe", "Collaborate", "Delegation", "Integrity", "Optimism", "Synergy", "Teamwork", "Winning",
             "AbsoluteAdvantage", "CompanyMan", "CompoundInterest", "HighFrequencyTrading", "Incentives", "MarginalBenefit",
             "SupplyAndDemand", "TaxationofTradeRoutes")
# one grade-derived attribute per skill worth printing next to the grade (the value the effect actually uses)
SWEEP_PROBES = {"AbsoluteAdvantage": "Quince_Doppel_FreeEnterprise.Misc.Att_AbsoluteAdvantage_ChanceForFreeAmmoCalc"}
_sweep_due = 0.0
_sweep_only: str | None = None
_sweep_note = ""


def equipped_class_mod(pc: UObject):
    """The WillowClassMod the player has on (None when none)."""
    try:
        mgr = pc.GetPawnInventoryManager()
    except Exception:
        mgr = None
    if mgr is None:
        return None
    try:
        r = mgr.GetCurrentClassMod(AllItems=[])
        r = r[0] if isinstance(r, tuple) else r
        if r is not None:
            return r
    except Exception:
        pass
    try:
        item = mgr.ItemChain
        while item is not None:
            if item.Class.Name == "WillowClassMod":
                return item
            item = item.Inventory
    except Exception:
        pass
    return None


def class_mod_skill_slots(com) -> str:
    """The class mod's skill slots as the engine holds them (AttributeSlots: attribute, grade, value)."""
    if com is None:
        return "-"
    out = []
    try:
        for slot in com.AttributeSlots:
            try:
                a = slot.AttributeToModify
                if a is None:
                    continue
                path = a._path_name()
                if ".SkillGradeModifiers." not in path:
                    continue
                out.append(f"{path.split('.')[-1]} +{int(slot.EffectGrade)} (value {float(slot.ComputedModifierValue):g}{'' if slot.bActivated else ', not activated'})")
            except Exception:
                out.append("?")
    except Exception:
        return "AttributeSlots unreadable: " + traceback.format_exc().splitlines()[-1]
    return ", ".join(out) if out else "no skill slots"


def class_mod_amount(com, d: UObject) -> int:
    """Points the equipped class mod gives one skill (WillowClassMod.IsModifyingSkill's out Amount)."""
    if com is None or d is None:
        return 0
    try:
        r = com.IsModifyingSkill(SkillDef=d, Amount=0)
        if isinstance(r, tuple):
            return int(r[1] or 0) if r[0] else 0
        return 0
    except Exception:
        _log_once("ismodifying", "IsModifyingSkill failed: " + traceback.format_exc().splitlines()[-1])
        return 0


def com_sweep(pc: UObject, only: str | None = None) -> str:
    """`doppelskill comsweep [Skill|all]`: one-shot check that class-mod points reach every boostable
    skill. Starts the kill skills the way a kill does (their instances only exist inside the window),
    then a few frames later compares, per skill, tree points + class-mod points (IsModifyingSkill)
    with the live Skill instance's Grade; the number every formula reads, directly (PerGradeUpgrade /
    InitializationDefScalesBaseValue) or through the SkillGradeModifiers attribute. Prints OK /
    MISMATCH / NO INSTANCE per skill; no need to equip a mod per skill."""
    global _sweep_due, _sweep_only, _sweep_note
    com = equipped_class_mod(pc)
    _sweep_note = f"class mod: {com.DefinitionData.ItemDefinition.Name if com is not None and com.DefinitionData.ItemDefinition is not None else 'none'}"
    _sweep_note += f" [slots: {class_mod_skill_slots(com)}]"
    try:
        _sweep_note += "; kill skills " + _trigger_kill_skills(pc)
    except Exception:
        _sweep_note += "; kill-skill start failed: " + traceback.format_exc().splitlines()[-1]
    _sweep_only = only
    _sweep_due = time.monotonic() + 0.4
    return "sweep armed (" + _sweep_note + "); results in a moment"


def _com_sweep_report(pc: UObject, only: str | None) -> str:
    com = equipped_class_mod(pc)
    lines = []
    bad = 0
    for leaf in BOOSTABLE:
        if only and only.lower() not in ("all", leaf.lower()):
            continue
        d = skill_def(leaf)
        if d is None:
            lines.append(f"{leaf}: definition missing")
            bad += 1
            continue
        try:
            g = pc.GetSkillGrade(Definition=d)
            tree = int((g[0] if isinstance(g, tuple) else g) or 0)
        except Exception:
            tree = -1
        amount = class_mod_amount(com, d)
        expected = tree + amount
        if expected <= 0 and not only:
            continue
        live = active_skill(leaf, pc)
        kill = leaf in KILL_SKILLS
        probe = ""
        if leaf in SWEEP_PROBES:
            v = attr_value(SWEEP_PROBES[leaf], pc)
            probe = f"; {SWEEP_PROBES[leaf].split('.')[-1]}={v if v is None else round(v, 3)}"
        if live is None:
            verdict = "NO INSTANCE" if expected > 0 else "unspecced"
            if expected > 0:
                bad += 1
            lines.append(f"{leaf}{' [kill]' if kill else ''}: tree {tree} + mod {amount} = {expected}; {verdict}{probe}")
            continue
        try:
            lg, lb = int(live.Grade), int(live.GradeBaseValue)
            stack = []
            for m in live.GradeModifierStack:
                try:
                    stack.append(round(float(m.Value), 2))
                except Exception:
                    stack.append("?")
        except Exception:
            lg, lb, stack = -1, -1, []
        verdict = "OK" if lg == expected else "MISMATCH"
        if lg != expected:
            bad += 1
        lines.append(f"{leaf}{' [kill]' if kill else ''}: tree {tree} + mod {amount} = {expected}; live Grade {lg} (base {lb}, stack {stack}) -> {verdict}{probe}")
    head = f"class-mod sweep ({_sweep_note}): {len(lines)} skill(s), {bad} problem(s)"
    return head + chr(10) + chr(10).join(lines)


def _struct_text(s, limit: int = 12) -> str:
    """Field=value for a WrappedStruct (whatever fields it has)."""
    parts = []
    try:
        for f in s._type._fields():
            name = str(f.Name)
            try:
                v = getattr(s, name)
            except Exception:
                continue
            try:
                v = v.Name if hasattr(v, "Name") and not isinstance(v, (int, float, str, bool)) else v
            except Exception:
                pass
            parts.append(f"{name}={v}")
            if len(parts) >= limit:
                break
    except Exception as e:
        return f"(unreadable: {e})"
    return " ".join(parts)


def tracked_report(pc: UObject) -> str:
    """`doppelskill tracked`: the PRI's tracked-skill slots (what the HUD draws icons from) and every
    live skill of ours with a tracked type."""
    lines = []
    try:
        pri = pc.PlayerReplicationInfo
        lines.append(f"NumTrackedSkillSlotsInUse={int(pri.NumTrackedSkillSlotsInUse)}")
        for k, t in enumerate(pri.TrackedSkills):
            lines.append(f"  slot {k}: {_struct_text(t)}")
    except Exception as e:
        lines.append(f"PRI tracked skills unreadable: {e}")
    try:
        for sk in pc.GetSkillManager().ActiveSkills:
            try:
                d = sk.Definition
                if d is None or not d._path_name().startswith("Quince_Doppel_"):
                    continue
                tt = int(d.TrackedSkillType)
                if tt == 0:
                    continue
                icon = d.SkillIcon
                lines.append(f"  live {d.Name}: TrackedSkillType={tt} slot={int(d.TrackedSkillHUDSlot)} state={int(sk.SkillState)} "
                             f"icon={icon._path_name() if icon is not None else None} frame={d.SkillIconTextureName}")
            except Exception as e:
                lines.append(f"  live ?: {e}")
    except Exception as e:
        lines.append(f"active skills unreadable: {e}")
    return "tracked skills:" + chr(10) + chr(10).join(lines)


COMSKILL_PROBES = ("GD_Quince_ClassMods.Misc.WeaponTotalAutomaticFiringTime",)
_comskills_until = 0.0
_comskills_next = 0.0
_comskills_samples: list = []


def comskills_start(seconds: float) -> str:
    """`doppelskill comskills [s]`: for `s` seconds after the command, sample (10 Hz) the class-mod
    skills that are live (GD_Quince_ClassMods.Skills.*), their state and applied modifier values, and
    the firing-time attribute; a summary prints at the end. Close the console and fire."""
    global _comskills_until, _comskills_next, _comskills_samples
    _comskills_samples = []
    _comskills_until = time.monotonic() + seconds
    _comskills_next = 0.0
    return f"sampling class-mod skills for {seconds:.0f} s; close the console and fire"


def _comskills_sample(pc: UObject) -> dict:
    out = {}
    try:
        for sk in pc.GetSkillManager().ActiveSkills:
            try:
                d = sk.Definition
                if d is None or not d._path_name().startswith("GD_Quince_ClassMods.Skills."):
                    continue
                vals = []
                for eff in sk.SkillEffects:
                    try:
                        m = eff.Modifier
                        vals.append(round(float(m.Value), 4) if m is not None else None)
                    except Exception:
                        vals.append("?")
                out[d.Name] = (int(sk.SkillState), vals)
            except Exception:
                pass
    except Exception:
        pass
    for path in COMSKILL_PROBES:
        v = attr_value(path, pc)
        if v is None and pc.Pawn is not None:
            v = attr_value(path, pc.Pawn.Weapon) if pc.Pawn.Weapon is not None else None
        out[path.split(".")[-1]] = v
    return out


def _comskills_tick(now: float) -> None:
    global _comskills_next, _comskills_until
    if not _comskills_until:
        return
    if now >= _comskills_until:
        _comskills_until = 0.0
        pc = _pc()
        names = sorted({k for s in _comskills_samples for k in s})
        lines = [f"{len(_comskills_samples)} samples:"]
        for k in names:
            seen = [s[k] for s in _comskills_samples if k in s]
            if seen and isinstance(seen[0], tuple):
                states = sorted({v[0] for v in seen})
                firsts, lasts = seen[0][1], seen[-1][1]
                lo = [min((v[1][i] for v in seen if isinstance(v[1][i], (int, float))), default=None) for i in range(len(firsts))]
                hi = [max((v[1][i] for v in seen if isinstance(v[1][i], (int, float))), default=None) for i in range(len(firsts))]
                lines.append(f"  {k}: states {states}; effect values min {lo} max {hi}")
            else:
                nums = [v for v in seen if isinstance(v, (int, float))]
                lines.append(f"  {k}: min {min(nums) if nums else None} max {max(nums) if nums else None} ({len(seen)} reads)")
        if not names:
            lines.append("  no class-mod skill live (GD_Quince_ClassMods.Skills.*); is a Legendary Doppelganger equipped?")
        diag.info(f"{TAG} class-mod skill sampler: " + chr(10).join(lines))
        return
    if now < _comskills_next:
        return
    _comskills_next = now + 0.1
    pc = _pc()
    if pc is not None:
        _comskills_samples.append(_comskills_sample(pc))


def _com_sweep_tick(now: float) -> None:
    global _sweep_due
    if not _sweep_due or now < _sweep_due:
        return
    _sweep_due = 0.0
    pc = _pc()
    if pc is None:
        return
    try:
        diag.info(f"{TAG} " + _com_sweep_report(pc, _sweep_only))
    except Exception:
        diag.info(f"{TAG} class-mod sweep failed: " + traceback.format_exc())


def class_mod_points(leaf: str, pc: UObject | None = None) -> int:
    """Points a class mod adds to a skill (effective grade minus the tree's)."""
    pc = pc or _pc()
    d = skill_def(leaf)
    if pc is None or d is None:
        return 0
    try:
        g = pc.GetSkillGrade(Definition=d)
        g = int((g[0] if isinstance(g, tuple) else g) or 0)
    except Exception:
        g = 0
    return max(0, effective_grade(leaf, pc) - g)


def active_skill(leaf: str, pc: UObject | None = None):
    """The live Skill instance of one of the player's skills (None when it has no points / is not active)."""
    pc = pc or _pc()
    d = skill_def(leaf)
    if pc is None or d is None:
        return None
    try:
        manager = pc.GetSkillManager()
    except Exception:
        return None
    for instigator in (pc, pc.Pawn):
        if instigator is None:
            continue
        try:
            sk = manager.GetActiveSkillForInstigatorByDefinition(SkillInstigator=instigator, Definition=d)
        except Exception:
            sk = None
        if sk is not None:
            return sk
    return None


_fired: dict = {}


BVAR_FLOAT = 3
BVAR_OBJECT = 5     # EBehaviorVariableType (GearboxFramework enum order)


def event_params(*values) -> list:
    """Parameters for ActivateBehaviorEventFromScript, in the order TPS's native raiser declared
    them (the graph's event outputs pick by ConnectionIndex): objects, or floats/ints."""
    out = []
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(unrealsdk.make_struct("BehaviorVariableValue", VariableType=BVAR_FLOAT, FloatValue=float(v), IntValue=int(v)))
        else:
            out.append(unrealsdk.make_struct("BehaviorVariableValue", VariableType=BVAR_OBJECT, ObjectValue=v))
    return out


def fire(leaf: str, event: str, other: UObject | None = None, pc: UObject | None = None, need_grade: bool = True,
         params: list | None = None) -> bool:
    """
    Raise a named behavior event on one of the player's skills (TPS's Oz* names included).
    `params`: event_params(...) in the native raiser's order, for events whose graph reads
    outputs (Accountability's OzOnFriendlyDamaged links SkillInstigator and Enemy).

    Same steps as BL2's Behavior_SkillCustomEvent (decompiled 2026-09-13): find the live
    Skill for the definition, take its behavior consumer handle, and call the static
    BehaviorKernel.ActivateBehaviorEventFromScript(handle, provider, EventName). That node
    locates the controller from ITS context, which for a Digi-Jack is the clone's own mind,
    so the clone-side CloneDied/PetKilledEnemy nodes never reach Jack's skills in BL2; this
    fires them on the player's instance instead. SkillDefinition.FireCustomEvent is the
    fallback (it did nothing visible for Sponsored By / Money Is Power in v36).
    """
    pc = pc or _pc()
    if pc is None:
        return False
    sk = active_skill(leaf, pc)
    if sk is None:
        if need_grade:
            return False
    d = skill_def(leaf)
    if d is None:
        return False
    key = f"{leaf}:{event}"
    try:
        if sk is not None:
            handle = sk.GetBehaviorConsumerHandle()
            kernel = unrealsdk.find_class("BehaviorKernel").ClassDefaultObject
            if params:
                kernel.ActivateBehaviorEventFromScript(ConsumerHandle=handle, ProviderDefinition=d.BehaviorProviderDefinition, EventName=event,
                                                       EventOutputToActivate=0, Parameters=params)
            else:
                kernel.ActivateBehaviorEventFromScript(ConsumerHandle=handle, ProviderDefinition=d.BehaviorProviderDefinition, EventName=event, EventOutputToActivate=0)
            n = _fired.get(key, 0) + 1
            _fired[key] = n
            if n <= 3:
                diag.info(f"{TAG} skill event {event} -> {leaf} (kernel route, #{n})")
            return True
    except Exception:
        _log_once("kernel:" + key, f"kernel event {event} -> {leaf} failed: " + traceback.format_exc().splitlines()[-1])
    try:
        d.FireCustomEvent(EventName=event, InController=pc, OtherParticipant=other)
        _log_once("fire:" + key, f"skill event {event} -> {leaf} via FireCustomEvent (fallback)")
        return True
    except Exception:
        _log_once("fire-fail:" + key, f"FireCustomEvent({leaf}, {event}) failed: " + traceback.format_exc().splitlines()[-1])
        return False


CLONE_BODY_DEATH_BPD = "GD_Quince_JackClone.Death.BodyDeath_DigiJack.BehaviorProviderDefinition_6"
CLONE_AI_BPD = "GD_Quince_JackClone.AI.AIDef_DigiJack_BaseAndBadass.AIBehaviorProviderDefinition_0"
CHANGE_ENABLE = 1       # EChangeStatus: CHANGE_Disable = 0, CHANGE_Enable = 1 (measured: 0 disabled the sequence)


def _kernel():
    return unrealsdk.find_class("BehaviorKernel").ClassDefaultObject


def fire_on_clone(pawn: UObject, event: str, provider_path: str = CLONE_BODY_DEATH_BPD) -> bool:
    """
    Raise a named behavior event on one of a clone's own graphs (the pawn is the behavior
    consumer of its BodyClass death graph; its mind runs the AI graph). TPS's native action
    skill raised CE_CloneDespawned on the body-death graph when it retired a Digi-Jack:
    that sequence sends CloneDied / CloneGibbed to the master's skills and sets off
    Optimism's explosion.
    """
    try:
        provider = unrealsdk.find_object("BehaviorProviderDefinition", provider_path)
        handle = pawn.GetBehaviorConsumerHandle()
        _kernel().ActivateBehaviorEventFromScript(ConsumerHandle=handle, ProviderDefinition=provider, EventName=event, EventOutputToActivate=0)
        key = "clone:" + event
        n = _fired.get(key, 0) + 1
        _fired[key] = n
        if n <= 3:
            if diag.enabled:
                diag.info(f"{TAG} clone graph event {event} -> {pawn.Name} (#{n})")
        return True
    except Exception:
        _log_once("clonefire:" + event, f"clone graph event {event} failed: " + traceback.format_exc().splitlines()[-1])
        return False


# --- the death nova at the moment of death -----------------------------------------------------------------------
# You Have My Shield's death nova is the YouHaveMyShield sequence of the clone's death graph, on OnKilledBy. Our
# clones only really die at cleanup (Suicide, DEATH_SECONDS after the dissolve starts), so the nova came a second
# after the clone visibly went; TPS's clones die at once and the nova is instant (players, 2026-09-29). So the
# death raises OnKilledBy itself, then NovaProcced: the sequence's own "already went off" flag, which its chain
# tests (CompareBool_0) before any nova, so the OnKilledBy that Suicide raises later finds it set. The 'Skill'
# sequence also answers OnKilledBy (CloneDied to the master's skills); the death already sends those through
# CE_CloneDespawned, so its entry nodes are held back for the early raise. Raised early, the chain also sees the
# clone's own shield copy, which cleanup releases before Suicide.
EARLY_NOVA_BLOCKED = frozenset([f"Behavior_SkillCustomEvent_{n}" for n in range(12, 18)]
                               + ["Behavior_Conditional_4", "Behavior_Conditional_5"])
_early_nova = False


def raise_clone_death_nova(pawn: UObject) -> None:
    global _early_nova
    try:
        yhms_death_specials(pawn)        # first: a Firehawk's own nova blocks the graph's (_fix_clone_death_nova)
    except Exception:  # noqa: BLE001
        _log_once("yhms:specials", "special shield on a Digi-Jack's death failed: " + traceback.format_exc().splitlines()[-1])
    _early_nova = True
    try:
        fire_on_clone(pawn, "OnKilledBy")
    finally:
        _early_nova = False
    fire_on_clone(pawn, "NovaProcced")


# --- You Have My Shield: the Antagonist, the Impaler and the Flame of the Firehawk on a Digi-Jack's death ----------
# The death nova above is the clone's shield copy's nova and nothing else, so a Digi-Jack wearing a spike shield did
# nothing when it died (users, 2026-10-01). Two spike shields fire projectiles of their own, from their skills'
# Behavior_SpawnProjectile when shot: the Impaler (GD_Shields.A_Item.Shield_Spike_CorrosiveLegendary) homing
# corrosive darts, the Antagonist (Tiny Tina's Seraph, GD_Aster_Shields.A_Item.Aster_Seraph_Antagonist_Shield) slag
# balls, both at Att_ShieldSpikeDamage_Corrosive. A Digi-Jack with a copy of either now looses a burst of them at the
# enemies near it as it dies: a runtime copy of the shield's own spawn node, run on the clone (so the damage resolves
# against its copy, as the shield's graph does), each projectile then pointed at an enemy and homed on it.
# The Firehawk (Shield_Nova_Fire_Phoenix) carries the fire nova attributes (its copy logged "Fire 1976"), yet the
# clone's death graph never set off its nova (user, 2026-10-01: no death nova line for any Firehawk death), so its own
# Phoenix explosion is set off here, once (a depleted Firehawk repeats it every few seconds; a dying clone gets one),
# with the copy's fire nova damage and radius and the Phoenix skill's burn (10% of the damage). Should the graph's
# nova go off for that clone after all, _fix_clone_death_nova blocks it: one nova, never two.
# The Deadly Bloom (Shield_Nova_Explosive_05_DeadlyBloom, skill GD_Shields.Skills.Nova_Shield_Skill_Legendary) novas
# when it breaks after being full and once more when its wearer is hit in Fight For Your Life (DamagedByEnemy at
# HealthCurrentValue <= 1, 1.5 s later). A Digi-Jack has no FFYL, so its death stands in for it (user, 2026-10-01):
# a Deadly Bloom copy novas on death, and when the clone dies with shield left (decay, leash) the break comes with it,
# so it goes off twice, the break at once and the death nova DEADLY_BLOOM_STAGGER later. Its own explosion, falloff off
# as in its skill, with the copy's explosive nova damage and radius; the break nova from enemy fire is still the copy's
# own skill. The graph's nova for that clone is blocked as for the Firehawk.
YHMS_SPIKE_SHIELDS = {
    "Shield_Spike_CorrosiveLegendary": ("Impaler", "GD_Shields.Skills.Spike_Shield_Skill_CorrosiveLegendary.BehaviorProviderDefinition_0.Behavior_SpawnProjectile_5"),
    "Aster_Seraph_Antagonist_Shield": ("Antagonist", "GD_Aster_Shields.Skills.Antagonist_Shield_Skill.BehaviorProviderDefinition_0.Behavior_SpawnProjectile_20"),
}
FIREHAWK_SHIELD = "Shield_Nova_Fire_Phoenix"
PHOENIX_EXPLOSION = "GD_Explosions.Incendiary.Explosion_IncendiaryMaster_ShieldNovaPhoenix"
SPIKE_DAMAGE_ATTR = "GD_Shields.Misc.Att_ShieldSpikeDamage_Corrosive"
PROJECTILE_DAMAGE_ATTR = "D_Attributes.Projectile.ProjectileDamage"
DEATH_SPIKES = 6                 # projectiles per death, spread over the nearest enemies
DEATH_SPIKE_RANGE = 2500.0       # units from the clone
DIRECTION_TOWARD_TARGET = 11     # EDirectionRelativeToParent.DIRECTION_StraightTowardTarget
FIREHAWK_NOVA_ATTRS = ("GD_Shields.Misc.Att_ShieldNovaDamage_Fire", "GD_Shields.Misc.Att_ShieldNovaSize_Fire")
DEADLY_BLOOM_SHIELD = "Shield_Nova_Explosive_05_DeadlyBloom"
DEADLY_BLOOM_EXPLOSION = "GD_Explosions.explosive.Explosion_ExplosiveMaster_ShieldNova"
DEADLY_BLOOM_NOVA_ATTRS = ("GD_Shields.Misc.Att_ShieldNovaDamage_Explosive", "GD_Shields.Misc.Att_ShieldNovaSize_Explosive")
DEADLY_BLOOM_STAGGER = 0.4       # seconds between the break nova and the death nova (the body is up for DEATH_SECONDS)
_special_novas: dict = {}        # clone path -> when its shield's own death nova went off (the graph's is blocked)
_bloom_pending: list = []        # [due, clone path, damage, radius]: the death nova after a break nova
_yhms_capture: list | None = None
_yhms_counter = 0
_yhms_logged = 0


_delegation_capture: list | None = None     # Delegation's bolts, collected inside its spawn node's call


@hook("WillowGame.WillowProjectile:PostBeginPlay", Type.POST, hook_identifier="doppel_yhms_projectile")
def _on_yhms_projectile(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """Collects the projectiles our own spawn node makes, inside its call (a scan of every projectile cost tens of ms)."""
    if _yhms_capture is not None:
        _yhms_capture.append(obj)
    if _delegation_capture is not None:
        _delegation_capture.append(obj)


def _shield_def_name(item) -> str:
    try:
        d = item.DefinitionData.ItemDefinition
        return d.Name if d is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _holder_shield_def(holder) -> str:
    """The shield definition's name for a shield item, or for a pawn its equipped shield's."""
    if holder is None:
        return ""
    if "Shield" in str(holder.Class.Name):
        return _shield_def_name(holder)
    return _shield_def_name(_shield_of(holder))


def _clone_shield_copy(pawn: UObject):
    path = _shield_copies.get(pawn._path_name()) if pawn is not None else None
    if not path:
        return None
    try:
        return unrealsdk.find_object("WillowShield", path)
    except Exception:  # noqa: BLE001
        return None


def _live_pawns(pc: UObject):
    """The level's pawns from WorldInfo.PawnList (a few dozen), not a scan of every object."""
    try:
        p = pc.WorldInfo.PawnList
        seen = 0
        while p is not None and seen < 2000:
            yield p
            seen += 1
            p = p.NextPawn
    except Exception:  # noqa: BLE001
        return


def _enemies_near(pc: UObject, loc, radius: float) -> list:
    out = []
    for p in _live_pawns(pc):
        try:
            if p.Name.startswith("Default__") or p.bDeleteMe or p.bHidden:
                continue
            l = p.Location
            d = math.sqrt((l.X - loc.X) ** 2 + (l.Y - loc.Y) ** 2 + (l.Z - loc.Z) ** 2)
            if d <= radius and _omm_enemy(pc, p):
                out.append((d, p))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def _aim_projectile(proj: UObject, target, damage: float, k: int) -> None:
    try:
        if damage > 0.0 and (attr_value(PROJECTILE_DAMAGE_ATTR, proj) or 0.0) <= 0.0:
            set_attr(PROJECTILE_DAMAGE_ATTR, proj, damage)          # the node's base value did not resolve on the clone
    except Exception:  # noqa: BLE001
        pass
    try:
        v, l = proj.Velocity, proj.Location
        speed = math.sqrt(v.X * v.X + v.Y * v.Y + v.Z * v.Z) or 1500.0
        if target is not None:
            t = target.Location
            d = _norm((t.X - l.X, t.Y - l.Y, t.Z + 40.0 - l.Z))
        else:                                                    # nobody near: a ring
            a = 2.0 * math.pi * k / DEATH_SPIKES
            d = _norm((math.cos(a), math.sin(a), 0.15))
        proj.Velocity = unrealsdk.make_struct("Vector", X=d[0] * speed, Y=d[1] * speed, Z=d[2] * speed)
    except Exception:  # noqa: BLE001
        pass
    if target is not None:
        try:
            proj.HomingActor = target
            proj.bIsHoming = True
        except Exception:  # noqa: BLE001
            pass


def _yhms_death_spikes(pc: UObject, pawn: UObject, copy, label: str, node_path: str) -> str:
    global _yhms_capture, _yhms_counter
    try:
        src = unrealsdk.find_object("Behavior_SpawnProjectile", node_path)
    except Exception:  # noqa: BLE001
        src = None
    if src is None or src.ProjectileDefinition is None:
        return f"{label}: its projectile is not loaded"
    damage = attr_value(SPIKE_DAMAGE_ATTR, copy) or attr_value(SPIKE_DAMAGE_ATTR, pawn) or 0.0
    targets = _enemies_near(pc, pawn.Location, DEATH_SPIKE_RANGE)
    _yhms_counter += 1
    beh = unrealsdk.construct_object("Behavior_SpawnProjectile", pawn, f"DoppelYhmsSpikes_{_yhms_counter}", 0, None)
    beh.ProjectileDefinition = src.ProjectileDefinition
    beh.ChildProjectileBaseValues = src.ChildProjectileBaseValues
    beh.NumProjectiles = 1
    beh.SpawnDirection = DIRECTION_TOWARD_TARGET
    spawned = []
    _yhms_capture = spawned
    try:
        for k in range(DEATH_SPIKES):
            target = targets[k % len(targets)] if targets else None
            before = len(spawned)
            beh.ApplyBehaviorToContext(ContextObject=pawn, KernelInfo=unrealsdk.make_struct("BehaviorKernelInfo"), SelfObject=pawn,
                                       MyInstigatorObject=pawn, OtherEventParticipantObject=target,
                                       EventData=unrealsdk.make_struct("BehaviorParameters"))
            for proj in spawned[before:]:
                _aim_projectile(proj, target, damage, k)
    finally:
        _yhms_capture = None
    carried = attr_value(PROJECTILE_DAMAGE_ATTR, spawned[0]) if spawned else None
    return (f"{label}: {len(spawned)} {src.ProjectileDefinition.Name} at {min(len(targets), DEATH_SPIKES)} enemies, "
            f"spike damage {damage:.0f}, projectile damage {carried if carried is None else round(carried)}")


def _yhms_firehawk_nova(pawn: UObject, copy) -> str:
    global _yhms_counter
    dmg = attr_value(FIREHAWK_NOVA_ATTRS[0], copy) or 0.0
    size = attr_value(FIREHAWK_NOVA_ATTRS[1], copy) or 0.0        # the copy's own, already scaled by the radius setting
    if dmg <= 0.0 or size <= 0.0:
        return f"Firehawk: no nova on the copy (damage {dmg:.0f}, radius {size:.0f})"
    definition = _obj(PHOENIX_EXPLOSION)
    if definition is None:
        return "Firehawk: its Phoenix explosion is not loaded"
    _yhms_counter += 1
    beh = unrealsdk.construct_object("Behavior_Explode", pawn, f"DoppelYhmsPhoenix_{_yhms_counter}", 0, None)
    beh.Definition = definition
    beh.DamageSource = unrealsdk.find_class("WillowDmgSource_ShieldNova")
    beh.DamageFormula = _init_value(dmg)
    beh.DamageRadiusFormula = _init_value(size)
    beh.StatusEffectDamage = _init_value(dmg * 0.1)
    beh.StatusEffectChance = _init_value(100.0)
    beh.bReplicate = True
    _special_novas[pawn._path_name()] = time.monotonic()
    beh.ApplyBehaviorToContext(ContextObject=pawn, KernelInfo=unrealsdk.make_struct("BehaviorKernelInfo"), SelfObject=pawn,
                               MyInstigatorObject=pawn, OtherEventParticipantObject=None,
                               EventData=unrealsdk.make_struct("BehaviorParameters"))
    return f"Firehawk: Phoenix nova, damage {dmg:.0f}, radius {size:.0f}"


def _bloom_explode(pawn: UObject, dmg: float, size: float) -> bool:
    global _yhms_counter
    definition = _obj(DEADLY_BLOOM_EXPLOSION)
    if definition is None:
        return False
    _yhms_counter += 1
    beh = unrealsdk.construct_object("Behavior_Explode", pawn, f"DoppelYhmsBloom_{_yhms_counter}", 0, None)
    beh.Definition = definition
    beh.DamageSource = unrealsdk.find_class("WillowDmgSource_ShieldNova")
    beh.DamageFormula = _init_value(dmg)
    beh.DamageRadiusFormula = _init_value(size)
    beh.bDisableRadiusDamageFalloff = True
    beh.bReplicate = True
    beh.ApplyBehaviorToContext(ContextObject=pawn, KernelInfo=unrealsdk.make_struct("BehaviorKernelInfo"), SelfObject=pawn,
                               MyInstigatorObject=pawn, OtherEventParticipantObject=None,
                               EventData=unrealsdk.make_struct("BehaviorParameters"))
    return True


def _yhms_deadly_bloom(pawn: UObject, copy) -> str:
    dmg = attr_value(DEADLY_BLOOM_NOVA_ATTRS[0], copy) or 0.0
    size = attr_value(DEADLY_BLOOM_NOVA_ATTRS[1], copy) or 0.0     # the copy's own, already scaled by the radius setting
    if dmg <= 0.0 or size <= 0.0:
        return f"Deadly Bloom: no nova on the copy (damage {dmg:.0f}, radius {size:.0f})"
    try:
        shield_left = float(pawn.GetShieldStrength())
    except Exception:  # noqa: BLE001
        shield_left = 0.0
    path = pawn._path_name()
    _special_novas[path] = time.monotonic()
    if not _bloom_explode(pawn, dmg, size):
        return "Deadly Bloom: its nova explosion is not loaded"
    if shield_left > 0.0:
        _bloom_pending.append([time.monotonic() + DEADLY_BLOOM_STAGGER, path, dmg, size])
        return f"Deadly Bloom: died with {shield_left:.0f} shield, break nova now and the death nova next, damage {dmg:.0f}, radius {size:.0f}"
    return f"Deadly Bloom: death nova, damage {dmg:.0f}, radius {size:.0f}"


def deadly_bloom_tick() -> None:
    """The Deadly Bloom's death nova that follows its break nova (a clone that died with shield left)."""
    if not _bloom_pending:
        return
    now = time.monotonic()
    from . import digijack
    for item in [x for x in _bloom_pending if now >= x[0]]:
        _bloom_pending.remove(item)
        pawn = digijack._resolve_pawn(item[1])
        if pawn is None:
            continue                     # the body went first: nothing to go off from
        try:
            _special_novas[item[1]] = now
            _bloom_explode(pawn, item[2], item[3])
        except Exception:  # noqa: BLE001
            _log_once("yhms:bloom", "Deadly Bloom death nova failed: " + traceback.format_exc().splitlines()[-1])


def yhms_special(pc: UObject, pawn: UObject, copy) -> str | None:
    """What this clone's shield copy does as it dies, beyond the plain nova; None for an ordinary shield."""
    name = _shield_def_name(copy)
    if name == FIREHAWK_SHIELD:
        return _yhms_firehawk_nova(pawn, copy)
    if name == DEADLY_BLOOM_SHIELD:
        return _yhms_deadly_bloom(pawn, copy)
    kind = YHMS_SPIKE_SHIELDS.get(name)
    return _yhms_death_spikes(pc, pawn, copy, *kind) if kind is not None else None


def yhms_death_specials(pawn: UObject) -> None:
    global _yhms_logged
    copy = _clone_shield_copy(pawn)
    if copy is None:
        return                       # no You Have My Shield copy on this clone
    pc = _cast_pc()
    if pc is None or pc.Pawn is None:
        return
    note = yhms_special(pc, pawn, copy)
    if note is None:
        return
    if _yhms_logged < 6:
        _yhms_logged += 1
        unrealsdk.logging.info(f"{TAG} You Have My Shield, Digi-Jack death: {note}")


def _early_nova_blocks(node: UObject) -> bool:
    try:
        return (_early_nova and node.Name in EARLY_NOVA_BLOCKED
                and node.Outer._path_name().replace(":", ".") == CLONE_BODY_DEATH_BPD)
    except Exception:  # noqa: BLE001
        return False


@hook("WillowGame.Behavior_SkillCustomEvent:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_early_nova_sce")
def _on_early_nova_skill_event(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    if _early_nova_blocks(obj):
        return Block
    if _no_leadership_raise and _is_leadership_death_node(obj):
        return Block                         # a Hostile Takeover Digi-Jack: its death is not Leadership's (see _death_stages)
    return None


def _is_leadership_death_node(node: UObject) -> bool:
    try:
        sd = node.SkillDef
        return (sd is not None and sd.Name == "Leadership"
                and node.Outer._path_name().replace(":", ".") == CLONE_BODY_DEATH_BPD)
    except Exception:  # noqa: BLE001
        return False


def clone_sequence_states(pawn: UObject) -> str:
    """Enabled state of every sequence of the clone's death and AI graphs (IsBehaviorSequenceEnabled)."""
    out = []
    for provider_path in (CLONE_BODY_DEATH_BPD, CLONE_AI_BPD):
        try:
            provider = unrealsdk.find_object("BehaviorProviderDefinition", provider_path)
            consumer = pawn if provider_path == CLONE_BODY_DEATH_BPD else pawn.Controller
            handle = consumer.GetBehaviorConsumerHandle()
            for seq in provider.BehaviorSequences:
                name = str(seq.BehaviorSequenceName)
                on = _kernel().IsBehaviorSequenceEnabled(ConsumerHandle=handle, ProviderDefinition=provider, BehaviorSequenceName=name)
                out.append(f"{provider_path.split('.')[-2]}/{name}={on}")
        except Exception:
            out.append(f"{provider_path.split('.')[-1]}: ERR " + traceback.format_exc().splitlines()[-1])
    return ", ".join(out)


def enable_clone_sequence(pawn: UObject, name: str, provider_path: str = CLONE_BODY_DEATH_BPD) -> bool:
    try:
        provider = unrealsdk.find_object("BehaviorProviderDefinition", provider_path)
        consumer = pawn if provider_path == CLONE_BODY_DEATH_BPD else pawn.Controller
        _kernel().ChangeBehaviorSequenceActivationStatus(ConsumerHandle=consumer.GetBehaviorConsumerHandle(), ProviderDefinition=provider,
                                                          SequenceName=name, ActivationStatusChanage=CHANGE_ENABLE)
        return True
    except Exception:
        _log_once("seqenable:" + name, f"enabling sequence {name} failed: " + traceback.format_exc().splitlines()[-1])
        return False


def _attr(path: str) -> UObject | None:
    for cls in ("AttributeDefinition", "DesignerAttributeDefinition"):
        try:
            a = unrealsdk.find_object(cls, path)
            if a is not None:
                return a
        except Exception:
            continue
    _log_once("attr:" + path, f"attribute not found: {path}")
    return None


def attr_value(path: str, ctx: UObject | None) -> float | None:
    a = _attr(path)
    if a is None or ctx is None:
        return None
    try:
        r = a.GetValue(ContextSource=ctx)
        if isinstance(r, tuple):
            r = r[0]
        return float(r)
    except Exception:
        _log_once("getvalue:" + path, f"GetValue({path}) failed: " + traceback.format_exc().splitlines()[-1])
        return None


def set_attr(path: str, ctx: UObject | None, value: float) -> bool:
    a = _attr(path)
    if a is None or ctx is None:
        return False
    try:
        a.SetAttributeBaseValue(ContextSource=ctx, BaseValue=float(value), bSuppressNotify=False)
        return True
    except Exception:
        _log_once("setvalue:" + path, f"SetAttributeBaseValue({path}) failed: " + traceback.format_exc().splitlines()[-1])
        return False


# --- skill lifecycle (called from digijack) --------------------------------------------------------------------------
_promote_grade_at_start = 0
_bolster_grade_at_start = 0
_skill_def_cache: UObject | None = None


def on_skill_start(pc: UObject | None) -> None:
    global _gear_dirty, _bolster_grade_at_start, _skill_def_cache, _clone_slot, _clone_deaths_this_cast
    _clone_deaths_this_cast = 0
    global _omm_used, _leadership_used, _leadership_was_down
    _omm_used = False                         # one On My Mark per cast, whoever's cast (co-op: each Jack's record)
    _leadership_used = False                  # and one Leadership revive (the graph's own rule)
    _leadership_was_down = False
    if pc is not None:
        _omm_new_activation(pc)               # its ready icon (the feedback skill) for whoever casts: a joined Jack's
                                              # cast never reached the local edge check in _omm_tick (2026-10-06)
    _gear_dirty = True
    _bolster_grade_at_start = grade("Bolster", pc)
    _skill_def_cache = None
    _clone_slot = 0


def reset_session() -> None:
    """Level change / save-quit: drop every cached package object, and every once-per-session
    package edit (a reload brings the streaming package back from disk without them: the
    runtime FX materials, the Laser Surplus / badass-barrage stand-ins) plus every path cache."""
    global _skill_def_cache, _cash_last, _gear_last, _ammo_last, _last_weapon_path, _action_skill_keys
    global _fx_applied, _ls_fixed, _badass_fixed, _merger_manager_path, _merger_impact_path, _fx_next_try
    global _skill_key_down_since, _skill_key_hold_done
    _skill_key_down_since, _skill_key_hold_done = 0.0, False
    _skill_def_cache = None
    _last_weapon_path = None
    _action_skill_keys = None
    _cash_last = None
    _gear_last = None
    _ammo_last = None
    _fx_applied = False
    _fx_materials.clear()
    _fx_next_try = 0.0
    _ls_fixed = False
    _badass_fixed = False
    _merger_manager_path = None
    _merger_impact_path = None
    _body_socket_paths.clear()
    _merger_socket_backup.clear()
    _tracked_projectiles.clear()
    _merger_hit_dist.clear()
    _amp_base_save(force=True)         # the measured shot, kept in memory between writes
    global _merger_infinite, _merger_fire_pending, _merger_move_at
    _merger_infinite = _merger_fire_pending = False
    _merger_move_at = 0.0
    global _merger_cancelled_at, _merger_late_destroy
    _merger_cancelled_at, _merger_late_destroy = 0.0, False
    _bloom_pending.clear()
    _omm_queue.clear()
    _death_stages.clear()
    _vehicle_table_paths.clear()
    _grade_cache.clear()                     # controller addresses are reused after a map change
    global _vehicle_scan_next, _player_count_cache
    _vehicle_scan_next = 0.0                 # the new map's tables at the first tick
    _player_count_cache = (0.0, 1)
    global _collab_due
    _collab_due = 0.0
    _own_beam_psc.clear()
    _hidden_mgr_psc.clear()
    _own_beam_attached.clear()
    _own_beam_push.clear()
    global _merger_prev_cam
    _merger_prev_cam = None
    _amp_pending.clear()               # paths from a world that has gone; nothing to pay them out of
    _amp_base.clear()
    _amp_boosted.clear()
    global _slag_fixed
    _slag_fixed = False                  # the package is reloaded per map with its cooked value back
    _amp_watch.clear()
    reset_commitment_loop()
    _screen_queue.clear()
    forget_action_skill_keys()
    from . import customs
    customs.forget_pawns()               # the next level's pawn gets its own grace before we touch it
    from . import progression
    progression.reset_session()
    diag.info(f"{TAG} session caches reset (level change)")


STACKS_RESET_AT_SKILL_END = ("Quince_Doppel_GreaterGood.Misc.Att_Collaborate_CurrentStacks",)


def on_skill_end(pc: UObject | None) -> None:
    fire("Leadership", "OzOnActionSkillEnd", pc=pc)
    # Collaborate's stacks live in a designer attribute the feedback skill only displays; TPS's
    # native action skill zeroed it at the end, otherwise the next cast shows the old count.
    if pc is not None and pc.Pawn is not None:
        for path in STACKS_RESET_AT_SKILL_END:
            set_attr(path, pc.Pawn, 0.0)
            set_attr(path, pc, 0.0)


BOLSTER_HEALTH_PER_GRADE = 0.06      # Att_Bolster_DigiJackHealthPerLevel
# Bolster's other half, "the longer a Digi-Jack is alive the more damage it does" (players: Bolster did nothing to
# the clones' damage, 2026-09-29). The clone damage skill scales Att_DigiJack_DamageBonusFromSkills (the wrist
# lasers' multiplier) by Init_Bolster_DamageBonus_Calc = Init_DigiJack_TimeAlive x Att_Bolster_TotalDamageBonus,
# with TimeAlive = D_Attributes.Time.CurrentTime - Att_DigiJack_SpawnTime in TPS. Borderlands 2 has no CurrentTime
# attribute (the port nulled it, so that term is 0) and TPS's native skill wrote the other two. So: SpawnTime is
# set to -1 at spawn, which makes TimeAlive exactly 1, and the total is written on every upkeep tick as
# Att_Bolster_DigiJackDamagePerLevel (0.02) x the Bolster grade x the seconds the clone has been alive.
BOLSTER_DAMAGE_PER_GRADE = 0.02      # Att_Bolster_DigiJackDamagePerLevel
ATT_BOLSTER_DAMAGE = "GD_Quince_JackClone.Misc.Att_Bolster_TotalDamageBonus"
ATT_DIGIJACK_SPAWN_TIME = "GD_Quince_JackClone.Misc.Att_DigiJack_SpawnTime"


def bolster_damage_tick(pawn: UObject, alive_seconds: float) -> None:
    """The clone's Bolster damage total for how long it has been alive (see BOLSTER_DAMAGE_PER_GRADE)."""
    if _bolster_grade_at_start <= 0 or pawn is None:
        return
    set_attr(ATT_BOLSTER_DAMAGE, pawn, BOLSTER_DAMAGE_PER_GRADE * _bolster_grade_at_start * max(0.0, alive_seconds))
BADASS_HEALTH_BONUS = 0.5            # Att_PromoteTheRanks_BadassHealthBonus (TPS flag resolver; additive with Bolster)
OPTIMISM_HEALTH_PER_DEATH = 0.05     # Att_Optimism_HealthReductionAmount, per stack of Att_Optimism_KillCount
ATT_OPTIMISM_KILLS = "Quince_Doppel_GreaterGood.Misc.Att_Optimism_KillCount"
OPTIMISM_MIN_HEALTH_FRACTION = 0.175 # TPS (user-measured): each wave spawns 10% lower (two deaths), never below 17.5%


_clone_deaths_this_cast = 0


def optimism_kill_count(pc: UObject | None = None) -> int:
    """Digi-Jack deaths this cast. Optimism's own graph keeps Att_Optimism_KillCount too, but its
    SimpleMath caps that at 7 (-35%) while TPS's spawn keeps going down to 11% health, so the
    count is ours (reset at cast)."""
    pc = pc or _cast_pc()
    if pc is None or grade("Optimism", pc) <= 0:
        return 0
    return _clone_deaths_this_cast


def clone_health_multiplier(badass: bool) -> float:
    """Health multiplier a clone gets over the base Digi-Jack; the decay tick is 10% of the base,
    so this is also ticks / 10: Bolster 5/5 -> 1.3, badass -> 1.5, both -> 1.8 (measured in TPS)."""
    return 1.0 + BOLSTER_HEALTH_PER_GRADE * _bolster_grade_at_start + (BADASS_HEALTH_BONUS if badass else 0.0)


def optimism_spawn_health_fraction() -> float:
    """Optimism in TPS (user-measured): the max health stays, the next Digi-Jacks spawn with health
    MISSING; 5% per death this cast (10% per wave, both die together), never below 17.5%."""
    kills = optimism_kill_count()
    if not kills:
        return 1.0
    frac = max(1.0 - OPTIMISM_HEALTH_PER_DEATH * kills, OPTIMISM_MIN_HEALTH_FRACTION)
    _log_once(f"optimism:hp:{kills}", f"Optimism: {kills} Digi-Jack death(s) this cast -> next clones spawn at {frac:.0%} health")
    return frac


ACTION_SKILL_DEF = "GD_Doppelganger_Skills.Action.Skill_DeathTrap"


def action_skill_start_time(pc: UObject | None) -> float:
    """StartTime of the live Skill instance behind the action skill (Best Foot Forward pushes it forward)."""
    global _skill_def_cache
    if pc is None:
        return 0.0
    if _skill_def_cache is None:
        _skill_def_cache = unrealsdk.find_object("SkillDefinition", ACTION_SKILL_DEF)
    manager = pc.GetSkillManager()
    for instigator in (pc, pc.Pawn):
        if instigator is None:
            continue
        try:
            sk = manager.GetActiveSkillForInstigatorByDefinition(SkillInstigator=instigator, Definition=_skill_def_cache)
        except Exception:
            sk = None
        if sk is not None:
            return float(sk.StartTime)
    return 0.0


def badass_roll(first: bool) -> bool:
    """Promote the Ranks: the first Digi-Jack of an activation is a badass, every later one has a chance."""
    pc = _cast_pc()
    g = grade("PromotetheRanks", pc)      # read live: in TPS speccing in mid-skill makes the next spawns roll
    if g <= 0:
        return False
    if first:
        return True
    chance = attr_value(ATT_BADASS_CHANCE, pc)
    if chance is None or chance <= 0:
        chance = BADASS_CHANCE_FALLBACK
    roll = random.random()
    diag.info(f"{TAG} Promote the Ranks: chance {chance:.2f}, roll {roll:.2f} -> {'badass' if roll < chance else 'regular'}")
    return roll < chance


_clone_slot = 0
_instdata_logged = 0
IDT_OBJECT = 4      # EInstanceDataType: Bool, Int, Float, Vector, Object, ...

# Skills the Digi-Jack graphs test with SkillExpressionEvaluator (AI: Diversify, Potential,
# OnMyMark, YouHaveMyShield, Optimism; BodyDeath: Collaborate, Optimism, YouHaveMyShield).
# BL2's evaluator asks the CONTEXT's own controller, so the clone has to have them active itself.
# NOT Collaborate: activating it on a clone's mind runs its OnActivated graph, which
# deactivates/activates its tracked HUD feedback skill on an AI controller and hangs the
# engine inside ActivateSkill (the "freeze on cast", traced with log_all_calls: the last
# calls were Behavior_DeactivateSkill_30 -> WillowMind.Behavior_DeactivateSkill on the
# clone's mind). Its only clone-side use is BodyDeath's CloneDied check, raised from Python.
CLONE_CHECKED_SKILLS = ("Potential", "Optimism", "Diversify", "YouHaveMyShield", "OnMyMark")


def activate_clone_side_skills(pawn: UObject) -> None:
    """
    Before the clone's behavior providers initialise (their OnSpawned / sequence-enabled
    conditionals run then): activate Skill_DigiJack_DamageBonuses plus every clone-checked
    skill the player has points in, with the clone's mind as instigator and the player's grade.
    """
    pc = _cast_pc()
    if pc is None or pawn.Controller is None:
        _log_once("cloneskills:noctrl", "clone-side skills: no controller yet")
        return
    manager = pc.GetSkillManager()
    ctrl = pawn.Controller

    def already(d):
        try:
            return bool(manager.IsSkillActive(SkillInstigator=ctrl, Definition=d))
        except Exception:
            return False

    try:
        d = unrealsdk.find_object("SkillDefinition", CLONE_DAMAGE_BONUS_SKILL)
        if already(d):
            _log_once("dmgbonus:dup", "Skill_DigiJack_DamageBonuses was already active on a fresh clone (not re-activated)")
        else:
            ok = manager.ActivateSkill(Definition=d, SkillInstigator=ctrl)
            _log_once("dmgbonus:ok", f"Skill_DigiJack_DamageBonuses activated on a clone (returned {ok})")
        n = 0
        for sk in manager.ActiveSkills:
            try:
                if sk.Definition is not None and sk.Definition.Name == d.Name and sk.SkillInstigator is not None and sk.SkillInstigator.Pawn is not None and sk.SkillInstigator.Pawn.Name == pawn.Name:
                    n += 1
            except Exception:
                pass
        if n != 1:
            if diag.enabled:
                diag.info(f"{TAG} Skill_DigiJack_DamageBonuses instances on {pawn.Name}: {n}")
    except Exception:
        _log_once("dmgbonus:fail", "ActivateSkill(Skill_DigiJack_DamageBonuses) failed: " + traceback.format_exc().splitlines()[-1])
    done = []
    for leaf in CLONE_CHECKED_SKILLS:
        g = grade(leaf, pc)
        if g <= 0:
            continue
        d = skill_def(leaf)
        if d is None or already(d):
            continue
        try:
            manager.ActivateSkill(Definition=d, SkillInstigator=ctrl, SkillGrade=g)
            done.append(f"{leaf}:{g}")
        except Exception:
            _log_once("cloneskill:" + leaf, f"ActivateSkill({leaf}) on a clone failed: " + traceback.format_exc().splitlines()[-1])
    if done:
        _log_once("cloneskills:" + ",".join(done), f"clone-side skills activated: {done}")


# AI flags the Digi-Jack graphs would set from skill checks on OnBehaviorSequenceEnabled;
# that event's zero-delay links never run on our clones (traced 2026-09-14: Conditional_31's
# +0.3 s link fires, Conditional_29's immediate one does not), so the flags are set here.
FLAG_SHOTGUN = "GD_Quince_JackClone.Flags.HasShotgunSkill"      # Diversify: the wrist lasers become shotgun blasts
CLONE_SKILL_FLAGS = (("Diversify", FLAG_SHOTGUN, True),)


def set_flag(path: str, ctx: UObject, value: bool) -> bool:
    """FlagDefinition.SetValue(ContextSource, Value); the flag's own resolver chain maps a pawn to its mind."""
    try:
        flag = unrealsdk.find_object("FlagDefinition", path)
        return bool(flag.SetValue(ContextSource=ctx, Value=bool(value)))
    except Exception:
        _log_once("flag:" + path, f"SetValue({path}) failed: " + traceback.format_exc().splitlines()[-1])
        return False


def apply_clone_flags(pawn: UObject) -> None:
    """After InitializeBehaviorProviders(): the per-skill AI flags (see CLONE_SKILL_FLAGS)."""
    pc = _cast_pc()
    if pc is None:
        return
    for leaf, path, value in CLONE_SKILL_FLAGS:
        if grade(leaf, pc) > 0:
            ok = set_flag(path, pawn, value)
            _log_once(f"cloneflag:{leaf}:{ok}", f"{leaf}: {path.split('.')[-1]}={value} on the clone (returned {ok})")


_spawn_hook_t = 0.0


def on_clone_spawned(pawn: UObject, badass: bool, action_skill: UObject | None = None, side: float = 0.0) -> None:
    """
    What TPS's native action skill did per clone: activate Skill_DigiJack_DamageBonuses on
    it, register it as the JackClone / JackClone2 instance data the skill graphs address
    (Take Their Freedom heals them by that name), and copy the player's shield to it when
    You Have My Shield is skilled (BL2's own DeathtrapActionSkill.TryToShareShields, done
    for our hand-spawned pawns).
    """
    global _clone_slot, _spawn_hook_t
    _spawn_hook_t = time.perf_counter()
    diag.crumb("on_clone_spawned: in")
    pc = _cast_pc()
    if pc is None:
        return
    name = "JackClone" if side < 0 else "JackClone2"
    for holder, label in ((pc, "controller"), (action_skill, "action skill")):
        if holder is None:
            continue
        try:
            # Replace, not append: nothing ever removed a dead clone's entry, and with
            # bAllowDuplicateNames=False the replacement's registration may leave the dead one
            # in place, so every graph addressing JackClone would see a corpse after the first
            # pair (Delegation reported dead, 2026-09-25). TPS's action skill removes its entries.
            try:
                holder.RemoveInstanceData(name)
            except Exception:  # noqa: BLE001
                pass
            data = unrealsdk.make_struct("InstanceDataUnion", Name=name, Type=IDT_OBJECT, Object=pawn)
            r = holder.SetInstanceData(InstanceData=data, bAllowDuplicateNames=False)
            global _instdata_logged
            if _instdata_logged < 8:
                _instdata_logged += 1
                unrealsdk.logging.info(f"{TAG} instance data {name} -> {pawn.Name} on the {label} (returned {r})")
        except Exception:
            _log_once(f"instdata:fail:{label}", f"SetInstanceData({name}) on the {label} failed: " + traceback.format_exc().splitlines()[-1])
    diag.crumb("on_clone_spawned: instance data done")
    t = diag.section("skills.spawn instance data", _spawn_hook_t)
    if grade("YouHaveMyShield", pc) > 0:
        diag.crumb("on_clone_spawned: share_shield in")
        if YHMS_SHARE:
            share_shield(pc, pawn)
        diag.crumb("on_clone_spawned: share_shield out")
    else:
        _fill_bonus_shield(pawn)
    diag.crumb("on_clone_spawned: shield done")
    t = diag.section("skills.spawn shield", t)
    if badass:
        arm_badass(pawn)
        try:                                     # TPS: the badass spawn branches trigger the Promote lines on Jack himself
            from . import digijack
            if pc.Pawn is not None and digijack._trigger_dialog(pc.Pawn, digijack.SKILL_PROMOTE_LINE_TAG, "Promote the Ranks line"):
                n = _fired.get("badass:line", 0) + 1
                _fired["badass:line"] = n
                if n <= 3:
                    if diag.enabled:
                        diag.info(f"{TAG} Promote the Ranks line triggered on {pc.Pawn.Name}")
        except Exception:
            _log_once("badass:line", "Promote the Ranks line failed: " + traceback.format_exc().splitlines()[-1])
    # Bolster's clone-side total (TPS's native skill wrote it from the skill's holder effect);
    # feeds the clone damage-bonus skill's damage formula. Health is enforced by digijack.
    bolster = BOLSTER_HEALTH_PER_GRADE * _bolster_grade_at_start
    if bolster > 0:
        set_attr("GD_Quince_JackClone.Misc.Att_Bolster_TotalHealthBonus", pawn, bolster)
        set_attr(ATT_DIGIJACK_SPAWN_TIME, pawn, -1.0)      # TimeAlive = 0 - (-1) = 1: the damage total carries the time
        bolster_damage_tick(pawn, 0.0)
    diag.crumb("on_clone_spawned: bolster done")
    diag.section("skills.spawn badass+bolster", t)
    # Collaborate's share is on the clone already: prime_clone_collaborate ran before its skills were applied
    diag.crumb("on_clone_spawned: out")


# TPS: the badass clone's AI tree (AIDef_DigiJack_BaseAndBadass, priority node children
# 'BadassBlast Up' / 'BadassBlast Front') plays Anim_DigiJack_BadassBlast(_Front); the anim's timed
# event spawns Projectile_DigiJack_BadassShot_Parent (straight up / forward), which detonates on a
# timer into homing BadassShot_Child missiles. The tree gates on AI flags: IsBadassDigiJack (set by
# CharClass_DigiJack_Badass's DesignerFlagStartingValues at mind init) and CanFireUp (set by the AI
# graph's spawn trace); neither reliably reaches our hand-spawned clones, so they are set here.
FLAG_IS_BADASS = "GD_Quince_JackClone.Flags.IsBadassDigiJack"
FLAG_CAN_FIRE_UP = "GD_Quince_JackClone.Flags.CanFireUp"
BADASS_SHOT_PARENT = "GD_Quince_JackClone.Projectiles.Projectile_DigiJack_BadassShot_Parent"
BADASS_SHOT_CHILD = "GD_Quince_JackClone.Projectiles.Projectile_DigiJack_BadassShot_Child"
BADASS_FIXUPS = (
    # TPS-only assets on the barrage projectiles -> BL2 stand-ins (Behavior_Explode_9's source was OzWillowDmgSource_Laser).
    # (v60-v64 put BL2 rocket trails on InFlightEffects; v65 restores TPS's comet, see BADASS_FX_MODE.)
    ("Behavior_Explode", BADASS_SHOT_PARENT + ".BehaviorProviderDefinition_0.Behavior_Explode_9", "DamageSource", "class:WillowDmgSource_Skill"),
    ("Behavior_SpawnParticleSystem", BADASS_SHOT_PARENT + ".BehaviorProviderDefinition_0.Behavior_SpawnParticleSystem_8", "ParticleEffect", "ParticleSystem:FX_WEP_Launchers.Particles.Part_Launcher_Muzzle_Incendiary"),
)
_badass_fixed = False
BADASS_COMET = "Quince_FX.Particles.Part_BadassShot_Fire"       # v65: TPS's Part_FinalBoss_MortarProjectile_Fire, the comet both barrage projectiles carry
BADASS_FX_MODE = "psc"                                         # "psc": the cooked body-composition component (as TPS); "inflight": BL2's InFlightEffects slot; "both"; `doppelskill barragefx x`


def _badass_fixups() -> None:
    global _badass_fixed
    if _badass_fixed:
        return
    notes = []
    try:                                     # the comet: on the projectiles' own component (cooked in v65) and/or BL2's in-flight slot
        comet = unrealsdk.find_object("ParticleSystem", BADASS_COMET)
        for path in (BADASS_SHOT_PARENT, BADASS_SHOT_CHILD):
            pdef = unrealsdk.find_object("ProjectileDefinition", path)
            pdef.InFlightEffects = comet if BADASS_FX_MODE in ("inflight", "both") else None
            for att in pdef.BodyComposition.Attachments:
                comp = att.Data.ComponentData.Component
                if comp is not None and comp.Class.Name == "ParticleSystemComponent":
                    comp.Template = comet if BADASS_FX_MODE in ("psc", "both") else None
        notes.append(f"comet mode {BADASS_FX_MODE}")
    except Exception as e:
        notes.append(f"comet failed: {str(e)[:90]}")
    for cls, leaf, prop, factor, floor in COMET_TONE:     # once: the tables persist (rooted), the floor check keeps a re-run from compounding
        try:
            mod = unrealsdk.find_object(cls, BADASS_COMET + "." + leaf)
            table = getattr(mod, prop).LookupTable
            if len(table) > 1 and table[1] >= floor:
                for i in range(len(table)):
                    table[i] = table[i] * factor
                notes.append(f"{leaf} x{factor}")
        except Exception as e:
            notes.append(f"{leaf} tone failed: {str(e)[:80]}")
    try:
        comet = unrealsdk.find_object("ParticleSystem", BADASS_COMET)
        for em in comet.Emitters:
            if em is not None and str(em.Name) in COMET_DISABLED:
                for lod in em.LODLevels:
                    if lod is not None and lod.bEnabled:
                        lod.bEnabled = False
                        notes.append(f"{em.Name} disabled")
    except Exception as e:
        notes.append(f"comet emitters: {str(e)[:80]}")
    for cls, path, prop, spec in BADASS_FIXUPS:
        try:
            obj = unrealsdk.find_object(cls, path)
            kind, target = spec.split(":", 1)
            value = unrealsdk.find_class(target) if kind == "class" else unrealsdk.find_object(kind, target)
            if getattr(obj, prop) is None:
                setattr(obj, prop, value)
                notes.append(f"{path.split('.')[-1]}.{prop}")
        except Exception as e:
            notes.append(f"{path.split('.')[-1]}.{prop} failed: {str(e)[:70]}")
    for path in (BADASS_SHOT_PARENT, BADASS_SHOT_CHILD):
        try:                                     # BL2's rocket trails are local-space emitters: the projectile must turn with its velocity
            pdef = unrealsdk.find_object("ProjectileDefinition", path)
            if not pdef.bAlwaysOrientInDirectionOfMovement:
                pdef.bAlwaysOrientInDirectionOfMovement = True
                notes.append(f"{path.split('.')[-1]}.bAlwaysOrientInDirectionOfMovement")
        except Exception as e:
            notes.append(f"{path.split('.')[-1]} orient failed: {str(e)[:70]}")
    _badass_fixed = True
    diag.info(f"{TAG} badass barrage fix-ups: {notes}")


def arm_badass(pawn: UObject) -> None:
    """A Promote the Ranks badass: the AI flags its missile-barrage attacks are gated on."""
    _badass_fixups()
    try:
        if not _fx_applied:                  # it walked every FX particle system for every badass spawn
            _log_once("badass:fxmat", "barrage FX materials: " + _fx_apply_materials())   # the comet's runtime materials, before the first barrage
    except Exception:
        _log_once("badass:fxmat:fail", "barrage FX materials failed: " + traceback.format_exc().splitlines()[-1])
    ok1 = set_flag(FLAG_IS_BADASS, pawn, True)
    ok2 = set_flag(FLAG_CAN_FIRE_UP, pawn, True)
    n = _fired.get("badass:armed", 0) + 1
    _fired["badass:armed"] = n
    if n <= 4:
        if diag.enabled:
            diag.info(f"{TAG} badass {pawn.Name}: IsBadassDigiJack={ok1} CanFireUp={ok2} (Digi-Missile Barrage armed)")


_badass_probe = 0


COMET_EMITTERS = {"ribbon": ("ParticleSpriteEmitter_14", "ParticleSpriteEmitter_1"), "flare": ("ParticleSpriteEmitter_11",),
                  "embers": ("ParticleSpriteEmitter_12",), "burst": ("ParticleSpriteEmitter_2",), "bolts": ("ParticleSpriteEmitter_3",)}


def _comet_enable(names) -> str:
    """Diagnostic: enable only the named comet emitters (ParticleLODLevel.bEnabled), `all` for every one."""
    try:
        ps = unrealsdk.find_object("ParticleSystem", BADASS_COMET)
    except Exception:
        return "comet not resident"
    wanted = set()
    for n in names:
        if n == "all":
            wanted.update(e for group in COMET_EMITTERS.values() for e in group)
        else:
            wanted.update(COMET_EMITTERS.get(n, ()))
    state = []
    for em in ps.Emitters:
        if em is None:
            continue
        on = str(em.Name) in wanted
        for lod in em.LODLevels:
            if lod is not None:
                lod.bEnabled = on
        state.append(f"{em.Name}={'on' if on else 'off'}")
    return ", ".join(state)


def _fx_dump(ps_leaf: str) -> str:
    """Every emitter of the named particle system: LOD required-module materials, their parents and textures."""
    ps = None
    for ps_path in FX_PARTICLE_EMITTERS:
        if ps_path.endswith("." + ps_leaf):
            try:
                ps = unrealsdk.find_object("ParticleSystem", ps_path)
            except Exception:
                return f"{ps_path} not resident"
    if ps is None:
        return f"{ps_leaf}: not a listed particle system"
    lines = []
    for em in ps.Emitters:
        if em is None:
            continue
        for k, lod in enumerate(em.LODLevels):
            req = lod.RequiredModule if lod is not None else None
            mat = req.Material if req is not None else None
            parent = None
            texs = []
            try:
                parent = mat.Parent
                for tp in mat.TextureParameterValues:
                    tex = tp.ParameterValue
                    if tex is None:
                        texs.append(f"{tp.ParameterName}=None")
                        continue
                    mips = ""
                    try:
                        mips = f" {tex.SizeX}x{tex.SizeY} resident={tex.ResidentMips} requested={tex.RequestedMips} forced={bool(tex.bForceMiplevelsToBeResident)}"
                    except Exception:
                        pass
                    texs.append(f"{tp.ParameterName}={tex.Name}{mips}")
            except Exception:
                pass
            lines.append(f"{em.Name}[{em.EmitterName}] lod{k}: {mat._path_name() if mat is not None else None} parent={parent.Name if parent is not None else None} {texs}")
    return "; ".join(lines)


@hook("WillowGame.Behavior_SpawnProjectile:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_badass_blast_pre")
def _on_badass_blast_pre(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_badass_blast_pre")
    global _delegation_capture
    try:
        outer = obj.Outer._path_name()
        if outer.replace(":", ".") == DELEGATION_BPD:
            _delegation_capture = []         # its bolts, as they spawn inside this call (see _on_delegation_spawn)
            return
        if "BadassBlast" not in outer and "BadassShot" not in outer:
            return
        if _fx_applied:
            return                           # on the templates already, until the next map (reset_session); every shot walked them (0.44 ms)
        how = _fx_apply_materials()          # the comet's runtime materials must be on the template before the instance activates
        n = _fired.get("badass:fxpre", 0) + 1
        _fired["badass:fxpre"] = n
        if n <= 2:
            diag.info(f"{TAG} barrage FX before {obj.Name}: {how}")
            diag.info(f"{TAG} comet emitters: {_fx_dump('Part_BadassShot_Fire')}")
    except Exception:
        _log_once("badass:fxpre", "barrage FX pre-hook failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.Behavior_SpawnProjectile:ApplyBehaviorToContext", Type.POST, hook_identifier="doppel_badass_blast")
def _on_badass_blast(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_badass_blast")
    global _badass_probe
    try:
        outer = obj.Outer._path_name()
        if "BadassBlast" not in outer and "BadassShot" not in outer:
            return
        if _badass_probe < 12:
            _badass_probe += 1
            ctx = args.ContextObject
            diag.info(f"{TAG} badass barrage: {obj.Name} ({outer.split('.')[-2]}) on {ctx.Name if ctx is not None else None} -> {obj.ProjectileDefinition.Name if obj.ProjectileDefinition is not None else None}")
    except Exception:
        pass


def _fill_bonus_shield(pawn: UObject) -> None:
    """A team shield-capacity bonus (Legendary Protagonist) reaches the Digi-Jacks; without You Have
    My Shield they own no shield, so the bonus alone showed as an empty bar (2026-09-15). Start them
    with it full instead."""
    try:
        if pawn.GetMaxShieldStrength() > 0 and pawn.GetShieldStrength() <= 0:
            pawn.FullyReplenishShields()
            _log_once("shield:bonus", f"clone shield capacity from gear ({pawn.GetMaxShieldStrength():.0f}) filled at spawn")
    except Exception:
        _log_once("shield:bonusfail", "bonus shield fill failed: " + traceback.format_exc().splitlines()[-1])


def share_shield(pc: UObject, pawn: UObject) -> None:
    """You Have My Shield: give the clone a copy of the player's equipped shield (TryToShareShields' recipe)."""
    try:
        player = pc.Pawn
        for item in list(player.EquippedItems):
            if item is None or "Shield" not in str(item.Class.Name):
                continue
            diag.crumb("share_shield: CreateClone in")
            copy = item.CreateClone()
            diag.crumb("share_shield: CreateClone out")
            if copy is None:
                _log_once("shield:none", "You Have My Shield: CreateClone returned None")
                return
            try:
                copy.bDropOnDeath = False
            except Exception:
                pass
            diag.crumb("share_shield: GiveTo in")
            copy.GiveTo(Other=pawn, bReady=True, bPlayPickupSound=False)
            diag.crumb("share_shield: GiveTo out")
            scale_copy_nova(copy)
            try:
                _shield_copies[pawn._path_name()] = copy._path_name()
            except Exception:  # noqa: BLE001
                pass
            _log_once("shield:ok", f"You Have My Shield: {item.Class.Name} copied to a clone; clone shield now {pawn.GetMaxShieldStrength():.0f}")
            return
        _log_once("shield:noitem", "You Have My Shield: no shield equipped")
    except Exception:
        _log_once("shield:fail", "You Have My Shield failed: " + traceback.format_exc().splitlines()[-1])


# Each clone's copy of the player's shield, and its removal (user, 2026-09-26: with You Have My
# Shield specced, travelling with clones out ended in an infinite load or Jack falling through the
# floor). BL2's TryToShareShields makes the same copy for Deathtrap (IsSkillActive(ShareShieldsSkill),
# CreateClone, bDropOnDeath = False, GiveTo; read off its bytecode) and nothing removes it because
# Deathtrap is destroyed and the copy with it. Our clones refuse Destroy() and linger as zombies, so
# every Digi-Jack of a cast, replacements included, left a live copy of the player's shield in the
# world, and the travel ran with all of them. The copy now goes when its clone is cleaned up, and the
# live clones' copies just before a travel or a save and quit. YHMS_SHARE off (doppelskill yhmsshare)
# skips the copy entirely, for telling our copy apart from the rest of the skill.
#
# The first version of this never removed anything (still infinite loads, 2026-09-26). The traced
# fast travel showed why, twice over: Destroy() from Python never runs (see release_shield_copy), and
# fast travel never calls PreClientTravel, so the travel release never fired either. The old map goes
# out through PrepareMapChange/CommitMapChange, and in that trace the travel came 3 s after the cast,
# with both clones alive and each copy's Chimera skill running on its clone's mind in Docks_P. During
# the commit the Docks_P minds went, and from then on nothing that walks WorldInfo's controller list
# found Jack's controller: AllowPausing, the FakeEntry SeqAct_Teleport and
# TeleportToFinalDestinationAfterLoad all skipped him, so he was never put at the station (falling
# through the floor) and the loading movie was never taken down (infinite load). The release now runs
# at GameInfo.PreCommitMapChange (digijack), where the population system destroys the map's own AI.
YHMS_SHARE = True
_shield_copies: dict = {}        # clone pawn path -> its shield copy's path


def release_shield_copy(pawn_path: str | None, why: str) -> str:
    path = _shield_copies.pop(pawn_path or "", None)
    if not path:
        return "none"
    try:
        item = unrealsdk.find_object("WillowShield", path)
    except Exception:  # noqa: BLE001
        item = None
    if item is None:
        return "already gone"
    # Behavior_Destroy, not Destroy. Destroy is native(279), and the SDK calls through ProcessEvent,
    # which drops any function with a native index without running it: the 2026-09-26 calls trace
    # has our Destroy() on both copies with no Destroyed after it. Actor.Behavior_Destroy is plain
    # script, `if (Role == ROLE_Authority) Destroy();`, and nothing between Actor and WillowShield
    # overrides it.
    try:
        item.Behavior_Destroy()
        gone = bool(item.bDeleteMe)
    except Exception:  # noqa: BLE001
        _log_once("shield:release", "You Have My Shield: removing a clone's shield copy failed: "
                  + traceback.format_exc().splitlines()[-1], always=True)
        return "failed"
    return f"destroyed ({path.split('.')[-1]} {'gone' if gone else 'STILL THERE'})"


def release_all_shield_copies(why: str) -> int:
    n, results = 0, []
    for pawn_path in list(_shield_copies):
        r = release_shield_copy(pawn_path, why)
        results.append(r)
        if r.startswith("destroyed"):
            n += 1
    if results:
        unrealsdk.logging.info(f"{TAG} You Have My Shield: {n} of {len(results)} clone shield "
                               f"cop{'y' if len(results) == 1 else 'ies'} removed ({why}): {', '.join(results)}")
    return n


# You Have My Shield hands the clone a copy of the item, which is how BL2 shares a shield with
# Deathtrap, so a nova, spike or amp should fire from the clone the same way it does from Gaige's:
# those behaviours live on the shield's own parts and are driven by the holder's shield events, not
# by anything the player's pawn does. What is worth checking is whether our hand-spawned pawns end
# up carrying the item the way a population-spawned Deathtrap does - the capacity actually applied,
# and the parts that carry the behaviour actually present (user, 2026-09-23). `doppelskill shield`
# prints both for the player and every clone that is out.
# --- Winning: the kill-skill shield restore -------------------------------------------------------
# Killing an enemy should hand back a slice of the shield. The graph is a faithful port (diffed
# against TPS on 2026-09-23: the behaviour graph, the restore calc and the per-level attribute are
# all identical, and the ShieldMaxValue_Player repoint from 2026-09-13 is in place and resolves).
# The restore is Init_Winning_ShieldRestore_Calc = Att_Winning_ShieldRestorePerLevel x
# SkillGradeModifiers.Winning, so a zero in either term is a restore of nothing, which is what the
# user sees. These are the live terms.
WINNING_TERMS = (
    ("grade attribute", "Quince_Doppel_GreaterGood.SkillGradeModifiers.Winning"),
    ("restore per level", "Quince_Doppel_GreaterGood.Misc.Att_Winning_ShieldRestorePerLevel"),
    ("shield current", "D_Attributes.ShieldResourcePool.ShieldCurrentValue"),
    ("shield max", "D_Attributes.ShieldResourcePool.ShieldMaxValue"),
)


# --- Winning: the restore on a refreshed kill skill ------------------------------------------------
# Winning's shield restore lives on its behaviour graph's OnActivated event, so it only runs when
# the skill turns on. Kill something while it is already running and the kill only refreshes the
# duration - OnActivated does not fire again and no shield comes back (user, 2026-09-23). Its own
# numbers confirm the arithmetic: `doppelskill winning` read the grade attribute as 0.0 with the
# skill down and 5.0 with it up, and the restore is ShieldMaxValue x per-level x grade.
#
# So the refresh case is done here, and only the refresh case: at PRE the skill has not been turned
# on for this kill yet, so a grade attribute above zero means it was already running and the graph
# will not do it. A fresh activation is left alone and restores itself as it always did.
WINNING_SKILL = "Quince_Doppel_GreaterGood.Skills.Winning"
WINNING_GRADE_ATTR = "Quince_Doppel_GreaterGood.SkillGradeModifiers.Winning"
WINNING_PER_LEVEL = "Quince_Doppel_GreaterGood.Misc.Att_Winning_ShieldRestorePerLevel"
SHIELD_CURRENT_ATTR = "D_Attributes.ShieldResourcePool.ShieldCurrentValue"
_winning_pending: dict = {}      # pawn path -> shield to restore (co-op: each Jack's own)
_winning_logged = 0
_winning_paid = 0


# Not SkillDefinition:OnKilledEnemy - that never fires for Jack's skills at all (the mod's own
# probe on it logged nothing in a whole session, 2026-09-23). The enemy's own death does.
@hook("WillowGame.WillowAIPawn:Died", Type.PRE, hook_identifier="doppel_winning_refresh")
def _on_winning_kill(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    global _winning_pending, _winning_logged
    diag.hcrumb("skills._on_winning_kill")
    try:
        pc = _player_controller_of(args.Killer)          # a Jack player's own kill (a clone's controller is not one)
        if pc is None:
            return
        queue_winning_refresh(pc, "kill")
    except Exception:  # noqa: BLE001
        _log_once("winning:kill", "Winning refresh restore failed: " + traceback.format_exc().splitlines()[-1])


def queue_winning_refresh(pc: UObject, why: str) -> str:
    """The refresh case of Winning's restore, for any event that refreshes the kill skill: the
    player's own kill (the Died hook above) and, since 2026-09-25, a Digi-Jack's death under
    Leadership (digijack -> on_clone_died), which the Died hook never counted because the clone's
    killer is an enemy or nobody. Call it BEFORE the kill skills are (re)started: a grade above zero
    means the skill was already running, so its OnActivated will not fire and the restore is ours;
    zero means a fresh activation, which restores itself through the graph."""
    global _winning_pending, _winning_logged
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return "no pawn"
    g = attr_value(WINNING_GRADE_ATTR, pawn) or 0.0
    if g <= 0.0:
        return "not running: the activation restores itself"
    per = attr_value(WINNING_PER_LEVEL, pawn) or 0.0
    mx = float(pawn.GetMaxShieldStrength())
    add = mx * per * g
    if add <= 0.0:
        return "restore of nothing"
    # Queued, not applied here: putting shield on from inside a kill callback would fire the
    # shield's own events inside the engine's event, which is the shape of the 2026-09-23 hang.
    key = pawn._path_name()
    _winning_pending[key] = _winning_pending.get(key, 0.0) + add
    note = f"{why} with the skill already up, restoring {add:.0f} shield ({per:.0%} x grade {g:.0f} of {mx:.0f})"
    if _winning_logged < 8:
        _winning_logged += 1
        diag.detail(f"{TAG} Winning: {note}")
    return note


def winning_tick() -> None:
    """Pay the queued Winning restore, from the tick rather than from inside the kill event."""
    if not _winning_pending:
        return
    owed = dict(_winning_pending)
    _winning_pending.clear()
    for key, add in owed.items():
        try:
            pawn = unrealsdk.find_object("WillowPlayerPawn", key)
        except Exception:  # noqa: BLE001
            pawn = None
        if pawn is not None and add > 0.0:
            _winning_pay(pawn, add)


def _winning_pay(pawn: UObject, add: float) -> None:
    try:
        global _winning_paid
        mx = float(pawn.GetMaxShieldStrength())
        cur = float(pawn.GetShieldStrength())
        if cur >= mx:
            if _winning_paid < 5:
                _winning_paid += 1
                diag.detail(f"{TAG} Winning: shield already full ({cur:.0f}/{mx:.0f}), "
                            f"nothing restored - any recharge pause here is not ours")
            return
        want = min(mx, cur + add)
        # Through the pool attribute, which is what Winning's own graph writes (its SimpleMath adds
        # to ShieldCurrentValue). SetShieldStrength cuts an in-progress recharge short, which the
        # engine's own restore does not do (user, 2026-09-23).
        via = "ShieldCurrentValue" if set_attr(SHIELD_CURRENT_ATTR, pawn, want) else "SetShieldStrength"
        if via != "ShieldCurrentValue":
            pawn.SetShieldStrength(ShieldValue=want)
        if _winning_paid < 5:
            _winning_paid += 1
            diag.detail(f"{TAG} Winning: restore written through {via} "
                        f"({cur:.0f} -> {want:.0f} of {mx:.0f})")
    except Exception:  # noqa: BLE001
        _log_once("winning:pay", "Winning restore failed: " + traceback.format_exc().splitlines()[-1])


def winning_report(pc: UObject) -> list:
    lines = [f"Winning: tree grade {grade('Winning', pc)}"]
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return lines + ["  no pawn"]
    vals = {}
    for label, path in WINNING_TERMS:
        v = attr_value(path, pawn)
        vals[label] = v
        lines.append(f"  {label}: {v}")
    g, per = vals.get("grade attribute"), vals.get("restore per level")
    if g is not None and per is not None:
        lines.append(f"  -> restore would be {per} x {g} = {per * g:.1f} shield per kill"
                     f"{' (nothing, because a term is zero)' if per * g == 0 else ''}")
    return lines


# --- Sponsored By: which branch a gun actually takes ----------------------------------------------
# The Maliwan bonus deals a second helping of the gun's own element: the skill's DamagedEnemy event
# runs Behavior_Conditional_19, whose four conditions read D_Attributes.WeaponDamageType
# .WeaponDamage_Is_<element> and fire CE_DoDamage_<element>, and each of those ends in a
# Behavior_CauseDamage carrying that element's impact damage type.
#
# The port of that graph is complete for Borderlands 2: TPS's ice condition (WeaponDamage_Is_Ice)
# is already WeaponDamage_Is_Amp here and its CauseDamage already carries GD_Amp.DamageType
# .DmgType_Amp_Impact, which is slag - "Amp" is what Borderlands 2 calls slag throughout, and its
# resolver matches DmgType_Amp_Impact, the type slag guns really use. So nothing in the data
# explains slag doing nothing (user, 2026-09-23), and this prints the live numbers instead: which
# element attribute the equipped gun actually raises, and whether the Maliwan flag is up.
ELEMENT_ATTRS = ("Incendiary", "Shock", "Corrosive", "Amp", "Explosive")
MALIWAN_ON = "Quince_Doppel_FreeEnterprise.Misc.Att_SponsoredBy_MaliwanOn"


# --- Sponsored By: the slag element condition ------------------------------------------------------
# Sponsored By's element conditional has one AttributeExpressionEvaluator per element. The slag one
# reads D_Attributes.WeaponDamageType.WeaponDamage_Is_Amp through an import whose ClassPackage says
# WillowGame, but AttributeDefinition lives in Engine - so UE3 cannot resolve the import's class and
# the reference comes back None. The condition can then never be true and every slag shot falls
# through to the Normal branch, which is exactly what the game did while the other four elements
# worked (found 2026-09-23 with upk_check_imports.py, after the package looked byte-perfect).
#
# Fixed here rather than in the package: the 4-byte edit to the import table is undone by the
# in-game asset build, which rebuilds that file from its delta. Repointing the live object costs
# nothing and survives the rebuild.
SLAG_ATTR = "D_Attributes.WeaponDamageType.WeaponDamage_Is_Amp"
# _path_name writes subobjects as Outer:Sub.Sub, which is the form find_object wants.
SPONSORED_ELEMENT_COND = ("Quince_Doppel_FreeEnterprise.Skills.SponsoredBy"
                          ":BehaviorProviderDefinition_0.Behavior_Conditional_19")
_slag_fixed = False


def fix_sponsored_slag() -> str:
    """Give the slag condition back its attribute. Once per session; no-op when it is already set."""
    global _slag_fixed
    if _slag_fixed:
        return ""
    try:
        cond = unrealsdk.find_object("Behavior_Conditional", SPONSORED_ELEMENT_COND)
    except Exception:  # noqa: BLE001
        cond = None
    if cond is None:
        return ""                        # the skill's package is not resident yet
    slag = _attr(SLAG_ATTR)
    if slag is None:
        return ""
    _slag_fixed = True
    try:
        fixed = []
        conditions = [c for c in cond.Conditions]      # a new list: the property refuses its own
        for i, c in enumerate(conditions):
            ev = getattr(c, "Condition", None)
            if ev is None:
                continue
            expr = ev.Expression
            if expr.AttributeOperand1 is not None:
                continue
            expr.AttributeOperand1 = slag
            ev.Expression = expr
            fixed.append(int(c.ConditionId))
        if not fixed:
            return "Sponsored By: every element condition already had its attribute"
        return (f"Sponsored By: gave condition(s) {fixed} back WeaponDamage_Is_Amp "
                f"(the import's ClassPackage names the wrong package, so it loaded as None)")
    except Exception:  # noqa: BLE001
        return "Sponsored By: slag condition fix failed: " + traceback.format_exc().splitlines()[-1]


# --- Sponsored By: raising its DamagedEnemy event ourselves ----------------------------------------
# The skill's manufacturer bonus is driven by a DamagedEnemy damage event on the SkillDefinition;
# its graph then picks the element (Behavior_Conditional_19 reading WeaponDamage_Is_<element>) and
# runs the matching Behavior_CauseDamage. Every part of that is correct in the package - with a slag
# gun in hand WeaponDamage_Is_Amp measured 1.0, and the slag branch has the same damage inputs as
# the shock branch. It simply never runs: a whole session of probes caught the conditional 0 times
# and the five CauseDamage nodes 0 times (2026-09-23). Borderlands 2 is not raising the event for an
# imported skill, the same way SkillDefinition:OnKilledEnemy never fires for Jack's skills either.
#
# So it is raised here, through the same BehaviorKernel route the mod already uses for TPS's Oz*
# events, with the enemy handed over as the event's output so the damage lands on it.
SPONSORED_DAMAGE_SOURCES = ("Bullet", "Rocket")     # the event's own constraints
_sponsored_raised = 0
_sponsored_guard = False

# ...and only here. The chain's first node (Behavior_CompareFloat_10, the Maliwan flag test) is let through only
# inside one of our raises: players reported the Maliwan second helping on grenades, shield novas and the Orphan
# Maker's self-damage (2026-09-29). The element is read from the gun in hand, not from what did the damage, so
# any other raise of DamagedEnemy while a Maliwan gun is held becomes a bonus hit; the skill's own event
# constraints include TPS's OzWillowDmgSource_Laser, a class Borderlands 2 does not have, and a constraint whose
# source did not resolve matches every source. Our raise covers every gun hit already, so nothing is lost.
SPONSORED_BPD = "Quince_Doppel_FreeEnterprise.Skills.SponsoredBy.BehaviorProviderDefinition_0"
SPONSORED_ENTRY = "Behavior_CompareFloat_10"
SPONSORED_RAISE_WINDOW = 0.05       # the chain runs inside our raise; anything later was raised by something else
_sponsored_raise_at = 0.0
_sponsored_blocked = 0
_last_pc_damage = ("", "", "")       # (source, causer class, target) of the player's latest hit, for the log


# Sponsored By's manufacturer bonus works for every element except slag (user, 2026-09-23), which
# rules out the whole graph being dead and rules out my reading that the bonus is the parent's
# Behavior_CauseDamage chain - those probes caught nothing while the skill was demonstrably working.
# The Maliwan boost's own graph is four Behavior_AttemptStatusEffect nodes (Amp/slag, Corrosive,
# Incendiary, Shock) behind an IntSwitchRange, so the status-effect route is the one to watch.
_remote_probe = 0


@hook("WillowGame.Behavior_RemoteCustomEvent:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_remote_probe")
def _on_remote_event(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """Which CE_DoDamage_<element> the conditional raises. CE_DoDamage_Cryo is the slag branch here
    (TPS's ice slot, repointed); if it is never raised, the conditional is not choosing pin 4 and
    the fault is upstream of the damage node."""
    global _remote_probe
    if _remote_probe >= 16:
        return
    try:
        if "Quince_Doppel" not in obj._path_name():
            return
        _remote_probe += 1
        diag.detail(f"{TAG} custom event: {obj.CustomEventName} raised by {obj._path_name()}")
    except Exception:  # noqa: BLE001
        pass


_status_probe = 0


@hook("WillowGame.Behavior_AttemptStatusEffect:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_status_probe")
def _on_attempt_status(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    global _status_probe
    if _status_probe >= 16:
        return
    try:
        outer = obj.Outer._path_name()
        if "Quince_Doppel" not in outer:
            return
        _status_probe += 1
        se = obj.StatusEffect
        unrealsdk.logging.info(f"{TAG} status effect: {outer.split('.')[-2]}.{obj.Name} -> "
                               f"{se.Name if se is not None else 'none'} on "
                               f"{args.ContextObject.Name if args.ContextObject is not None else '?'}")
    except Exception:  # noqa: BLE001
        pass


# Everything on the per-pellet path that does not change between shots, kept for a second: the
# definition (a find_object), the live Skill's consumer handle (GetSkillManager + a native scan of
# the active skills), its provider, the kernel and the grade (an attribute evaluation through the
# skill context). Measured before caching: 526 us per pellet, 3.65 ms worst, and a 20-pellet
# Maliwan shotgun put ~10 ms of Python into the frame its pellets landed in (doppelperf,
# 2026-09-25). With a hit, what is left is two parameter structs and the native graph call.
_sponsored_cache: dict = {}          # pc address -> {"until", "grade", "handle", "bpd", "kernel"}
SPONSORED_CACHE_SECONDS = 1.0


def _sponsored_state(pc: UObject) -> dict | None:
    """The cached raise state for this controller, rebuilt once a second; None while the skill has
    no points or no live instance."""
    try:
        key = pc._get_address()
    except Exception:  # noqa: BLE001
        return None
    now = time.monotonic()
    st = _sponsored_cache.get(key)
    if st is not None and now < st["until"]:
        return st if st["grade"] > 0 else None
    st = {"until": now + SPONSORED_CACHE_SECONDS, "grade": 0, "handle": None, "bpd": None, "kernel": None}
    _sponsored_cache[key] = st
    if len(_sponsored_cache) > 8:
        _sponsored_cache.clear()
        _sponsored_cache[key] = st
    st["grade"] = grade("SponsoredBy", pc)
    if st["grade"] <= 0:
        return None
    sk = active_skill("SponsoredBy", pc)
    d = skill_def("SponsoredBy")
    if sk is None or d is None:
        st["grade"] = 0
        return None
    try:
        st["handle"] = sk.GetBehaviorConsumerHandle()
        st["bpd"] = d.BehaviorProviderDefinition
        st["kernel"] = unrealsdk.find_class("BehaviorKernel").ClassDefaultObject
    except Exception:  # noqa: BLE001
        st["grade"] = 0
        return None
    return st


# Optional (off by default, user 2026-09-29): Sponsored By on the Orphan Maker's self-damage. The skill used to take
# every raise of its DamagedEnemy event, the Orphan Maker's hit on Jack included, and players liked the element it
# rolled on him; the round-1 fixes limited the bridge to enemies. With the setting on, that one self-hit is raised
# again, through the same route, so the element lands at the rate it would on an enemy. The Orphan Maker is the
# Captain Scarlett DLC's Jakobs shotgun (GD_Orchid_BossWeapons, title part Title__Unique_OrphanMaker).
def _orphan_self_on() -> bool:
    try:
        return bool(_orphan_self_option.value)
    except Exception:  # noqa: BLE001
        return False


def _is_orphan_maker(causer: UObject | None, pc: UObject) -> bool:
    w = causer
    try:
        if w is None or "Weapon" not in w.Class.Name:
            w = pc.Pawn.Weapon
        dd = w.DefinitionData
        for part in (dd.TitlePartDefinition, dd.BalanceDefinition, dd.PrefixPartDefinition):
            if part is not None and "OrphanMaker" in part.Name:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


_orphan_notes = 0


def _orphan_note(dtype: str, causer, orphan: bool) -> None:
    """The first self-hits with the setting on, recognised or not: what the Orphan Maker's hit looks like."""
    global _orphan_notes
    if _orphan_notes >= 6:
        return
    _orphan_notes += 1
    try:
        cname = causer.Class.Name if causer is not None else None
    except Exception:  # noqa: BLE001
        cname = "?"
    try:
        cname = f"{cname} {causer._path_name()}" if causer is not None else cname
    except Exception:  # noqa: BLE001
        pass
    unrealsdk.logging.info(f"{TAG} Sponsored By on the Orphan Maker: self-hit ({dtype}, causer {cname}) "
                           + ("recognised, raised on Jack" if orphan else "not taken (not the Orphan Maker, or not a gun hit)"))


# A status effect landed on Jack from the Orphan Maker case but did him no harm (user, 2026-09-29): the damage a pawn
# deals itself is scaled by the causer's GetInstigatorSelfDamageScale, and Sponsored By's five element nodes leave
# Behavior_CauseDamage.InstigatorSelfDamageScale at its default, so the bonus hit and the DoT ticks that name the node
# as their causer came to nothing on him (the Orphan Maker's own damage node sets its scale; that is how it hurts).
# Only this case ever aims those nodes at Jack, so they get a full scale once the case is used; on enemies the scale
# plays no part.
SPONSORED_DAMAGE_NODES = tuple(f"{SPONSORED_BPD}.Behavior_CauseDamage_{i}" for i in (5, 6, 7, 8, 9))
SPONSORED_SELF_SCALE = 1.0
_self_scale_checked = 0.0


def _sponsored_self_damage_scale() -> None:
    global _self_scale_checked
    now = time.monotonic()
    if now - _self_scale_checked < 5.0:
        return
    _self_scale_checked = now
    for path in SPONSORED_DAMAGE_NODES:
        try:
            node = unrealsdk.find_object("Behavior_CauseDamage", path)
            if abs(float(node.InstigatorSelfDamageScale) - SPONSORED_SELF_SCALE) > 1e-6:
                node.InstigatorSelfDamageScale = SPONSORED_SELF_SCALE
        except Exception:  # noqa: BLE001
            _log_once("orphan:scale:" + path, "Sponsored By self-damage scale not set on " + path.split(".")[-1] + ": "
                      + traceback.format_exc().splitlines()[-1])


# The element's DoT: Sponsored By's nodes carry no StatusEffectDamage (TPS's neither), so its status effects tick at the
# base of about 1 a second on anyone. The user chose to keep that for enemies and give the Orphan Maker case a real DoT
# (2026-09-29): for the length of the raise on Jack, the five nodes' StatusEffectDamage is a fraction of the Orphan
# Maker's own self-hit, then back to what the package holds, so enemy hits never see it. `doppelskill orphandot <f>`.
SPONSORED_SELF_DOT = 0.2
_orphan_dot_logged = 0


def _sponsored_self_dot(self_hit: float) -> list:
    """Set the nodes' StatusEffectDamage for the self raise; returns what to put back."""
    global _orphan_dot_logged
    saved = []
    value = max(0.0, self_hit) * SPONSORED_SELF_DOT
    for path in SPONSORED_DAMAGE_NODES:
        try:
            node = unrealsdk.find_object("Behavior_CauseDamage", path)
            cur = node.StatusEffectDamage
            saved.append((path, (float(cur.BaseValueConstant), cur.BaseValueAttribute, cur.InitializationDefinition,
                                 float(cur.BaseValueScaleConstant))))
            node.StatusEffectDamage = unrealsdk.make_struct("AttributeInitializationData", BaseValueConstant=value,
                                                            BaseValueAttribute=None, InitializationDefinition=None,
                                                            BaseValueScaleConstant=1.0)
        except Exception:  # noqa: BLE001
            _log_once("orphan:dot:" + path, "Sponsored By self DoT not set on " + path.split(".")[-1] + ": "
                      + traceback.format_exc().splitlines()[-1])
    if _orphan_dot_logged < 3:
        _orphan_dot_logged += 1
        unrealsdk.logging.info(f"{TAG} Sponsored By on the Orphan Maker: self-hit {self_hit:.0f}, element DoT base "
                               f"{value:.0f} ({SPONSORED_SELF_DOT:g} of the hit)")
    return saved


def _sponsored_restore_dot(saved: list) -> None:
    for path, (const, attr, init, scale) in saved:
        try:
            node = unrealsdk.find_object("Behavior_CauseDamage", path)
            node.StatusEffectDamage = unrealsdk.make_struct("AttributeInitializationData", BaseValueConstant=const,
                                                            BaseValueAttribute=attr, InitializationDefinition=init,
                                                            BaseValueScaleConstant=scale)
        except Exception:  # noqa: BLE001
            pass


# --- `doppelskill shieldwatch [s]`: what keeps Jack's shield short of full (2026-09-30) ---------------------------
# A Transformer under the Orphan Maker case (Sponsored By's shock rolled on Jack) refills from the absorbed shock but
# stays about 5% short of full (user). Nothing in the mod writes Jack's shield there, and the shock node is BL2's own
# DmgType_Shock_Impact, so the watch logs every half second: the shield, the shield pool's attributes (its recharge
# delay and rate among them), the pawn's status-effect properties, and every hit on him in between.
SHIELD_WATCH_HOOKS = ("WillowGame.WillowPawn:AdjustDamage", "WillowGame.WillowPlayerPawn:TakeDamage",
                      "WillowGame.WillowPawn:TakeDamage", "Engine.Pawn:TakeDamage")
SHIELD_WATCH_ID = "doppel_shieldwatch"
_shield_watch: dict = {}


def _brief(v, depth: int = 0) -> str:
    try:
        if isinstance(v, (int, float, bool, str)):
            return f"{v:.1f}" if isinstance(v, float) else str(v)
        if isinstance(v, UObject):
            s = f"{v.Class.Name}'{v.Name}'"
            if depth == 0:
                inner = []
                for p in v.Class._properties():
                    if any(k in p.Name for k in ("Status", "Effect")):
                        inner.append(f"{p.Name}={_brief(getattr(v, p.Name), 1)}")
                if inner:
                    s += " {" + ", ".join(inner[:8]) + "}"
            return s
        if hasattr(v, "__len__"):
            n = len(v)
            return f"[{n}]" + (f" first {_brief(v[0], depth + 1)}" if n and depth < 2 else "")
        return type(v).__name__
    except Exception as e:  # noqa: BLE001
        return f"? ({type(e).__name__})"


def _shield_watch_hit(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    try:
        if not _shield_watch or obj._path_name() != _shield_watch.get("pawn"):
            return
        dtype = args.DamageType.Name if getattr(args, "DamageType", None) is not None else "-"
        causer = getattr(args, "DamageCauser", None)
        amount = getattr(args, "InDamage", None)
        if amount is None:
            amount = getattr(args, "DamageAmount", None)
        try:
            fname = func.func.Name
        except Exception:  # noqa: BLE001
            fname = "hit"
        _shield_watch["hits"].append(f"{fname} {dtype} {float(amount or 0):.0f} by "
                                     f"{causer.Class.Name + ' ' + causer.Name if causer is not None else '-'}")
    except Exception:  # noqa: BLE001
        pass


def _shield_watch_stop(why: str) -> None:
    for fn in SHIELD_WATCH_HOOKS:
        try:
            unrealsdk.hooks.remove_hook(fn, Type.PRE, SHIELD_WATCH_ID)
        except Exception:  # noqa: BLE001
            pass
    if _shield_watch:
        unrealsdk.logging.info(f"{TAG} shield watch ended ({why})")
    _shield_watch.clear()


def shield_watch_start(pc: UObject, seconds: float) -> str:
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return "no pawn"
    _shield_watch_stop("restarted")
    attrs = []
    try:
        for a in unrealsdk.find_all("AttributeDefinition", exact=False):
            path = a._path_name()
            if path.startswith("D_Attributes.ShieldResourcePool."):
                attrs.append(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        status = [p.Name for p in pawn.Class._properties()        # the component, not the 30 modifier floats
                  if "Status" in p.Name and "Modifier" not in p.Name and "VfTable" not in p.Name and not p.Name.startswith("b")]
    except Exception:  # noqa: BLE001
        status = []
    _shield_watch.update(until=time.monotonic() + seconds, next=0.0, attrs=sorted(attrs), status=status, hits=[],
                         pawn=pawn._path_name(), started=time.monotonic())
    for fn in SHIELD_WATCH_HOOKS:
        try:
            unrealsdk.hooks.add_hook(fn, Type.PRE, SHIELD_WATCH_ID, _shield_watch_hit)
        except Exception:  # noqa: BLE001
            pass
    try:
        shield = pawn.ShieldArmor if hasattr(pawn, "ShieldArmor") else None
    except Exception:  # noqa: BLE001
        shield = None
    unrealsdk.logging.info(f"{TAG} shield watch: {seconds:.0f} s; pool attributes: "
                           + (", ".join(a.split(".")[-1] for a in attrs) or "none found")
                           + f"; status properties: {', '.join(status) or 'none'}; shield item {_brief(shield, 1)}")
    return f"shield watch on for {seconds:.0f} s: reproduce it, then send the log"


def shield_watch_tick(pc: UObject) -> None:
    if not _shield_watch:
        return
    now = time.monotonic()
    if now > _shield_watch["until"]:
        _shield_watch_stop("time")
        return
    if now < _shield_watch["next"]:
        return
    _shield_watch["next"] = now + 0.5
    pawn = pc.Pawn
    if pawn is None or pawn._path_name() != _shield_watch["pawn"]:
        return
    try:
        parts = [f"shield {float(pawn.GetShieldStrength()):.0f}/{float(pawn.GetMaxShieldStrength()):.0f}"]
        vals = []
        for path in _shield_watch["attrs"]:
            v = attr_value(path, pawn)
            if v is not None:
                vals.append(f"{path.split('.')[-1].replace('Shield', '')} {v:g}")
        parts.append("pool: " + ", ".join(vals))
        parts.append("status: " + "; ".join(f"{n}={_brief(getattr(pawn, n))}" for n in _shield_watch["status"]))
        hits = _shield_watch["hits"]
        if hits:
            parts.append(f"hits since last: {len(hits)}: " + " | ".join(hits[-4:]))
            hits.clear()
        unrealsdk.logging.info(f"{TAG} shield watch +{now - _shield_watch['started']:.1f}s: " + " / ".join(parts))
    except Exception:  # noqa: BLE001
        unrealsdk.logging.info(f"{TAG} shield watch sample failed: " + traceback.format_exc().splitlines()[-1])


# The self raise waits for the self-hit to land. It ran from the damage hook's PRE, before the hit: with a Transformer
# the shock it rolled was absorbed into a still-full shield (nothing to fill), then the self-hit took its share, so the
# shield sat one self-hit short of full for as long as the Orphan Maker fired (shieldwatch, 2026-09-30: 7357366 max,
# 310901 per hit, held at 7046465 through every shot, full only ~15 s after the last). Raised from the tick instead, the
# absorbed shock refills what the hit just took.
_orphan_self_pending: list[float] = []


def orphan_self_tick(pc: UObject) -> None:
    if not _orphan_self_pending:
        return
    hits = _orphan_self_pending[:4]
    _orphan_self_pending.clear()
    pawn = pc.Pawn
    if pawn is None or not _orphan_self_on():
        return
    for self_hit in hits:
        _sponsored_self_damage_scale()
        saved = _sponsored_self_dot(self_hit)
        try:
            _sponsored_damaged_enemy(pc, pawn)
        finally:
            _sponsored_restore_dot(saved)


def _is_orphan_self_hit(dtype: str, causer, pc: UObject) -> bool:
    """The Orphan Maker's hit on its user. Not a gun hit (measured 2026-09-29: the generic DamageType, and the causer is
    a Behavior_CauseDamage node, the weapon's own self-damage behaviour), so it is known by that node: one of the
    Orphan Maker's own, or any damage node while the Orphan Maker is the gun in hand."""
    try:
        if causer is not None and causer.Class.Name == "Behavior_CauseDamage":
            path = causer._path_name()
            if path.startswith(("Quince_Doppel", "GD_Doppelganger", "GD_Quince")):
                return False            # Sponsored By's own bonus hit (or another Jack skill's): never raise on that
            return "OrphanMaker" in path or _is_orphan_maker(None, pc)
    except Exception:  # noqa: BLE001
        pass
    return dtype in SPONSORED_SOURCES and _is_orphan_maker(causer, pc)


def _sponsored_damaged_enemy(pc: UObject, enemy: UObject) -> None:
    global _sponsored_raised, _sponsored_guard
    if _sponsored_guard or enemy is None or pc is None:
        return
    st = _sponsored_state(pc)
    if st is None:
        return
    global _sponsored_raise_at
    _sponsored_guard = True              # the bonus damage re-enters this path; do not stack
    _sponsored_raise_at = time.monotonic()
    t0 = time.perf_counter()
    # A splash hit (launchers, Torgue gyrojets, anything that cannot crit) arrives inside the enemy's
    # TakeRadiusDamage, which holds bTakingRadiusDamage up for the whole call and rolls one status effect at its
    # end, for the explosion's own damage type. TookDamageFromEnemy skips the per-hit roll while the flag is up,
    # so the bonus hit raised from here dealt its slag damage and never slagged (user, 2026-10-01). The flag is
    # down for the bonus hit alone, which then rolls as it does inside a bullet's hit.
    radius = False
    try:
        radius = bool(enemy.bTakingRadiusDamage)
        if radius:
            enemy.bTakingRadiusDamage = False
    except Exception:  # noqa: BLE001
        radius = False
    try:
        st["kernel"].ActivateBehaviorEventFromScript(ConsumerHandle=st["handle"], ProviderDefinition=st["bpd"],
                                                     EventName="DamagedEnemy", EventOutputToActivate=0,
                                                     Parameters=event_params(pc, enemy))
        if _sponsored_raised < 6:
            _sponsored_raised += 1
            diag.detail(f"{TAG} Sponsored By: raised DamagedEnemy on {enemy.Name}")
    except Exception:  # noqa: BLE001
        _sponsored_cache.clear()         # a stale handle after a respec or a map change: rebuild next pellet
        _log_once("sponsored:fire", "Sponsored By raise failed: " + traceback.format_exc().splitlines()[-1])
    finally:
        if radius:
            try:
                enemy.bTakingRadiusDamage = True     # the rest of the splash call (its own roll, its summary)
            except Exception:  # noqa: BLE001
                pass
        _sponsored_guard = False
        diag.section("sponsored: native graph", t0)


def sponsored_report(pc: UObject) -> list:
    lines = [f"Sponsored By: grade {grade('SponsoredBy', pc)}"]
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return lines + ["  no pawn"]
    on = attr_value(MALIWAN_ON, pawn)
    lines.append(f"  Maliwan bonus flag: {on} (1 means the Maliwan branch is live)")
    w = None
    try:
        w = pawn.Weapon
        if w is None:
            lines.append("  no weapon held")
        else:
            dd = w.DefinitionData
            man = getattr(dd, "ManufacturerDefinition", None)
            el = getattr(dd, "ElementalPartData", None)
            lines.append(f"  weapon {w.Name}: manufacturer {man.Name if man is not None else '?'}, "
                         f"elemental part {el.Name if el is not None else 'none (non-elemental)'}")
    except Exception:  # noqa: BLE001
        lines.append("  weapon unreadable: " + traceback.format_exc().splitlines()[-1])
    # Read on the pawn and on the weapon: the skill's evaluators use the skill's own context, and
    # which of the two these resolve against is exactly what is in question.
    hits = 0
    for leaf in ELEMENT_ATTRS:
        path = "D_Attributes.WeaponDamageType.WeaponDamage_Is_" + leaf
        on_pawn = attr_value(path, pawn)
        on_weap = attr_value(path, w) if w is not None else None
        if (on_pawn or 0) >= 1 or (on_weap or 0) >= 1:
            hits += 1
            mark = "  <- this is the branch that should fire"
        else:
            mark = ""
        lines.append(f"  WeaponDamage_Is_{leaf}: pawn {on_pawn}, weapon {on_weap}{mark}")
    if hits == 0:
        lines.append("  NOTE: no element is raised at all, which is correct for a non-elemental gun. "
                     "Hold the SLAG gun you want tested when running this - Amp is slag in BL2.")
    return lines


# --- amp shields on the clones -------------------------------------------------------------------
# An amp shield is BL2's "impact" shield, and GD_Shields.Skills.Impact_Shield_Skill is all weapon:
# it post-adds D_Attributes.Shield.ImpactShield_DamageBonus to D_Attributes.Weapon.WeaponDamage and
# sets EnableWeaponFireSkillEvent, which is what spends the charge. A Digi-Jack has no WillowWeapon
# at all - it fires wrist lasers from anim notifies - so a clone handed a copy of the Bee or any
# other amp shield simply did nothing with it (user, 2026-09-23), while novas and spikes worked,
# because those hang off shield events the clone does get.
#
# The shot is amped here instead, where a clone's damage lands: spend the shield's own consumption
# out of the clone's charge and add the shield's own bonus. That reads the way it does for the
# player - amped while there is charge to pay for it, ordinary once the shield is down - without
# inventing numbers, since both come off the equipped shield through the clone.
AMP_ENABLED = True          # `doppelskill amp on|off`: takes the clone amp out of the damage path entirely
# How the bonus reaches the engine, settled by measurement on 2026-09-23:
#
#   WillowAIPawn.TakeDamage's Damage is a by-value parameter, so writing it in a hook does nothing
#   and the documented way round that is Block + call the function again. That re-enters the native
#   damage pipeline from inside itself - TakeDamage even carries the Pipeline object that call owns
#   - and it hung the game every time (breadcrumbs ended inside this hook three runs running; with
#   the re-invoke switched off the hang stopped and the bonus stopped landing, which named it).
#
#   WillowPawn.AdjustDamage is the engine's own "adjust the damage about to be applied" step and
#   its InDamage parameter is an OUT parameter (property flags 0x180 = Parm|Out), so a value
#   written there is passed back to the caller by the engine itself. No Block, no second call, no
#   re-entry. WillowAIPawn does not override it, so enemies come through WillowPawn's.
AMP_BONUS_ATTR = "D_Attributes.Shield.ImpactShield_DamageBonus"
AMP_DRAIN_ATTR = "D_Attributes.Shield.ImpactShield_ConsumptionOnEmpower"
_amp_seen = False
# Shield to spend, per clone path, applied from the tick. The drain is NOT written inside the
# damage hook: SetShieldStrength there fires the shield's own events, and on a nova that damages
# enemies, which re-enters TakeDamage, which drains again. The guard below was set one line too
# late to stop it and the game hung (Application Hang, 2026-09-23, while testing amp shields; no
# amp line ever reached the log because the freeze began before it). Queue it, drain it from the
# tick - the same rule as the clone cleanup queue.
_amp_pending: dict = {}


_amp_probe = 0


def amp_numbers(holder) -> tuple:
    """(bonus damage per shot, shield spent per shot) for this clone's shield; (0, 0) if not an amp.

    Read off the player first, then the clone. Impact_Shield_Skill is what raises
    ImpactShield_DamageBonus, and a shield grants its skills to the character wearing it; a clone
    handed the item with GiveTo never gets that skill run, so the attribute reads 0 on the clone
    however good its shield is - which is why the first build of this logged nothing at all and
    amped nothing (2026-09-23). You Have My Shield gives the clone a copy of the player's shield,
    so the player's resolved numbers are that same shield's numbers."""
    global _amp_probe
    for ctx, label in ((_cast_pc().Pawn if _cast_pc() is not None else None, "player"), (holder, "clone")):
        if ctx is None:
            continue
        bonus = attr_value(AMP_BONUS_ATTR, ctx) or 0.0
        if bonus > 0.0:
            return bonus, max(0.0, attr_value(AMP_DRAIN_ATTR, ctx) or 0.0)
    if _amp_probe < 3:
        _amp_probe += 1
        diag.info(f"{TAG} amp: no amp bonus resolves on the player or the clone "
                  f"({AMP_BONUS_ATTR} reads 0 on both); not an amp shield, or the shield's skill is not running")
    return 0.0, 0.0


def _clone_behind(args):
    """The Digi-Jack that caused this damage, or None. The causer is the projectile, whose
    Instigator is the pawn; InstigatedBy is only its controller."""
    from . import digijack
    if not digijack.all_clone_entries():
        return None
    pawn = None
    try:
        causer = args.DamageCauser
        if causer is not None:
            pawn = causer.Instigator
        if pawn is None:
            by = args.InstigatedBy
            pawn = by.Pawn if by is not None else None
        if pawn is None:
            return None
        return pawn if pawn._get_address() in digijack._clone_addrs else None      # no path built per hit
    except Exception:  # noqa: BLE001
        return None


# How the bonus reaches the shot, after three routes through the damage call failed (2026-09-23):
#
#   TakeDamage.Damage is by value, so writing it does nothing; Block + call it again does work but
#   re-enters the native damage pipeline and hangs the game. WillowPawn.AdjustDamage's InDamage is
#   an OUT parameter and the write does survive the call ("our value survived", measured) - the
#   caller simply goes on using its own figure.
#
#   So the damage call is left alone entirely. Projectile_WristLaserBlast's own graph works its
#   damage out as ProjectileDamage x Att_DigiJack_DamageBonusFromSkills (a BinaryMath multiply of
#   those two variables), and the clone damage skills already drive that attribute with MT_Scale
#   effects. Its BASE value is what they scale, so putting 1 + bonus/D there - D being what the
#   clone's shot currently lands for - comes out as exactly D + bonus, through the engine's own
#   arithmetic. No hook in the damage path, nothing to re-enter.
#
#   D is measured rather than assumed: the AdjustDamage hook below watches what the clone's
#   unamped shots land for. That means no amp until a clone has landed one ordinary hit, and it
#   means the bonus is a per-shot average rather than exactly BL2's flat add on a crit - the
#   honest cost of not being able to touch the damage itself.
# Two ways to land it, because the faithful one has a history:
#
#  "damage"     Block WillowAIPawn.TakeDamage and call it again with the bonus added. This is the
#               only route that gives BL2's real amp - a flat add on that shot, crits and all - and
#               it is what was working before the hangs. The hang is re-entrancy, and the thing
#               being re-entered is named in the parameter list: TakeDamage carries the
#               WillowDamagePipeline the outer call is in the middle of using, and BL2 reuses
#               pipeline objects (WillowDamagePipeline.ResetToDefaults). Handing that same live
#               object to a nested call is what spins. Pipeline is Parm|Optional, so the re-invoke
#               now passes None and the engine sorts itself out a clean one.
#  "multiplier" No damage hook at all: set the base of the attribute the projectile's own graph
#               multiplies ProjectileDamage by. Cannot hang, but it scales the shot instead of
#               adding to it and needs one ordinary hit first to size itself. The fallback.
#  "projectile" The default, and the one that keeps BL2's real amp without touching the damage
#               call. The wrist laser's own graph works its damage out as
#               D_Attributes.Projectile.ProjectileDamage x the clone multiplier, and the projectile
#               exists, with its own attribute values, before it hits anything. Adding bonus/mult
#               to the projectile's ProjectileDamage at spawn therefore lands exactly `bonus` extra
#               on that one shot: flat, per shot, one damage number, and nothing hooked anywhere in
#               the damage path. Set at PostBeginPlay, which the mod already hooks for clone shots.
# Default: "multiplier". "projectile" is disproved - ProjectileDamage reads 0 on the projectile at
# PostBeginPlay (2026-09-23), so either it does not resolve against the projectile or its damage is
# filled in after spawn; either way the write lands where nothing reads it.
AMP_MODE = "projectile"
PROJECTILE_DAMAGE_ATTR = "D_Attributes.Projectile.ProjectileDamage"
AMP_MAX_REINVOKES_PER_FRAME = 32     # a backstop: if anything ever does run away, amping stops for
_amp_reinvokes = 0                   # the rest of the frame instead of taking the game with it
_amp_reentry = False

CLONE_DMG_MULT = "GD_Quince_JackClone.Misc.Att_DigiJack_DamageBonusFromSkills"
_amp_base: dict = {}        # clone path -> what its ordinary shot lands for
_amp_boosted: dict = {}     # clone path -> the base value we wrote, so ours is not measured back
# Every clone of a given level shoots for the same amount, so the reading is not really per clone:
# keeping the last one means a fresh clone is amped from its first shot instead of having to land an
# ordinary one first, which is what "the amp only works from the second attack" was (user,
# 2026-09-23). Only the very first clone of a session has an unamped opening shot now. Cleared with
# everything else on a map change, so a level-up's numbers get measured again rather than carried.
_amp_base_last = 0.0
# ...and remembered between launches, keyed on the clone's level, so the very first clone of a fresh
# game is amped too. Without this there is exactly one unamped shot per launch: the reading has to
# come from somewhere, and the only source is a shot that has already landed.
_AMP_BASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amp_base.json")
_amp_base_by_level: dict = {}


def _amp_base_load() -> None:
    global _amp_base_by_level
    try:
        with open(_AMP_BASE_FILE, encoding="utf-8") as f:
            _amp_base_by_level = {str(k): float(v) for k, v in json.load(f).items()}
    except Exception:  # noqa: BLE001; a missing or unreadable file just means measure it again
        _amp_base_by_level = {}


# A clone's ordinary shot differs from the last by a point or more on almost every hit (crits, resistances), and each
# difference rewrote the file from inside the AdjustDamage hook: most of that hook's 0.54 ms a call with nine clones
# firing (doppelperf, 2026-09-30). The value is kept in memory and written from the tick at most every
# AMP_BASE_SAVE_SECONDS, and at a level change.
AMP_BASE_SAVE_SECONDS = 30.0
_amp_base_dirty = False
_amp_base_saved_at = 0.0


def _amp_base_remember(level: int, d: float) -> None:
    global _amp_base_dirty
    key = str(int(level))
    if abs(_amp_base_by_level.get(key, 0.0) - d) < 1.0:
        return
    _amp_base_by_level[key] = d
    _amp_base_dirty = True


def _amp_base_save(force: bool = False) -> None:
    global _amp_base_dirty, _amp_base_saved_at
    if not _amp_base_dirty:
        return
    now = time.monotonic()
    if not force and now - _amp_base_saved_at < AMP_BASE_SAVE_SECONDS:
        return
    _amp_base_saved_at = now
    _amp_base_dirty = False
    try:
        with open(_AMP_BASE_FILE, "w", encoding="utf-8") as f:
            json.dump(_amp_base_by_level, f, indent=2, sort_keys=True)
    except Exception:  # noqa: BLE001
        pass


_amp_base_load()


def _amp_should(clone) -> float:
    """The bonus this clone's shield should be adding right now, or 0."""
    bonus, drain = amp_numbers(clone)
    if bonus <= 0.0:
        return 0.0
    full = float(clone.GetMaxShieldStrength())
    charge = float(clone.GetShieldStrength())
    if full <= 0.0 or charge < full or charge < drain:
        return 0.0
    return bonus


# The projectile's ProjectileDamage reads 0 at PostBeginPlay: it is filled in after the spawn, not
# unreadable. A projectile is in the air for many frames, so the damage can be read a frame or two
# later and the bonus written onto that one shot while it is still travelling - flat, exact, on the
# very first shot, and with nothing hooked in the damage path. If the reading never arrives the log
# says so and `doppelskill amp mode multiplier` is the fallback.
_amp_watch: dict = {}          # projectile path -> [clone path, frames left]
AMP_WATCH_FRAMES = 6
_amp_watch_logged = 0


def _resolve(path: str):
    try:
        return unrealsdk.find_object("Object", path)
    except Exception:  # noqa: BLE001
        return None


@hook("WillowGame.WillowProjectile:PostBeginPlay", Type.POST, hook_identifier="doppel_clone_amp_proj")
def _on_clone_amp_projectile(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """Note a clone's shot; its damage is not readable yet, so the work happens from the tick."""
    if not AMP_ENABLED or AMP_MODE != "projectile":
        return
    from . import digijack
    if not digijack.all_clone_entries():
        return
    try:
        clone = obj.Instigator
        if clone is None:
            return
        cpath = clone._path_name()
        if any(e["path"] == cpath for e in digijack.all_clone_entries()):
            _amp_watch[obj._path_name()] = [cpath, AMP_WATCH_FRAMES]
    except Exception:  # noqa: BLE001
        pass


def _amp_watch_tick() -> None:
    """Load the bonus onto clone shots now in flight, as soon as their damage is readable."""
    global _amp_watch_logged, _amp_seen
    if not _amp_watch:
        return
    for ppath, entry in list(_amp_watch.items()):
        cpath, left = entry
        entry[1] = left - 1
        proj = _resolve(ppath)
        if proj is None or left <= 0:
            del _amp_watch[ppath]
            if proj is not None and _amp_watch_logged == 0:
                # It never became readable in AMP_WATCH_FRAMES. Say so once rather than quietly
                # doing nothing: it means this route cannot work and multiplier is the one to use.
                _log_once("amp:unread", f"amp: a clone's shot never reported a ProjectileDamage "
                                        f"within {AMP_WATCH_FRAMES} frames; try `doppelskill amp mode multiplier`")
            continue
        try:
            cur = attr_value(PROJECTILE_DAMAGE_ATTR, proj)
            if not cur:                       # not filled in yet; look again next frame
                continue
            del _amp_watch[ppath]
            clone = _resolve(cpath)
            if clone is None:
                continue
            bonus, drain = amp_numbers(clone)
            if bonus <= 0.0:
                continue
            full = float(clone.GetMaxShieldStrength())
            charge = float(clone.GetShieldStrength()) - _amp_pending.get(cpath, 0.0)
            if full <= 0.0 or charge < full or charge < drain:
                continue
            mult = attr_value(CLONE_DMG_MULT, clone) or 1.0
            if mult <= 0.0:
                mult = 1.0
            if not set_attr(PROJECTILE_DAMAGE_ATTR, proj, float(cur) + bonus / mult):
                continue
            if drain > 0.0:
                _amp_pending[cpath] = _amp_pending.get(cpath, 0.0) + drain
            if not _amp_seen:
                _amp_seen = True
                unrealsdk.logging.info(f"{TAG} amp: a clone's shield amps (+{bonus:.0f} per shot for {drain:.0f} shield)")
            if _amp_watch_logged < 6:
                _amp_watch_logged += 1
                unrealsdk.logging.info(
                    f"{TAG} amp: shot in flight {float(cur):.0f} -> {float(cur) + bonus / mult:.0f} "
                    f"(x{mult:.2f} = +{bonus:.0f} on the hit), {AMP_WATCH_FRAMES - left + 1} frame(s) after spawn")
        except Exception:  # noqa: BLE001
            _amp_watch.pop(ppath, None)
            _log_once("amp:watch", "amp in-flight load failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.WillowAIPawn:TakeDamage", Type.PRE, hook_identifier="doppel_clone_amp_dmg")
def _on_clone_amp_damage(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    """BL2's amp, on the clone's shot: the flat bonus added to the damage actually being applied."""
    global _amp_reentry, _amp_reinvokes, _amp_seen
    diag.hcrumb("skills._on_clone_amp_damage")
    if not AMP_ENABLED or AMP_MODE != "damage" or _amp_reentry:
        return None
    if _amp_reinvokes >= AMP_MAX_REINVOKES_PER_FRAME:
        return None
    clone = _clone_behind(args)
    if clone is None:
        return None
    _amp_reentry = True              # before anything, not after; see the 2026-09-23 hang
    try:
        diag.hcrumb("amp: numbers")
        bonus, drain = amp_numbers(clone)
        if bonus <= 0.0:
            return None
        path = clone._path_name()
        diag.hcrumb("amp: shield read")
        full = float(clone.GetMaxShieldStrength())
        charge = float(clone.GetShieldStrength()) - _amp_pending.get(path, 0.0)
        if full <= 0.0 or charge < full or charge < drain:
            return None
        if drain > 0.0:              # paid from the tick, never from inside the damage stack
            _amp_pending[path] = _amp_pending.get(path, 0.0) + drain
        before = float(args.Damage)
        args.Damage = before + bonus
        args.Pipeline = None         # the live pipeline is the outer call's; let this one get its own
        if not _amp_seen:
            _amp_seen = True
            unrealsdk.logging.info(f"{TAG} amp: a clone's shield amps (+{bonus:.0f} per shot for {drain:.0f} shield)")
        _amp_reinvokes += 1
        diag.hcrumb("amp: reinvoke in")
        try:
            func(args)             # TakeDamage returns nothing: plain Block (a (Block, None) made the SDK warn per shot)
            return Block
        finally:
            diag.hcrumb("amp: reinvoke out")
    except Exception:  # noqa: BLE001
        _log_once("amp:fail", "clone amp failed: " + traceback.format_exc().splitlines()[-1])
    finally:
        _amp_reentry = False
    return None


@hook("WillowGame.WillowPawn:AdjustDamage", Type.PRE, hook_identifier="doppel_clone_amp_pre")
def _on_clone_amp_pre(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """Watch only: what a clone's ordinary shot lands for, so the multiplier can be sized."""
    global _amp_base_last, _last_pc_damage
    diag.hcrumb("skills._on_clone_amp_pre")
    if not AMP_ENABLED:
        return
    # The player's own damage first, before the clone lookup (a clone's shot is instigated by the
    # clone's controller, never by the player): 140 us of property reads per call were being spent
    # on every hit in the game before this ordering (doppelperf, 2026-09-25).
    try:
        by = args.InstigatedBy
        pc = by if by is not None and _is_jack_controller(by) else None
        if pc is not None and by is not None and by is pc:
            dtype = str(args.DamageType.Name if args.DamageType is not None else "")
            merger = time.monotonic() < _merger_live_until       # Merger's beams arrive as the held gun's damage
            try:
                # for Compound Interest's gate: was the damage that is about to land a gun's?
                _ci_last_damage[obj._path_name()] = (time.monotonic(), dtype in CI_GUN_SOURCES and not merger)
            except Exception:  # noqa: BLE001
                pass
            try:
                _last_pc_damage = (dtype, args.DamageCauser.Class.Name if args.DamageCauser is not None else "-", obj.Name)
            except Exception:  # noqa: BLE001
                pass
            # an enemy: not Jack himself (the Orphan Maker's self-damage is a gun hit on his own pawn) nor a player
            if dtype in SPONSORED_SOURCES and not merger and obj is not pc.Pawn and "PlayerPawn" not in obj.Class.Name:
                _sponsored_damaged_enemy(pc, obj)
            elif obj is pc.Pawn and not merger and _orphan_self_on():
                # the optional special case: the Orphan Maker's hit on Jack himself runs Sponsored By like a hit
                # on an enemy, so the skill's own graph rolls its element on him at its usual chance (the element
                # comes only with the Maliwan bonus, i.e. a Maliwan gun held just before)
                orphan = _is_orphan_self_hit(dtype, args.DamageCauser, pc)
                _orphan_note(dtype, args.DamageCauser, orphan)
                if orphan and len(_orphan_self_pending) < 8:
                    _orphan_self_pending.append(float(args.InDamage))     # raised once the hit has landed
            return
    except Exception:  # noqa: BLE001
        pass
    clone = _clone_behind(args)
    if clone is None:
        return
    try:
        path = clone._path_name()
        d = float(args.InDamage)
        if path in _amp_boosted:
            # Carrying our multiplier, so this is an amped hit, not a base reading: it is also the
            # moment the shield pays for it. The Bee's consumption is 0, so nothing is spent and it
            # amps every shot, which is the Bee; a shield that does charge for it drops below full
            # here and stops amping until it has recharged.
            if d > 0.0:
                _, drain = amp_numbers(clone)
                if drain > 0.0:
                    _amp_pending[path] = _amp_pending.get(path, 0.0) + drain
            return
        if d > 0.0:
            _amp_base[path] = _amp_base_last = d
            try:
                _amp_base_remember(int(clone.GetExpLevel()), d)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


_amp_logged_attr = 0
AMP_TICK_SECONDS = 0.1
_amp_tick_next = 0.0


def amp_tick() -> None:
    """Put the amp on (or take it off) each live clone's damage multiplier, and pay any drain."""
    global _amp_logged_attr, _amp_seen, _amp_reinvokes, _amp_tick_next
    from . import digijack
    _amp_reinvokes = 0
    if not AMP_ENABLED:
        return
    _amp_watch_tick()
    # every live clone's shield and multiplier each frame grew with the crowd (12.5 ms a second with nine out,
    # 2026-09-30); ten times a second keeps the amp within a tenth of a second of the shield
    now = time.monotonic()
    if now < _amp_tick_next:
        return
    _amp_tick_next = now + AMP_TICK_SECONDS
    _amp_base_save()
    live = set()
    for entry in list(digijack._clones):
        path = entry.get("path")
        clone = digijack._resolve_pawn(path)
        if clone is None:
            continue
        live.add(path)
        try:
            bonus = _amp_should(clone) if AMP_MODE == "multiplier" else 0.0
            base = _amp_base.get(path) or _amp_base_last
            if not base:                       # nothing measured yet this launch: last time's
                try:
                    base = _amp_base_by_level.get(str(int(clone.GetExpLevel())), 0.0)
                except Exception:  # noqa: BLE001
                    base = 0.0
            want = (1.0 + bonus / base) if (bonus > 0.0 and base > 0.0) else 1.0
            if abs(want - _amp_boosted.get(path, 1.0)) < 0.001:
                continue
            if not set_attr(CLONE_DMG_MULT, clone, want):
                continue
            if want > 1.0:
                _amp_boosted[path] = want
            else:
                _amp_boosted.pop(path, None)
            if not _amp_seen and bonus > 0.0:
                _amp_seen = True
                unrealsdk.logging.info(f"{TAG} amp: a clone's shield amps (+{bonus:.0f} per shot)")
            if _amp_logged_attr < 6:
                _amp_logged_attr += 1
                unrealsdk.logging.info(
                    f"{TAG} amp: {clone.Name} damage multiplier {want:.2f} "
                    f"(ordinary shot {base:.0f} + {bonus:.0f} bonus)")
        except Exception:  # noqa: BLE001
            _log_once("amp:attr", "amp multiplier failed: " + traceback.format_exc().splitlines()[-1])
    for path in [p for p in list(_amp_boosted) + list(_amp_base) if p not in live]:
        _amp_boosted.pop(path, None)
        _amp_base.pop(path, None)
    for path, spend in list(_amp_pending.items()):
        del _amp_pending[path]
        pawn = digijack._resolve_pawn(path)
        if pawn is None:
            continue
        try:
            pawn.SetShieldStrength(ShieldValue=max(0.0, float(pawn.GetShieldStrength()) - spend))
        except Exception:  # noqa: BLE001
            _log_once("amp:drain", "amp drain failed: " + traceback.format_exc().splitlines()[-1])


def _shield_of(holder):
    try:
        for it in list(holder.EquippedItems):
            if it is not None and "Shield" in str(it.Class.Name):
                return it
    except Exception:  # noqa: BLE001
        pass
    return None


def _shield_line(holder, label: str) -> str:
    try:
        cur, mx = holder.GetShieldStrength(), holder.GetMaxShieldStrength()
    except Exception:  # noqa: BLE001
        cur = mx = -1.0
    item = _shield_of(holder)
    if item is None:
        return f"  {label}: no shield item equipped; strength {cur:.0f}/{mx:.0f}"
    parts = []
    try:
        dd = item.DefinitionData
        for slot in ("ItemDefinition", "BalanceDefinition", "AlphaPartData", "BetaPartData",
                     "GammaPartData", "DeltaPartData", "EpsilonPartData", "ZetaPartData"):
            v = getattr(dd, slot, None)
            if v is not None:
                parts.append(f"{slot[:-8] if slot.endswith('PartData') else slot}={v.Name}")
    except Exception:  # noqa: BLE001
        pass
    seqs = []
    try:
        for s in item.BehaviorProviderDefinition.BehaviorSequences:
            seqs.append(str(s.BehaviorSequenceName))
    except Exception:  # noqa: BLE001
        pass
    bonus, drain = amp_numbers(holder)
    amp = f"amp +{bonus:.0f} per shot for {drain:.0f} shield" if bonus > 0.0 else "not an amp shield"
    return (f"  {label}: {item.Class.Name} strength {cur:.0f}/{mx:.0f}; {amp}; "
            f"parts [{', '.join(parts) or 'unreadable'}]; sequences {seqs or '(none on the item)'}")


def shield_report(pc: UObject) -> list:
    from . import digijack
    lines = [f"You Have My Shield: grade {grade('YouHaveMyShield', pc)}"]
    if pc is not None and pc.Pawn is not None:
        lines.append(_shield_line(pc.Pawn, "player"))
    out = 0
    for entry in list(digijack._clones):
        pawn = digijack._resolve_pawn(entry["path"])
        if pawn is None:
            continue
        out += 1
        lines.append(_shield_line(pawn, pawn.Name))
    if not out:
        lines.append("  no clone is out; cast the action skill and run this again")
    return lines


CLONE_DIED_LISTENERS = ("Teamwork", "Commitment", "Collaborate", "Optimism", "Leadership")
SEVT_KILLED_ENEMY = 11          # ESkillEventType (BL2): ..., SEVT_ShieldDepletedAfterBeingFull = 10, SEVT_KilledEnemy = 11
CLONE_KILLS_COUNT_AS_PLAYER_KILLS = False    # off until the OnKilledEnemy diag shows whether BL2 already routes pet kills


LEADERSHIP_KILL_LISTENERS = ("BestFootForward", "TakeTheirFreedom", "JacksCache")
KILL_SKILLS = ("Believe", "Synergy", "Winning", "AbsoluteAdvantage")       # Jack's SKILL_TYPE_Kill skills
SKILL_TYPE_KILL = 3              # ESkillType: Passive 0, Action 1, ActionAugment 2, Kill 3, Proficiency 4


TTF_BRIDGE = False
_health_snapshot_due = 0.0


def _snapshot_clone_health(label: str) -> None:
    """Log every live clone's health now and again 0.3 s later (heal sizing)."""
    global _health_snapshot_due
    from . import digijack
    parts = []
    for entry in list(digijack._clones):
        pawn = digijack._resolve_pawn(entry["path"])
        if pawn is not None:
            parts.append(f"{pawn.Name} {pawn.GetHealth():.0f}/{pawn.GetMaxHealth():.0f}")
    pc = _cast_pc()
    if pc is not None and pc.Pawn is not None:
        parts.append(f"player {pc.Pawn.GetHealth():.0f}/{pc.Pawn.GetMaxHealth():.0f}")
    diag.info(f"{TAG} health {label}: {parts}")
    if label.startswith("before"):
        _health_snapshot_due = time.monotonic() + 0.3


def _hostile_pawn(pc: UObject) -> UObject | None:
    """Any live enemy of the player (the victim slot of a kill)."""
    me = pc.Pawn
    if me is None:
        return None
    from . import digijack
    for p in unrealsdk.find_all("WillowAIPawn", exact=False):
        try:
            if p.Name.startswith("Default__") or p.Controller is None or digijack._is_digijack_pawn(p):
                continue
            if p.GetHealth() <= 0 or not p.IsEnemy(me):
                continue
            return p
        except Exception:
            continue
    return None


def _trigger_kill_skills(pc: UObject) -> str:
    """What the engine does on a kill for SKILL_TYPE_Kill skills (read off
    WillowPlayerController.UpdateKillSkills / ForceKillSkillsOff, 2026-09-14): arm the
    kill-skill timer (KillSkillTimeRemaining, Globals.KillSkillDurationsPerPlayers[players-1]
    times the duration multiplier) and run UpdateKillSkills(state, True) for every graded kill
    skill in the tree; that starts the ones not running (no stacking) and the HUD's kill-skill
    icons; the timer's expiry (ForceKillSkillsOff) ends them together."""
    tree = pc.PlayerSkillTree
    if tree is None:
        return "no skill tree"
    duration = 7.0
    try:
        g = unrealsdk.find_object("GlobalsDefinition", "GD_Globals.General.Globals")
        duration = float(g.KillSkillDurationsPerPlayers[_player_count(pc) - 1].Duration) * float(pc.KillSkillDurationMultiplier or 1.0)
    except Exception:
        pass
    graded = []
    if diag.enabled:                             # for the log line only: ~100 skills x 3 reads on every Leadership kill
        for sd in tree.Skills:                   # PlayerSkillTreeSkillData: Definition, Grade
            d = sd.Definition
            try:
                if d is not None and int(d.SkillType) == SKILL_TYPE_KILL and int(sd.Grade) > 0:
                    graded.append(f"{d.Name}:g{sd.Grade}")
            except Exception:
                pass
    # UpdateKillSkills(bEnable) walks the tree itself (its SkillState is a local, not a parameter)
    # and starts every kill skill afresh each call (four clone deaths = four Synergies,
    # 2026-09-14): the engine only calls it when the timer has run out, otherwise it just
    # refreshes the timer. Same here.
    global _kill_skills_until
    remaining = float(pc.KillSkillTimeRemaining)
    running = any(active_skill(k_, pc) is not None for k_ in KILL_SKILLS)
    ok = None
    if not running:
        ok = pc.UpdateKillSkills(bEnable=True)
    pc.KillSkillTimeRemaining = duration
    pc.DurationOfLastKillSkillActivation = duration
    _kill_skills_until = _game_clock(pc) + duration
    global _kill_timer_seen
    _kill_timer_seen = None
    return f"{'refreshed' if running else f'UpdateKillSkills(True) -> {ok}'} (timer was {remaining:.2f}) for {graded}, {duration:.1f}s"


# The kill-skill duration depends on how many players are in the game. It was counted with a scan of every object in
# the game (find_all) on every Leadership kill: 15+ ms a Digi-Jack death with a crowd cycling (2026-10-01). The game
# info's own count instead (the host's), the replication info's player list on a client, kept for ten seconds.
_player_count_cache = (0.0, 1)


def _player_count(pc: UObject) -> int:
    global _player_count_cache
    now = time.monotonic()
    if now < _player_count_cache[0]:
        return _player_count_cache[1]
    n = 1
    try:
        game = pc.WorldInfo.Game
        if game is not None:
            n = int(game.NumPlayers)
        else:
            n = sum(1 for p in pc.WorldInfo.GRI.PRIArray if p is not None and not bool(p.bOnlySpectator))
    except Exception:  # noqa: BLE001
        n = 1
    n = max(1, min(4, n))
    _player_count_cache = (now + 10.0, n)
    return n


_kill_skills_until = 0.0
_kill_skill_probe_at = 0.0
_kill_timer_seen = None          # (game time, KillSkillTimeRemaining) once the deadline has passed


def _game_clock(pc) -> float:
    """WorldInfo.TimeSeconds: stands still while the game is paused (inventory, menus)."""
    try:
        return float(pc.WorldInfo.TimeSeconds)
    except Exception:  # noqa: BLE001
        return time.monotonic()


def _kill_skill_tick(now: float) -> None:
    """Does the engine run KillSkillTimeRemaining down and switch the skills off? A fallback for a
    timer that never moves after a Leadership kill. On the game clock: the wall clock kept running
    while the game was paused, so the kill skills were ended in the inventory while the engine's own
    timer stood still (user, 2026-09-26). And only a timer that has stopped is ended: a real kill
    refreshes the engine's timer past our deadline, and that one is running, not stuck."""
    global _kill_skill_probe_at, _kill_skills_until, _kill_timer_seen
    if not _kill_skills_until:
        return
    pc = _cast_pc()
    if pc is None:
        return
    gt = _game_clock(pc)
    if gt < _kill_skills_until + 0.5:
        return
    try:
        cur = float(pc.KillSkillTimeRemaining)
        if cur <= 0.5 or not any(active_skill(k_, pc) is not None for k_ in KILL_SKILLS):
            _kill_skills_until, _kill_timer_seen = 0.0, None        # ended on its own
            return
        if _kill_timer_seen is None:
            _kill_timer_seen = (gt, cur)
            return
        t0, v0 = _kill_timer_seen
        if gt - t0 < 1.0:
            return
        _kill_skills_until, _kill_timer_seen = 0.0, None
        if cur < v0 - 0.2:
            return                                                  # running down: the engine has it
        pc.KillSkillTimeRemaining = 0.0
        pc.ForceKillSkillsOff()
        diag.info(f"{TAG} kill-skill timer stuck at {cur:.1f}s: ForceKillSkillsOff() from the mod")
    except Exception:
        _log_once("killskill:off", "ForceKillSkillsOff failed: " + traceback.format_exc().splitlines()[-1])


# Init_Collaborate_DamageBonus_Calc works out the clones' share from two designer attributes on the
# clone itself (DASCOPE_Local): Att_Collaborate_Stacks and Att_Collaborate_DamagePerStack. In TPS the
# native action skill wrote them; nothing in BL2 does, so both read 0, the formula came out at 1.0
# and the clones got no Collaborate damage at all, not even a stale amount (`doppelcheck`, 2026-09-22).
CLONE_COLLAB_STACKS = "GD_Quince_JackClone.Misc.Att_Collaborate_Stacks"
CLONE_COLLAB_PER_STACK = "GD_Quince_JackClone.Misc.Att_Collaborate_DamagePerStack"
PLAYER_COLLAB_STACKS = "Quince_Doppel_GreaterGood.Misc.Att_Collaborate_CurrentStacks"
COLLAB_DMG_PER_STACK_PER_GRADE = 0.01        # Att_Collaborate_DmgPerStackPerLevel's constant
_collab_pushed: tuple | None = None


def push_collaborate_to_clones(why: str, force: bool = False) -> str:
    """Put the player's stack count and per-stack damage on every live clone, then recompute."""
    global _collab_pushed
    from . import digijack
    pc = _cast_pc()
    if pc is None or pc.Pawn is None:
        return "no pawn"
    g = grade("Collaborate", pc)
    stacks = attr_value(PLAYER_COLLAB_STACKS, pc.Pawn)
    if stacks is None:
        stacks = attr_value(PLAYER_COLLAB_STACKS, pc) or 0.0
    per = COLLAB_DMG_PER_STACK_PER_GRADE * g
    # The clones themselves are not part of the key: a new clone is given the values as it spawns
    # (push_collaborate_to_one), so a Digi-Jack coming or going no longer re-pushed and refreshed all of them.
    key = (round(float(stacks), 3), round(per, 4))
    if key == _collab_pushed and not force:
        return "unchanged"
    n = 0
    for entry in list(digijack._clones):
        if entry.get("death_start") is not None or entry.get("derez_until") is not None:
            continue                                 # dissolving: nothing left to hit with
        pawn = digijack._resolve_pawn(entry.get("path"))
        if pawn is None:
            continue
        set_attr(CLONE_COLLAB_STACKS, pawn, float(stacks))
        set_attr(CLONE_COLLAB_PER_STACK, pawn, per)
        n += 1
    _collab_pushed = key
    if n:
        refresh_clone_skill_effects(why)
        _log_once(f"collab:{key[:2]}", f"Collaborate: {stacks:.0f} stack(s) x {per:.2%} per stack on {n} clone(s) ({why})")
    return f"{n} clone(s)"


# A clone death moves the stacks, and pushing them at once re-applied every standing clone's effects for every death
# (and every spawn): with twelve Digi-Jacks dying and respawning that was a full refresh of all of them several times
# a second (2026-10-01). Deaths close together now share one push, COLLAB_PUSH_DELAY after the first.
COLLAB_PUSH_DELAY = 0.2
_collab_due = 0.0


def request_collaborate_push() -> None:
    global _collab_due
    if not _collab_due:
        _collab_due = time.monotonic() + COLLAB_PUSH_DELAY


def prime_clone_collaborate(pawn: UObject) -> None:
    """A Digi-Jack being built: the current stacks on it before its damage-bonus skill is applied, so the skill
    starts with them and needs no refresh (the refresh after the spawn cost several ms a clone)."""
    pc = _cast_pc()
    if pc is None or pc.Pawn is None or pawn is None:
        return
    g = grade("Collaborate", pc)
    if g <= 0:
        return
    stacks = attr_value(PLAYER_COLLAB_STACKS, pc.Pawn)
    if stacks is None:
        stacks = attr_value(PLAYER_COLLAB_STACKS, pc) or 0.0
    set_attr(CLONE_COLLAB_STACKS, pawn, float(stacks))
    set_attr(CLONE_COLLAB_PER_STACK, pawn, COLLAB_DMG_PER_STACK_PER_GRADE * g)


def push_collaborate_to_one(pawn: UObject) -> None:
    """A Digi-Jack just spawned: the current stacks on it, and its own effects re-applied (one refresh, not all)."""
    pc = _cast_pc()
    if pc is None or pc.Pawn is None or pawn is None:
        return
    g = grade("Collaborate", pc)
    if g <= 0:
        return
    stacks = attr_value(PLAYER_COLLAB_STACKS, pc.Pawn)
    if stacks is None:
        stacks = attr_value(PLAYER_COLLAB_STACKS, pc) or 0.0
    set_attr(CLONE_COLLAB_STACKS, pawn, float(stacks))
    set_attr(CLONE_COLLAB_PER_STACK, pawn, COLLAB_DMG_PER_STACK_PER_GRADE * g)
    refresh_clone_skill_effects("clone spawned", only=pawn)


def collaborate_tick() -> None:
    """The stack count changes on a clone death and resets at the end of a cast; a check a second
    apart is enough to keep the live clones current without another hook."""
    global _collab_next, _collab_due
    now = time.monotonic()
    if _collab_due and now >= _collab_due:
        _collab_due = 0.0
        from . import digijack
        if digijack._clones and grade("Collaborate", _cast_pc()) > 0:
            push_collaborate_to_clones("clone death", force=True)
        _collab_next = now + 1.0
        return
    if now < _collab_next:
        return
    _collab_next = now + 1.0
    from . import digijack
    if digijack._clones and grade("Collaborate", _cast_pc()) > 0:
        push_collaborate_to_clones("tick")


_collab_next = 0.0


def refresh_clone_skill_effects(why: str, only: UObject | None = None) -> int:
    """Re-apply the live clones' Skill_DigiJack_DamageBonuses.

    Its Collaborate effect takes its value from Init_Collaborate_DamageBonus_Calc when the skill is
    applied, and the skill is applied once, as the clone spawns. A stack gained after that did
    nothing for the clones already standing there until they were replaced, which is what players
    saw: one Digi-Jack hitting harder than the other (2026-09-22). RefreshSkillsForInstigator makes
    the engine recompute the effects it has already applied for that instigator."""
    from . import digijack
    pc = _cast_pc()
    if pc is None:
        return 0
    try:
        manager = pc.GetSkillManager()
    except Exception:  # noqa: BLE001
        return 0
    if manager is None:
        return 0
    n = 0
    pawns = [only] if only is not None else [digijack._resolve_pawn(e.get("path")) for e in list(digijack._clones)
                                               if e.get("death_start") is None and e.get("derez_until") is None]
    for pawn in pawns:
        ctrl = None
        try:
            ctrl = pawn.Controller if pawn is not None and not bool(pawn.bDeleteMe) else None
        except Exception:  # noqa: BLE001
            ctrl = None
        if ctrl is None:
            continue
        try:
            manager.RefreshSkillsForInstigator(SkillInstigator=ctrl)
            n += 1
        except Exception:
            try:
                manager.RefreshSkillsForInstigator(ctrl)
                n += 1
            except Exception:
                _log_once("refreshclone", "RefreshSkillsForInstigator failed: " + traceback.format_exc().splitlines()[-1])
                return n
    if n:
        _log_once(f"refreshclone:{why}", f"{why}: refreshed the damage bonuses on {n} live clone(s)")
    return n


def on_clone_died(pawn: UObject, badass: bool, leadership: bool = True) -> None:
    """A Digi-Jack dies: raise CE_CloneDespawned on ITS body-death graph, what TPS's native action
    skill did when it retired one. That 'Skill' sequence sends CloneDied to Teamwork, Commitment,
    Collaborate, Optimism and Leadership on the master (its SkillCustomEvent nodes resolve the
    player through the clone's PlayerMaster; traced 2026-09-14), CloneGibbed to Accountability,
    and sets off Optimism's explosion at the clone. The engine's own OnKilledBy never reaches that
    sequence for our clones (they die through SetHealth(0) after the dissolve), so the per-skill
    CloneDied bridge stays only as the fallback.

    Leadership's own graph then fires CE_LeadershipActivate at the kill-event skills through a
    TPS-only node that is stubbed here, so that half of Leadership ("a Digi-Jack death counts as
    a kill") is done directly."""
    if leadership:
        _leadership_injured_health(_cast_pc(), "clone death")
    global _clone_deaths_this_cast
    _clone_deaths_this_cast += 1
    t = time.perf_counter()
    raise_clone_death_nova(pawn)                 # the part you see, on the frame it dies
    diag.section("skills.death nova", t)
    try:
        _death_stages.append([_frame_no + 2, "despawn", pawn._path_name(), badass, None, leadership, _loaded_cast_key()])
    except Exception:  # noqa: BLE001
        pass


# A Digi-Jack's death was 21 ms in one frame with a crowd cycling (2026-10-01): its death graph (CloneDied to the
# master's skills, Optimism's explosion), the nova, Leadership and the kill skills. The nova stays on the frame it dies;
# the death graph runs on the next frame and Leadership on the one after, the body still standing (DEATH_SECONDS).
_death_stages: list = []       # [due frame, stage, clone path, badass, despawned, counts for Leadership]

# Extra Skill Trees' Hostile Takeover marks its Digi-Jacks entry["no_leadership"] (2026-10-01): their deaths reach
# every death listener except Leadership. digijack passes leadership=False for them; the death graph's own CloneDied
# to Leadership (Behavior_SkillCustomEvent_17) is held back while that clone's graph runs, the fallback loop leaves
# Leadership out, and the Leadership stage (its kill listeners, Winning, the kill skills) is not queued.
_no_leadership_raise = False
_frame_no = 0


def _loaded_cast_key():
    try:
        from . import digijack
        return digijack._loaded_key or digijack._pc_path
    except Exception:  # noqa: BLE001
        return None


def cast_tick() -> None:
    """The per-frame cast work player_tick does for this machine's player, for another Jack's cast (co-op phase 2:
    digijack's secondary pass, with his record loaded)."""
    pc = _cast_pc()
    t = time.perf_counter()
    collaborate_tick()
    amp_tick()
    omm_queue_tick(pc)
    _kill_skill_tick(time.monotonic())
    diag.section("skills.cast_tick (another Jack)", t)


def death_stage_tick() -> None:
    leadership_check_tick()
    if not _death_stages:
        return
    from . import digijack
    due = [st for st in _death_stages if _frame_no >= st[0]]
    for st in due:
        _death_stages.remove(st)
        _, stage, path, badass, despawned, leadership, owner = st
        pawn = digijack._resolve_pawn(path)
        if pawn is None:
            continue
        t = time.perf_counter()
        try:
            with digijack.using_key(owner):          # the dead clone's own Jack (co-op)
                if stage == "despawn":
                    _death_despawn_stage(pawn, path, badass, leadership)
                    diag.section("skills.death graph (next frame)", t)
                else:
                    _death_leadership_stage(pawn, despawned)
                    diag.section("skills.death leadership (frame after)", t)
        except Exception:  # noqa: BLE001
            _log_once("death:" + stage, f"Digi-Jack death ({stage}) failed: " + traceback.format_exc().splitlines()[-1])
        break                                    # one stage a frame: two deaths together do not stack up


def _death_despawn_stage(pawn: UObject, path: str, badass: bool, leadership: bool = True) -> None:
    global _no_leadership_raise
    if leadership:
        # again here, the frame the graph runs: set at the death itself two frames earlier, a running heal had Jack
        # back above 0 by now (462809 after the reset, user 2026-10-06)
        _leadership_injured_health(_cast_pc(), "clone death")
    _no_leadership_raise = not leadership
    try:
        despawned = fire_on_clone(pawn, "CE_CloneDespawned")
    finally:
        _no_leadership_raise = False
    # the stack this death just gave Collaborate reaches the clones already standing only once their
    # own attributes carry it and the applied effects are recomputed: shortly, once for deaths close together
    request_collaborate_push()
    if not despawned:
        for leaf in CLONE_DIED_LISTENERS:
            if leaf == "Leadership" and not leadership:
                continue
            fire(leaf, "CloneDied", pc=_cast_pc(), other=pawn)
    if leadership:
        _death_stages.append([_frame_no + 1, "leadership", path, badass, despawned, True, _loaded_cast_key()])


def _death_leadership_stage(pawn: UObject, despawned: bool) -> None:
    pc = _cast_pc()
    _leadership_backup_revive(pc, "clone death")
    if pc is not None and grade("Leadership", pc) > 0:
        # "counts as if you had killed an enemy": the kill-event listeners (Best Foot Forward,
        # Take Their Freedom, Jack's Cache) hear TPS's CE_LeadershipActivate; the kill skills go
        # through the engine's kill-skill timer below. (WillowPlayerPawn.KilledEnemy(clone) on
        # top of that started Synergy a second time; stacking icon, 2026-09-14.)
        killed = False
        if not despawned:
            # only when the death graph did not run: its CloneDied reaches Leadership, whose own
            # SkillCustomEvent nodes send CE_LeadershipActivate (a second send here doubled Take
            # Their Freedom's heal and Best Foot Forward's counter, traced 2026-09-15)
            for leaf in LEADERSHIP_KILL_LISTENERS:
                fire(leaf, "CE_LeadershipActivate", pc=pc, other=pawn)
        how = "none"
        try:
            # Winning's restore for the refresh case, measured before the kill skills are restarted
            # (a clone death never counted: the Died hook only saw the player's own kills, 2026-09-25)
            if grade("Winning", pc) > 0:
                queue_winning_refresh(pc, "clone death (Leadership)")
        except Exception:
            _log_once("leadership:winning", "Leadership Winning restore failed: " + traceback.format_exc().splitlines()[-1])
        try:
            how = _trigger_kill_skills(pc)
        except Exception:
            _log_once("leadership:killskills", "Leadership kill-skill start failed: " + traceback.format_exc().splitlines()[-1])
        n = _fired.get("leadership:kill", 0) + 1
        _fired["leadership:kill"] = n
        if n <= 4:
            if diag.enabled:
                diag.info(f"{TAG} Leadership: clone death counted as a kill (KilledEnemy(clone) {killed}; kill skills {how}); graded "
                                       f"{[k_ for k_ in KILL_SKILLS if grade(k_, pc) > 0]}; listeners graded {[k_ for k_ in LEADERSHIP_KILL_LISTENERS if grade(k_, pc) > 0]}")


# A recall sends the Digi-Jacks away without killing them, so nothing ran the death path and Leadership never heard of
# them (players, 2026-10-01). For Leadership each recalled Digi-Jack now counts as a Digi-Jack death, the way the
# death path's fallback raises it (CloneDied on Leadership, CE_LeadershipActivate on its kill listeners, the kill skills
# once); the rest of the death (nova, Optimism's explosion, the other CloneDied listeners) stays with real deaths.
LEADERSHIP_REVIVE_MESSAGE = "Quince_Doppel_GreaterGood.Misc.ReviveMessage_Leadership"
# One Leadership revive per action skill use, as its graph allows: the backup gave one at every death and recall
# (user, 2026-10-06). Per cast record (co-op: each Jack's own); a revive by the graph counts too (Jack was down when
# the death reached it and is up a frame later).
_leadership_used = False
_leadership_was_down = False
_leadership_checks: list = []        # [frame due, controller path, why]: recalls, checked the frame after


def _leadership_backup_revive(pc, why: str) -> None:
    """The graph had its chance (a frame ago): if Jack is still in Fight For Your Life, the second wind directly.
    WillowPawn's injured state answers GiveInjuredPlayerSecondWind(bDontCountAsKill) with GoFromInjuredToHealthy();
    the banner is the graph's own ClientShowGenericRevive(ReviveMessage_Leadership). Resetting the health to 0 was not
    enough on its own: the revive still did not come after a heal in FFYL (user, 2026-10-06)."""
    global _leadership_used, _leadership_was_down
    try:
        if pc is None or grade("Leadership", pc) <= 0:
            return
        body = pc.Pawn
        down = body is not None and bool(body.bIsInjured)
        was_down, _leadership_was_down = _leadership_was_down, False
        if not down:
            if was_down:
                _leadership_used = True          # the graph revived him: that was this cast's one
            return
        if _leadership_used or not was_down:
            return                               # this cast's revive is spent (or he went down after the death)
        _leadership_used = True
        body.GoFromInjuredToHealthy()
        try:
            msg = unrealsdk.find_object("Object", LEADERSHIP_REVIVE_MESSAGE)
            if msg is not None:
                pc.ClientShowGenericRevive(ReviveDef=msg, OtherPRI=None, bIsOtherEventParticipant=False)
        except Exception:  # noqa: BLE001
            pass
        unrealsdk.logging.info(f"{TAG} Leadership: {pc.Name} still down after the graph ({why}); second wind given "
                               f"directly (now injured {bool(body.bIsInjured)})")
    except Exception:  # noqa: BLE001
        _log_once("leadership:backup", "Leadership backup revive failed: " + traceback.format_exc().splitlines()[-1])


def leadership_check_tick() -> None:
    """From death_stage_tick: the recall's backup revive, a frame after its CloneDied reached the graph."""
    if not _leadership_checks:
        return
    for chk in list(_leadership_checks):
        if _frame_no < chk[0]:
            continue
        _leadership_checks.remove(chk)
        try:
            pc = unrealsdk.find_object("WillowPlayerController", chk[1])
        except Exception:  # noqa: BLE001
            pc = None
        _leadership_backup_revive(pc, chk[2])


def _leadership_injured_health(pc, why: str) -> None:
    """Leadership's revive gates on HealthStateExpressionEvaluator(bInjured) before its GiveInjuredPlayerSecondWind.
    BL2's evaluator only passes at 0 health: a Jack healed while in Fight For Your Life (Take Their Freedom, a health
    regen relic, any heal) got no revive from a Digi-Jack death (user, 2026-10-06). 0 is FFYL's own resting value,
    so the health goes back to it just before the death reaches the graph, which then revives as designed (the
    second wind itself does not care: WillowPawn's injured state calls GoFromInjuredToHealthy whatever the health)."""
    global _leadership_was_down
    try:
        if pc is None or grade("Leadership", pc) <= 0:
            return
        body = pc.Pawn
        if body is None or not bool(body.bIsInjured):
            return
        _leadership_was_down = True
        if _leadership_used or float(body.GetHealth()) <= 0.0:
            return
        before = float(body.GetHealth())
        body.SetHealth(0.0)
        n = _fired.get("leadership:ffyl0", 0) + 1
        _fired["leadership:ffyl0"] = n
        if n <= 4:
            unrealsdk.logging.info(f"{TAG} Leadership: {pc.Name} healed to {before:.0f} in Fight For Your Life; back to 0 "
                                   f"so the {why} can revive him")
    except Exception:  # noqa: BLE001
        _log_once("leadership:ffyl0", "Leadership FFYL health reset failed: " + traceback.format_exc().splitlines()[-1])


def on_clones_recalled(pawns: list) -> None:
    pc = _cast_pc()
    if pc is None or not pawns or grade("Leadership", pc) <= 0:
        return
    _leadership_injured_health(pc, "recall")
    try:
        _leadership_checks.append([_frame_no + 2, pc._path_name(), "recall"])
    except Exception:  # noqa: BLE001
        pass
    for pawn in pawns:
        fire("Leadership", "CloneDied", pc=pc, other=pawn)
        for leaf in LEADERSHIP_KILL_LISTENERS:
            fire(leaf, "CE_LeadershipActivate", pc=pc, other=pawn)
    try:
        if grade("Winning", pc) > 0:
            queue_winning_refresh(pc, "recall (Leadership)")
    except Exception:  # noqa: BLE001
        _log_once("leadership:recallwinning", "Leadership recall Winning restore failed: " + traceback.format_exc().splitlines()[-1])
    how = "none"
    try:
        how = _trigger_kill_skills(pc)
    except Exception:  # noqa: BLE001
        _log_once("leadership:recallkill", "Leadership recall kill-skill start failed: " + traceback.format_exc().splitlines()[-1])
    n = _fired.get("leadership:recall", 0) + 1
    _fired["leadership:recall"] = n
    if n <= 3:
        unrealsdk.logging.info(f"{TAG} Leadership: {len(pawns)} recalled Digi-Jack(s) counted (kill skills {how})")


def on_clone_killed(victim: UObject, clone: UObject) -> None:
    """A Digi-Jack killed an enemy: Jack's kill skills, Best Foot Forward, Jack's Cache and Take
    Their Freedom all count it in TPS. SkillEffectManager.NotifySkillEvent(SEVT_KilledEnemy) with
    the player as instigator is the engine's own kill-skill entry point."""
    pc = _cast_pc()
    if pc is None:
        return
    _snapshot_clone_health("before clone kill")
    if not CLONE_KILLS_COUNT_AS_PLAYER_KILLS:
        # the dedicated clone-kill events only; the generic kill event would reach the same handlers twice
        # Take Their Freedom and Jack's Cache already hear clone kills natively: the clone's own
        # Skill_DigiJack_DamageBonuses (Kill) and its AI (OnKilledPawn) raise them with
        # Behavior_SkillCustomEvent, which does reach the master's skills (measured 2026-09-14:
        # two clone kills with the bridge off still healed the clones). Best Foot Forward has no
        # clone-side sender, so its Kill is raised here.
        if TTF_BRIDGE:
            fire("TakeTheirFreedom", "CE_OnDigiJackKilledEnemy", pc=pc, other=victim)
            fire("JacksCache", "PetKilledEnemy", pc=pc, other=victim)
        fire("BestFootForward", "Kill", pc=pc, other=victim)
        return
    try:
        pc.GetSkillManager().NotifySkillEvent(EventType=SEVT_KILLED_ENEMY, EventInstigator=pc, EventRecipient=victim)
        n = _fired.get("clonekill", 0) + 1
        _fired["clonekill"] = n
        if n <= 3:
            diag.info(f"{TAG} clone kill -> player kill event ({victim.Name}), #{n}")
    except Exception:
        _log_once("clonekill:fail", "NotifySkillEvent(KilledEnemy) failed: " + traceback.format_exc().splitlines()[-1])


CI_ACTIVE_SKILL = "CompoundInterest_Active"
CI_PROJECTILE = "Projectile_CompoundInterestFX"
CI_BPD = "Quince_Doppel_FreeEnterprise.Skills.CompoundInterest_Active.BehaviorProviderDefinition_0"
# Compound Interest's floating "$ 170" counter is a WillowServerSideProjectile that
# CompoundInterest_Active (a skill hosted ON the marked enemy) spawns from one of its four
# Compounding sequences and destroys from the same sequence (Behavior_Destroy on the saved
# FX_ProjectileN variable). In BL2 neither end works: bAttachToContext leaves the projectile
# loose where the enemy stood (v56 log: attached=None base=None owner=None) and the counters
# were never destroyed; seven alive after four kills. So the mod keeps its own book: counter
# per (enemy, sequence slot), attached here, destroyed on the slot's own Destroy/payout node
# and on the enemy's death.
_ci_counters: dict[tuple[str, str], str] = {}     # (enemy pawn name, slot) -> counter projectile path
_ci_known: set[str] = set()
_ci_pending: list[str] = []      # counter projectile paths seen by PostBeginPlay, not yet adopted

# "Shooting an enemy applies Compound Interest ... Shooting the same enemy with a different gun
# consumes it. Does not work with Rocket Launchers." The enemy-hosted graph implements the second
# sentence as CompareObject(weapon that damaged, weapon stored) straight off the Damaged event, and
# the engine raises Damaged for ANY damage: an Antagonist shield's slag ball arrived with no gun
# behind it, compared as "different", and consumed the stack (user, 2026-09-25). So the first node
# of every Damaged chain runs only when the damage that just landed was a gun's, which the
# AdjustDamage hook records per enemy a moment earlier (WillowDmgSource_* names measured with a
# probe: guns are Pistol/SubMachineGun/Shotgun/MachineGun/SniperRifle; grenades, skills, melee and
# barrel behaviours are not). Anything else neither applies, accrues nor consumes, as the text reads.
CI_GUN_SOURCES = frozenset(("WillowDmgSource_Pistol", "WillowDmgSource_SubMachineGun", "WillowDmgSource_Shotgun",
                            "WillowDmgSource_MachineGun", "WillowDmgSource_AssaultRifle", "WillowDmgSource_SniperRifle",
                            "WillowDmgSource_Laser",
                            # Borderlands 2's own class names (WillowGame's WillowDmgSource_* list, 2026-09-28): its
                            # snipers are WillowDmgSource_Sniper, so sniper shots never counted; Bullet is the generic one
                            "WillowDmgSource_Sniper", "WillowDmgSource_Bullet"))
# What Sponsored By's bridge takes for a shot: a gun's damage, launchers included (Maliwan makes them). Everything else
# is left out -- skills, grenades, melee, shields and above all damage over time (WillowDmgSource_StatusEffect): every
# DoT tick of Merger's shock, and of the elemental guns' own, raised a shot and let the Maliwan boost put its element
# on it (user, 2026-09-28). It used to take anything whose source was not a Skill.
SPONSORED_SOURCES = CI_GUN_SOURCES | {"WillowDmgSource_Rocket"}
CI_DAMAGED_ENTRY_NODES = frozenset(("Behavior_CompareFloat_1",                       # Default: applies the mark
                                    "Behavior_CompareObject_163", "Behavior_CompareObject_152",
                                    "Behavior_CompareObject_150", "Behavior_CompareObject_148"))   # Compounding 1-4
CI_GATE_WINDOW = 0.05           # the graph runs inside the same damage call; anything older is another hit
_ci_last_damage: dict[str, tuple[float, bool]] = {}      # enemy pawn path -> (when, was a gun)


def _ci_gate(obj: UObject, args: WrappedStruct):
    """Block the entry node of a Damaged chain when the damage that just landed was not a gun's."""
    try:
        if obj.Name not in CI_DAMAGED_ENTRY_NODES or not _ci_node_is_ours(obj):
            return None
        pawn = _ci_pawn_of(args.ContextObject)
        if pawn is None:
            return None
        rec = _ci_last_damage.get(pawn._path_name())
        if rec is None or time.monotonic() - rec[0] > CI_GATE_WINDOW:
            return None                       # not the player's damage, or not this call: leave the graph alone
        if rec[1]:
            return None
        return Block
    except Exception:  # noqa: BLE001
        return None


@hook("WillowGame.Behavior_CompareObject:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_ci_gate_obj")
def _on_ci_compare_object(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    return _ci_gate(obj, args)


def _sponsored_gate(obj: UObject):
    """Block Sponsored By's DamagedEnemy chain unless our own raise started it (see SPONSORED_BPD)."""
    global _sponsored_blocked
    try:
        if obj.Name != SPONSORED_ENTRY or obj.Outer._path_name().replace(":", ".") != SPONSORED_BPD:
            return None
        if time.monotonic() - _sponsored_raise_at <= SPONSORED_RAISE_WINDOW:
            return None
        if _sponsored_blocked < 6:
            _sponsored_blocked += 1
            src, causer, target = _last_pc_damage
            diag.info(f"{TAG} Sponsored By: blocked a DamagedEnemy that was not ours (the player's last hit: "
                      f"{src or '-'} by {causer} on {target})")
        return Block
    except Exception:  # noqa: BLE001
        return None


@hook("WillowGame.Behavior_CompareFloat:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_ci_gate_float")
def _on_ci_compare_float(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    blocked = _sponsored_gate(obj)
    return blocked if blocked is not None else _ci_gate(obj, args)


@hook("WillowGame.WillowProjectile:PostBeginPlay", Type.POST, hook_identifier="doppel_ci_projectile")
def _on_ci_projectile(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """A Compound Interest counter just spawned: remember its path for the spawn node's hook, which
    runs after this (the spawn is synchronous inside the node). _on_ci_spawn used to find it with a
    find_all over every projectile in the game: 2 ms mean, 15 ms worst, per counter (doppelperf,
    2026-09-25)."""
    try:
        d = obj.Definition
        if d is not None and d.Name == CI_PROJECTILE and not obj.Name.startswith("Default__"):
            _ci_pending.append(obj._path_name())
            if len(_ci_pending) > 64:
                del _ci_pending[:-64]
    except Exception:  # noqa: BLE001
        pass


# Marking an enemy ended the mark on every other one (user, 2026-10-02: in TPS it stacks across enemies). The mark is
# Behavior_ActivateSkill(CompoundInterest_Active, bNoSkillStacking) on the enemy's mind. TPS's node deactivates the
# skill first with its own bIgnoreSkillInstigator (False here: only the instance this enemy already has); BL2's passes
# True (decompiled 2026-10-02), which ends the skill for every instigator. So the node runs here as TPS's does: this
# enemy's instance ended, then the node re-invoked without its stacking pass (the activation itself is untouched).
CI_ACTIVATE_NODE = "Quince_Doppel_FreeEnterprise.Skills.CompoundInterest.BehaviorProviderDefinition_0.Behavior_ActivateSkill_1594"


@hook("WillowGame.Behavior_ActivateSkill:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_ci_activate")
def _on_ci_activate(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_ci_activate")
    try:
        if not obj.bNoSkillStacking or obj.Name != "Behavior_ActivateSkill_1594" \
                or obj._path_name().replace(":", ".") != CI_ACTIVATE_NODE:
            return None
        ctx = args.ContextObject
        if ctx is None:
            return None
        try:
            # TPS's node had bIgnoreSkillInstigator False; BL2's class has no such property (AttributeError, 2026-10-03)
            ctx.Behavior_DeactivateSkill(SkillToDeactivate=obj.SkillToActivate, bIgnoreSkillInstigator=False)
        except Exception:  # noqa: BLE001
            _log_once("ci:deact", "Compound Interest: this enemy's previous mark could not be ended: "
                      + traceback.format_exc().splitlines()[-1])
        obj.bNoSkillStacking = False
        try:
            func(args)
        finally:
            obj.bNoSkillStacking = True
        if _fired.get("ci:multi", 0) < 3:
            _fired["ci:multi"] = _fired.get("ci:multi", 0) + 1
            diag.info(f"{TAG} Compound Interest: marked {getattr(ctx, 'Pawn', ctx)} without ending the other marks")
        return Block
    except Exception:  # noqa: BLE001
        _log_once("ci:activate", "Compound Interest activation hook failed: " + traceback.format_exc().splitlines()[-1])
        return None


def _ci_slot(node: UObject) -> str:
    """The Compounding sequence a node belongs to, by the node's numeric suffix (Compounding1's
    nodes are _76/_103, 2's _73/_97, 3's _72/_96, 4's _71/_95)."""
    n = node.Name.rsplit("_", 1)[-1]
    return {"76": "1", "103": "1", "73": "2", "97": "2", "72": "3", "96": "3", "71": "4", "95": "4"}.get(n, n)


def _ci_node_is_ours(node: UObject) -> bool:
    try:
        return node.Outer._path_name().replace(":", ".") == CI_BPD
    except Exception:
        return False


def _ci_pawn_of(ctx: UObject | None) -> UObject | None:
    """The enemy behind a behavior context (the pawn itself, its controller, or an instance-data
    wrapper carrying an Owner/Pawn)."""
    if ctx is None:
        return None
    for attr in (None, "Pawn", "Owner", "Instigator"):
        o = ctx if attr is None else getattr(ctx, attr, None)
        if o is not None and hasattr(o, "GetMaxHealth") and hasattr(o, "Controller"):
            return o
    return None


def _ci_destroy(path: str, why: str) -> bool:
    """Destroy() is refused on these projectiles (returns False, like on the clones), so hide
    the counter at once and let the engine's own LifeSpan tick delete it. The path stays in
    _ci_known: a refused Destroy leaves the object alive, and forgetting it made the next spawn
    scan re-adopt the corpse's counter as the new one (v56 test: every mark after the first
    booked a stale projectile and hid the real one)."""
    try:
        proj = unrealsdk.find_object("WillowServerSideProjectile", path) if path else None
    except Exception:
        proj = None                                       # already gone
    if proj is None:
        return False
    try:
        ok = proj.Destroy()
        if not ok:
            proj.SetHidden(True)
            proj.LifeSpan = 0.1
        if _fired.get("ci:destroyed", 0) < 6:
            diag.info(f"{TAG} Compound Interest counter {proj.Name} removed ({why}, Destroy -> {ok})")
        _fired["ci:destroyed"] = _fired.get("ci:destroyed", 0) + 1
        return True
    except Exception:
        _log_once("ci:destroy", "counter destroy failed: " + traceback.format_exc().splitlines()[-1])
        return False


def _ci_prune_known() -> None:
    if len(_ci_known) < 400:
        return
    for path in list(_ci_known):
        try:
            unrealsdk.find_object("WillowServerSideProjectile", path)
        except Exception:
            _ci_known.discard(path)


def _ci_release(pawn_name: str, slot: str | None, why: str) -> int:
    n = 0
    for key in [k for k in _ci_counters if k[0] == pawn_name and (slot is None or k[1] == slot)]:
        path = _ci_counters.pop(key)
        if _ci_destroy(path, why):
            n += 1
    return n


@hook("WillowGame.Behavior_SpawnProjectile:ApplyBehaviorToContext", Type.POST, hook_identifier="doppel_ci_spawn")
def _on_ci_spawn(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_ci_spawn")
    """A Compounding sequence just spawned its counter: adopt it, attach it to the enemy."""
    if not _ci_node_is_ours(obj):
        return
    try:
        pawn = _ci_pawn_of(args.ContextObject) or _ci_pawn_of(getattr(args, "SelfObject", None))
        slot = _ci_slot(obj)
        fresh = []
        pending, _ci_pending[:] = list(_ci_pending), []
        for path in pending:
            if path in _ci_known:
                continue
            try:
                proj = unrealsdk.find_object("WillowServerSideProjectile", path)
            except Exception:  # noqa: BLE001
                continue
            _ci_known.add(path)
            fresh.append((path, proj))
        if not fresh and not pending:
            # nothing came through PostBeginPlay (a projectile class the hook does not cover?): the
            # old scan, once, so the feature keeps working while the log says why it cost a frame
            _log_once("ci:scan", "Compound Interest: no counter through PostBeginPlay, scanning projectiles")
            for proj in unrealsdk.find_all("WillowServerSideProjectile", exact=False):
                d = proj.Definition
                if d is None or d.Name != CI_PROJECTILE or proj.Name.startswith("Default__"):
                    continue
                path = proj._path_name()
                if path not in _ci_known:
                    _ci_known.add(path)
                    fresh.append((path, proj))
        if not fresh:
            _log_once("ci:nospawn", f"Compound Interest spawn node {obj.Name} ran but no new counter appeared (context {args.ContextObject})")
            return
        if pawn is None:
            _log_once("ci:nopawn", f"Compound Interest spawn: no pawn behind context {args.ContextObject} / self {getattr(args, 'SelfObject', None)}")
            return
        _ci_prune_known()
        try:
            dead = pawn.GetHealth() <= 0 or not pawn.IsAliveAndWell()
        except Exception:
            dead = False
        for path, proj in fresh:
            if dead:                                          # the killing shot re-marks the corpse
                _ci_destroy(path, "marked a dead enemy")
                continue
            old = _ci_counters.pop((pawn.Name, slot), None)
            if old:
                _ci_destroy(old, "slot reused")
            _ci_counters[(pawn.Name, slot)] = path
            try:
                proj.Physics = 0                              # PHYS_None: ride the base
                proj.SetBase(NewBase=pawn)
            except Exception:
                _log_once("ci:attach", "counter attach failed: " + traceback.format_exc().splitlines()[-1])
        if dead:
            return
        if _fired.get("ci:spawn", 0) < 4:
            diag.info(f"{TAG} Compound Interest counter {fresh[-1][1].Name} on {pawn.Name} slot {slot} (attached, base {fresh[-1][1].Base})")
        _fired["ci:spawn"] = _fired.get("ci:spawn", 0) + 1
    except Exception:
        _log_once("ci:spawnhook", "Compound Interest spawn hook failed: " + traceback.format_exc().splitlines()[-1])


@hook("GearboxFramework.Behavior_Destroy:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_ci_destroy")
def _on_ci_destroy(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_ci_destroy")
    """The sequence's own Destroy of its counter (its FX_ProjectileN variable is empty in BL2)."""
    if not _ci_node_is_ours(obj):
        return
    try:
        slot = _ci_slot(obj)
        pawn = _ci_pawn_of(getattr(args, "SelfObject", None)) or _ci_pawn_of(args.ContextObject)
        if _fired.get("ci:destroynode", 0) < 4:
            diag.info(f"{TAG} Compound Interest Destroy node {obj.Name}: context {args.ContextObject}, self {getattr(args, 'SelfObject', None)}, pawn {pawn.Name if pawn is not None else None}")
        _fired["ci:destroynode"] = _fired.get("ci:destroynode", 0) + 1
        if pawn is not None:
            _ci_release(pawn.Name, slot, f"sequence {slot} ended")
        else:
            for key in [k for k in _ci_counters if k[1] == slot]:
                _ci_destroy(_ci_counters.pop(key), f"sequence {slot} ended (no pawn)")
    except Exception:
        _log_once("ci:destroyhook", "Compound Interest destroy hook failed: " + traceback.format_exc().splitlines()[-1])


def on_enemy_died(pawn: UObject) -> None:
    """The counter must not outlive the enemy: deactivate the enemy-hosted skill (its own
    cleanup chain) and drop every counter booked on the pawn."""
    name = pawn.Name
    for pc in _jack_controllers():
        try:
            for sk in list(pc.GetSkillManager().ActiveSkills):
                d = sk.Definition
                if d is None or d.Name != CI_ACTIVE_SKILL:
                    continue
                inst = sk.SkillInstigator
                host = getattr(inst, "Pawn", None) if inst is not None else None
                if (inst is not None and inst.Name == name) or (host is not None and host.Name == name):
                    sk.Deactivate()
        except Exception:
            _log_once("ci:deactivate", "Compound Interest deactivate on death failed: " + traceback.format_exc().splitlines()[-1])
    n = _ci_release(name, None, "enemy died")
    if n and _fired.get("ci:died", 0) < 6:
        diag.info(f"{TAG} Compound Interest: {n} counter(s) removed with {name}")
        _fired["ci:died"] = _fired.get("ci:died", 0) + 1


# ---------------------------------------------------------------- Compound Interest pays out cash
# Optional, off by default (user, 2026-09-25): when Compound Interest pays out, the enemy also drops
# one on-level money pile per 100% on its counter, rounded down (170% -> 1, 210% -> 2, 1400% -> 14).
# The payout is the CauseDamage at the end of each Compounding sequence's Damaged chain (different
# gun -> particle -> sound -> damage -> sequence disabled -> counter destroyed), so at that node the
# slot's counter is still booked in _ci_counters. Its number is the NumberValue parameter the
# counter's own graph writes on its particle every second (starts at 100). The piles come from
# BL2's own single-drop pool (91% small, 9% big) at the enemy's game stage, spawned from the next
# player tick rather than inside the damage call.
CI_PAYOUT_NODES = frozenset(("Behavior_CauseDamage_76", "Behavior_CauseDamage_73",
                             "Behavior_CauseDamage_72", "Behavior_CauseDamage_71"))      # Compounding 1-4
CI_MONEY_POOL = "GD_Itempools.AmmoAndResourcePools.Pool_Money_1"
CI_MONEY_MAX = 20               # a guard, not a rule: the counter tops out around 1400%
_ci_cash_option = None
_ci_cash_queue: list = []       # (x, y, z, game stage, piles, label)


def ci_cash_enabled() -> bool:
    return _ci_cash_option is not None and bool(_ci_cash_option.value)


def _ci_counter_value(path: str) -> float | None:
    """The number the counter shows: NumberValue on its particle component."""
    try:
        proj = unrealsdk.find_object("WillowServerSideProjectile", path)
    except Exception:  # noqa: BLE001
        return None
    comps = []
    for attr in ("Components", "AllComponents"):
        try:
            comps += [c for c in getattr(proj, attr) if c is not None]
        except Exception:  # noqa: BLE001
            pass
    for comp in comps:
        try:
            if comp.Class.Name != "ParticleSystemComponent":
                continue
            for p in comp.InstanceParameters:
                if str(p.Name) == "NumberValue":
                    return float(p.Scalar)
            ret = comp.GetFloatParameter(InName="NumberValue", OutFloat=0.0)
            if isinstance(ret, tuple) and ret[0]:
                return float(ret[-1])
        except Exception:  # noqa: BLE001
            continue
    return None


def _ci_payout_counter(slot: str, args: WrappedStruct) -> str | None:
    """The counter a payout belongs to. The payout node's context is the player's controller, not
    the enemy (first test, 2026-09-25), so the enemy cannot be read off the call. The counter is
    found by its slot instead; with more than one enemy carrying that slot, the one named by the
    call's other objects or, failing that, the one the player's gun hit a moment ago (the payout
    runs inside that hit's Damaged event) is the one."""
    keys = [k for k in _ci_counters if k[1] == slot]
    if len(keys) <= 1:
        return _ci_counters[keys[0]] if keys else None
    names = []
    for attr in ("ContextObject", "SelfObject", "MyInstigatorObject", "OtherEventParticipantObject"):
        pawn = _ci_pawn_of(getattr(args, attr, None))
        if pawn is not None:
            names.append(pawn.Name)
    now = time.monotonic()
    for when, path in sorted(((w, p) for p, (w, _gun) in _ci_last_damage.items() if now - w < 0.25), reverse=True):
        names.append(path.rsplit(".", 1)[-1])
    for name in names:
        if (name, slot) in _ci_counters:
            return _ci_counters[(name, slot)]
    return None


def _ci_book_payout(node: UObject, args: WrappedStruct) -> None:
    slot = _ci_slot(node)
    if "ci:cashargs" not in _logged:
        bits = []
        for attr in ("ContextObject", "SelfObject", "MyInstigatorObject", "OtherEventParticipantObject"):
            try:
                o = getattr(args, attr, None)
                bits.append(f"{attr}={o.Name if o is not None else None}")
            except Exception:  # noqa: BLE001
                bits.append(f"{attr}=?")
        _log_once("ci:cashargs", f"Compound Interest cash: payout node {node.Name} (slot {slot}): {', '.join(bits)}; "
                                 f"counters booked {sorted(_ci_counters)}")
    path = _ci_payout_counter(slot, args)
    if path is None:
        _log_once("ci:cashnone", f"Compound Interest cash: no counter booked for slot {slot}; nothing dropped", always=True)
        return
    value = _ci_counter_value(path)
    try:
        proj = unrealsdk.find_object("WillowServerSideProjectile", path)
    except Exception:  # noqa: BLE001
        proj = None
    if value is None or proj is None:
        seen = []
        for attr in ("Components", "AllComponents"):
            try:
                for c in getattr(proj, attr):
                    if c is None:
                        continue
                    try:
                        params = [str(x.Name) for x in c.InstanceParameters]
                    except Exception:  # noqa: BLE001
                        params = "-"
                    seen.append(f"{attr}:{c.Class.Name} {c.Name} params={params}")
            except Exception:  # noqa: BLE001
                seen.append(f"{attr}: unreadable")
        _log_once("ci:cashvalue", f"Compound Interest cash: counter {path} gave no NumberValue; nothing dropped. "
                                  f"Components: {seen[:12]}", always=True)
        return
    piles = min(CI_MONEY_MAX, int(value // 100))
    if piles <= 0:
        return
    enemy = proj.Base                      # the counter rides its enemy (SetBase in _on_ci_spawn)
    stage = None
    for call in (lambda: enemy.GetGameStage(), lambda: enemy.GetExpLevel()):
        try:
            stage = int(call())
            if stage > 0:
                break
        except Exception:  # noqa: BLE001
            continue
    loc = enemy.Location if enemy is not None else proj.Location
    label = f"{enemy.Name if enemy is not None else proj.Name} {value:.0f}%"
    _ci_cash_queue.append((float(loc.X), float(loc.Y), float(loc.Z), stage, piles, label))


def _ci_cash_tick(pc: UObject) -> None:
    """Drop the booked piles: one Pool_Money_1 roll each, thrown out of where the enemy stood."""
    if not _ci_cash_queue:
        return
    queue, _ci_cash_queue[:] = list(_ci_cash_queue), []
    try:
        pool = unrealsdk.find_object("ItemPoolDefinition", CI_MONEY_POOL)
        kernel = unrealsdk.find_class("ItemPool").ClassDefaultObject
    except Exception:  # noqa: BLE001
        _log_once("ci:cashpool", f"Compound Interest cash: {CI_MONEY_POOL} not resident", always=True)
        return
    for x, y, z, stage, piles, label in queue:
        if stage is None:
            try:
                stage = int(pc.PlayerReplicationInfo.ExpLevel)
            except Exception:  # noqa: BLE001
                stage = 1
        dropped = 0
        for k in range(piles):
            try:
                ret = kernel.SpawnBalancedInventoryFromPool(Definition=pool, GameStage=stage, AwesomeLevel=0, ContextSource=pc.Pawn,
                                                            SpawnedInventory=[], OuterPoolChance=1.0, GameStageVarianceFormula=None,
                                                            bInventoryMayDropOnDeath=True)
                spawned = list((ret[1] if isinstance(ret, tuple) else []) or [])
            except Exception:  # noqa: BLE001
                _log_once("ci:cashspawn", "Compound Interest cash: spawn failed: " + traceback.format_exc().splitlines()[-1], always=True)
                break
            for inv in spawned:
                angle = random.uniform(0.0, 2.0 * math.pi)
                speed = random.uniform(120.0, 260.0)
                try:
                    inv.DropFrom(StartLocation=unrealsdk.make_struct("Vector", X=x, Y=y, Z=z + 60.0),
                                 StartVelocity=unrealsdk.make_struct("Vector", X=math.cos(angle) * speed,
                                                                     Y=math.sin(angle) * speed, Z=random.uniform(250.0, 400.0)),
                                 bTorque=False, Torque=unrealsdk.make_struct("Vector", X=0, Y=0, Z=0), bForceNoRBPhysics=False)
                    dropped += 1
                except Exception:  # noqa: BLE001
                    _log_once("ci:cashdrop", "Compound Interest cash: drop failed: " + traceback.format_exc().splitlines()[-1], always=True)
        if _fired.get("ci:cash", 0) < 8:
            diag.detail(f"{TAG} Compound Interest cash: {label} -> {piles} pile(s) at stage {stage}, {dropped} dropped")
        _fired["ci:cash"] = _fired.get("ci:cash", 0) + 1


# ---------------------------------------------------------------- Delegation
# Delegation's graph runs whole in BL2 (traced 2026-09-14: shield+health damage summed, alive
# clones counted, the player healed by the delegated share, one Projectile_Delegation per
# clone with the clone as Instigator = homing target). The projectile's own graph then deals
# that share to the clone from ProjectileDefinition.OnCloseToTarget; a TPS-only event BL2's
# WillowProjectile never raises (6 spawns, 0 damage nodes). The mod watches the bolts and
# raises the event through the kernel when one reaches its clone.
DELEGATION_BPD = "Quince_Doppel_GreaterGood.Skills.Delegation.BehaviorProviderDefinition_0"
DELEGATION_PROJECTILE = "Projectile_Delegation"
DELEGATION_PROJ_BPD = "Quince_Doppel_GreaterGood.Projectiles.Projectile_Delegation.BehaviorProviderDefinition_0"
DELEGATION_CLOSE = 150.0         # units: the bolts circle their clone for a second or two before they get within 90
DELEGATION_TIMEOUT = 4.0         # seconds: a bolt that never gets close still pays out
_delegation_bolts: dict[str, float] = {}      # projectile path -> spawn time
_delegation_counts = {"heal": 0, "spawn nodes": 0, "bolts adopted": 0, "bolts paid": 0, "bolt damage": 0}
_delegation_seen: set[str] = set()


@hook("WillowGame.Behavior_SpawnProjectile:ApplyBehaviorToContext", Type.POST, hook_identifier="doppel_delegation_spawn")
def _on_delegation_spawn(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_delegation_spawn")
    global _delegation_capture
    try:
        if obj.Outer._path_name().replace(":", ".") != DELEGATION_BPD:
            return
        _delegation_counts["spawn nodes"] += 1
        now = time.monotonic()
        # the bolts this node just spawned, collected by the projectile spawn hook during its call; the scan of every
        # projectile in the game only if the pre-hook did not run (it cost up to 17 ms a bolt with a crowd out)
        captured, _delegation_capture = _delegation_capture, None
        source = captured if captured is not None else unrealsdk.find_all("WillowServerSideProjectile", exact=False)
        for proj in source:
            d = proj.Definition
            if d is None or d.Name != DELEGATION_PROJECTILE or proj.Name.startswith("Default__"):
                continue
            path = proj._path_name()
            if path not in _delegation_seen:
                _delegation_seen.add(path)
                _delegation_bolts[path] = now
                _delegation_counts["bolts adopted"] += 1
        if len(_delegation_seen) > 400:
            _delegation_seen.intersection_update(_delegation_bolts)
    except Exception:
        _log_once("delegation:spawn", "Delegation spawn hook failed: " + traceback.format_exc().splitlines()[-1])


def _delegation_close_to_target(proj: UObject, why: str) -> None:
    """TPS's OnCloseToTarget(ConsumerHandle, CurrentTarget): the bolt's graph deals the delegated
    damage to its clone (CauseDamage_94) and destroys itself."""
    target = proj.HomingActor
    try:
        provider = unrealsdk.find_object("BehaviorProviderDefinition", DELEGATION_PROJ_BPD)
        _kernel().ActivateBehaviorEventFromScript(ConsumerHandle=proj.GetBehaviorConsumerHandle(), ProviderDefinition=provider,
                                                  EventName="OnCloseToTarget", EventOutputToActivate=0, Parameters=event_params(target))
        n = _fired.get("delegation:hit", 0) + 1
        _fired["delegation:hit"] = n
        _delegation_counts["bolts paid"] += 1
        if n <= 4:
            diag.info(f"{TAG} Delegation bolt {proj.Name} -> {target.Name if target is not None else None} ({why}, #{n})")
    except Exception:
        _log_once("delegation:fire", "Delegation OnCloseToTarget failed: " + traceback.format_exc().splitlines()[-1])
    try:
        if not proj.bDeleteMe:                     # Destroy() is refused on these; the graph's Destroy too
            proj.SetHidden(True)
            proj.LifeSpan = 0.1
    except Exception:
        pass


def _effect_lines(label: str, arr) -> list:
    out = []
    try:
        for e in arr:
            a = e.AttributeToModify
            b = e.BaseModifierValue
            parts = [f"C={float(b.BaseValueConstant):g}"]
            try:
                if b.BaseValueAttribute is not None:
                    parts.append(f"attr={b.BaseValueAttribute._path_name()}")
            except Exception:  # noqa: BLE001
                pass
            try:
                if b.InitializationDefinition is not None:
                    parts.append(f"init={b.InitializationDefinition._path_name()}")
            except Exception:  # noqa: BLE001
                pass
            try:
                parts.append(f"scale={float(b.BaseValueScaleConstant):g}")
            except Exception:  # noqa: BLE001
                pass
            tgt = ""
            for f_ in ("EffectTarget", "TargetType"):
                try:
                    tgt = f" target={int(getattr(e, f_))}"
                    break
                except Exception:  # noqa: BLE001
                    continue
            out.append(f"      {label}: {a._path_name() if a is not None else None} {int(e.ModifierType)}{tgt} [{' '.join(parts)}]")
    except Exception:  # noqa: BLE001
        pass
    return out


SPONSORED_BOOSTS = tuple(f"Quince_Doppel_FreeEnterprise.Skills.SponsoredBy_WeaponBoost_{m}"
                         for m in ("Dahl", "Hyperion", "Jakobs", "Maliwan", "Scav", "Tediore", "Torgue", "Vladof"))


def sponsored_slot(kind: str, hud_slot: int | None = None) -> str:
    """TPS draws Sponsored By's eight manufacturer buffs in the large subroutine slot
    (TrackedSkillType TRACKEDSKILL_Subroutine, a type BL2 does not have); the port fell back to
    TRACKEDSKILL_Triggered, the ordinary tray. BL2's other special types are ConsumableBuff (used
    only by Hammerlock's DLC), MeleeOverride and Respond. Live switch so each can be looked at:
    doppelskill sponsoredslot <Triggered|ConsumableBuff|MeleeOverride|Respond> [hud slot]. The HUD
    re-reads the type the next time the buff is added (swap weapons)."""
    try:
        enum = unrealsdk.find_enum("ETrackedSkillType")
        value = getattr(enum, "TRACKEDSKILL_" + kind)
    except Exception:  # noqa: BLE001
        return f"no TRACKEDSKILL_{kind} in this game"
    done = 0
    for path in SPONSORED_BOOSTS:
        try:
            d = unrealsdk.find_object("SkillDefinition", path)
        except Exception:  # noqa: BLE001
            continue
        try:
            d.TrackedSkillType = value
            if hud_slot is not None:
                d.TrackedSkillHUDSlot = hud_slot
            done += 1
        except Exception:  # noqa: BLE001
            pass
    return f"Sponsored By buffs: TrackedSkillType -> {kind}" + (f", HUD slot {hud_slot}" if hud_slot is not None else "") + f" on {done} of 8"


def shield_effects_report(pc: UObject) -> list:
    """For You Have My Shield x Evolution x Bolster (2026-09-25): the shield on the player and on
    each clone - definition, owner, instigator - every effect its definition and parts carry, and
    each holder's max health, so the bonus the player gets from a clone's copy can be traced to
    the attribute it is computed from."""
    from . import digijack
    holders = [("player", pc.Pawn)]
    for e in list(digijack._clones):
        try:
            holders.append((e["path"].split(".")[-1], unrealsdk.find_object("WillowAIPawn", e["path"])))
        except Exception:  # noqa: BLE001
            continue
    lines = []
    for label, pawn in holders:
        if pawn is None:
            continue
        try:
            lines.append(f"{label}: max health {float(pawn.GetMaxHealth()):.0f}, max shield {float(pawn.GetMaxShieldStrength()):.0f}")
        except Exception:  # noqa: BLE001
            lines.append(f"{label}: unreadable")
        try:
            items = [it for it in pawn.EquippedItems if it is not None and "Shield" in str(it.Class.Name)]
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            try:
                dd = it.DefinitionData
                d = dd.ItemDefinition
                lines.append(f"   shield {d._path_name()} owner={it.Owner.Name if it.Owner is not None else None} "
                             f"instigator={it.Instigator.Name if it.Instigator is not None else None}")
                for f_ in ("AttributeEffects", "ExternalAttributeEffects", "AttributeSlotEffects"):
                    lines += _effect_lines(f"def.{f_}", getattr(d, f_, []) or [])
                for pf in ("AlphaItemPartDefinition", "BetaItemPartDefinition", "GammaItemPartDefinition", "DeltaItemPartDefinition",
                           "EpsilonItemPartDefinition", "ZetaItemPartDefinition", "EtaItemPartDefinition", "ThetaItemPartDefinition",
                           "MaterialItemPartDefinition"):
                    part = getattr(dd, pf, None)
                    if part is None:
                        continue
                    for f_ in ("AttributeEffects", "ExternalAttributeEffects"):
                        lines += _effect_lines(f"{part.Name}.{f_}", getattr(part, f_, []) or [])
            except Exception:  # noqa: BLE001
                lines.append("   shield unreadable: " + traceback.format_exc().splitlines()[-1])
    return lines


def delegation_report(pc: UObject) -> list:
    """Where Delegation's chain stops: the heal node (player's share back), the projectile spawn
    nodes, bolts found, bolts paid (OnCloseToTarget raised), the bolts' damage node on the clones.
    Measured 2026-09-25 under enemy fire: 55 / 110 / 110 / 73 / 73 - the chain runs; unpaid bolts
    are ones whose clone died on the way. Self-damage never starts it (SDE_DamagedByEnemy, as TPS).
    The instance data it addresses is logged at registration (on_clone_spawned); the readback
    accessor is not exposed by this SDK build."""
    lines = [f"Delegation: grade {grade('Delegation', pc)}; counts since launch {dict(_delegation_counts)}"]
    from . import digijack
    live = {e["path"] for e in digijack._clones}
    lines.append(f"  live clones: {sorted(p.split('.')[-1] for p in live) or 'none'}")
    return lines


def _delegation_tick(now: float) -> None:
    if not _delegation_bolts:
        return
    for path, t0 in list(_delegation_bolts.items()):
        try:
            proj = unrealsdk.find_object("WillowServerSideProjectile", path)
        except Exception:
            proj = None
        if proj is None or proj.bDeleteMe:
            _delegation_bolts.pop(path, None)
            continue
        try:
            target = proj.HomingActor
            if target is None:
                if now - t0 > DELEGATION_TIMEOUT:
                    _delegation_bolts.pop(path, None)
                    proj.LifeSpan = 0.1
                continue
            a, b = proj.Location, target.Location
            dist = ((a.X - b.X) ** 2 + (a.Y - b.Y) ** 2 + (a.Z - b.Z) ** 2) ** 0.5
            if dist <= DELEGATION_CLOSE or now - t0 > DELEGATION_TIMEOUT:
                _delegation_bolts.pop(path, None)
                _delegation_close_to_target(proj, f"{dist:.0f} units after {now - t0:.2f}s")
        except Exception:
            _delegation_bolts.pop(path, None)
            _log_once("delegation:tick", "Delegation bolt check failed: " + traceback.format_exc().splitlines()[-1])


# ---------------------------------------------------------------- Merger
# The dual wrist beams are BL2's WillowLightProjectileManager beams (Behavior_FireBeam ->
# AddBeam) from the two wrist sockets. TPS aimed them with bFirstPersonSource (a TPS-only
# flag); in BL2 they leave the third-person sockets and land apart. Probe first: what the
# manager holds for them while they fire.
MERGER_BEAM_BPD = "Quince_Doppel_FreeEnterprise.SpecialMoves.SpecialMove_DualLaser.BehaviorProviderDefinition_1"
MERGER_SOCKETS = ("L_WristBeam_Muzzle", "R_WristBeam_Muzzle")
_merger_beams_until = 0.0
# While Merger's wrist beams are live (monotonic time). Their damage comes in as the player's gun damage, the
# damage source of the gun in hand (the beams are the light-projectile manager's), so Sponsored By's bridge took
# every beam hit for a shot: with the Maliwan boost up, Merger applied the Maliwan gun's element (user,
# 2026-09-28), and Compound Interest counted the hits as gun hits. Jack cannot shoot while the move plays, so any
# "gun" hit of his in this window is Merger's.
_merger_live_until = 0.0
MERGER_LIVE_GRACE = 0.3
_merger_probe_lines = 0
_merger_beam_sockets: dict[int, str] = {}     # beam index -> wrist socket it was fired from
MERGER_FORWARD, MERGER_SIDE, MERGER_UP = 60.0, 12.0, 40.0     # beam start in the controller's view frame (calibrated by the probe)


def _v(x) -> str:
    try:
        return f"({x.X:.0f},{x.Y:.0f},{x.Z:.0f})"
    except Exception:
        return str(x)


@hook("WillowGame.Behavior_FireBeam:ApplyBehaviorToContext", Type.POST, hook_identifier="doppel_merger_beam")
def _on_merger_beam(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_merger_beam")
    global _merger_beams_until, _merger_live_until
    try:
        if obj.Outer._path_name().replace(":", ".") != MERGER_BEAM_BPD:
            return
        global _merger_move_beams, _merger_late_destroy
        _merger_move_beams = True
        if (not _merger_infinite and not _merger_move_at and _merger_cancelled_at
                and time.monotonic() - _merger_cancelled_at < 1.5):
            _merger_late_destroy = True      # fired after the key stopped its move: nothing would destroy them
        if not _merger_infinite:
            _merger_live_until = time.monotonic() + 0.5      # the tick keeps it up while a wrist beam is enabled
        _merger_beams_until = time.monotonic() + 5.0
        _merger_beam_sockets.clear()
        global _merger_motion_lines, _merger_draw_lines
        _merger_motion_lines = 0
        _merger_draw_lines = 0
        _merger_prev_origin.clear()
        _merger_draw_sample.clear()
        if _fired.get("merger:fire", 0) < 3:
            diag.info(f"{TAG} Merger beam node {obj.Name} on {args.ContextObject}")
        _fired["merger:fire"] = _fired.get("merger:fire", 0) + 1
    except Exception:
        _log_once("merger:hook", "Merger beam hook failed: " + traceback.format_exc().splitlines()[-1])


# --- Infinite Merger: TPS's bug, as a setting (off by default; 2026-10-01) ------------------------------------------
# In the Pre-Sequel, activating the action skill just as Merger's arms finish rising, before the lasers appear, left
# the lasers firing for good, through gunfire, grenades, menus and vehicles, until Merger was used again (Borderlands
# wiki, Lootlemon). Merger's move (SpecialMove_DualLaser) fires the two beams at its 0.60 s timed event and destroys
# them (Behavior_DestroyBeams_4, every beam of the pawn) at its 4.60 s event and when the move stops (OnStop); the cast
# stopped the move just before 0.60 and the beam start still went through after the stop had run. BL2 refuses the
# action-skill key while a special move plays, so the key during Merger's move does what TPS's did: the move is
# stopped and the skill cast (user, 2026-10-01: ours armed the lasers and cast nothing). That cancel is TPS's own
# behaviour, so it runs with the setting off too (user, same day); only the endless lasers need it on. Pressed
# inside MERGER_GLITCH_WINDOW (the arms finishing their rise, before the lasers), that move's destroy is held back and
# its beams fired anyway, so the lasers stay until the next Merger, whose own destroy ends them, or a map change
# (TPS lost them at a zone transition; BL2 keeps the pawn through fast travel, so they are destroyed there). While
# they run the beam hits count as gun hits, as the bugged lasers did in TPS (they froze on crits with an Excalibastard).
MERGER_MOVE_START = "Quince_Doppel_FreeEnterprise.SpecialMoves.SpecialMove_DualLaser.Behavior_ChangeWeaponVisibility_3"
MERGER_DESTROY = "Quince_Doppel_FreeEnterprise.SpecialMoves.SpecialMove_DualLaser.BehaviorProviderDefinition_1.Behavior_DestroyBeams_4"
MERGER_FIRE_NODES = tuple(f"{MERGER_BEAM_BPD}.Behavior_FireBeam_{n}" for n in (8, 9))
MERGER_GLITCH_WINDOW = [0.3, 0.6]      # seconds after Merger starts (its lasers appear at 0.6); `doppelskill mergerwindow a b`
MERGER_MOVE_SECONDS = 4.6              # the move's own end (its 4.60 destroy)
_merger_infinite_option = None
_merger_move_at = 0.0                  # when the latest Merger move started (monotonic)
_merger_move_beams = False             # whether that move's beams went out
_merger_infinite = False
_merger_fire_pending = False
_merger_infinite_pawn = ""
_merger_cancelled_at = 0.0             # when the key stopped a Merger without the endless lasers
_merger_late_destroy = False           # that move's beams went out after the stop: destroy them on the next tick


def _merger_infinite_on() -> bool:
    try:
        return bool(_merger_infinite_option.value)
    except Exception:  # noqa: BLE001
        return False


@hook("WillowGame.Behavior_ChangeWeaponVisibility:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_merger_start")
def _on_merger_move_start(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    global _merger_move_at, _merger_move_beams, _merger_infinite
    try:
        if obj._path_name().replace(":", ".") != MERGER_MOVE_START:
            return
    except Exception:  # noqa: BLE001
        return
    _merger_move_at = time.monotonic()
    _merger_move_beams = False
    if _merger_infinite:
        _merger_infinite = False         # a new Merger: its own destroy takes the endless lasers with it, as in TPS
        unrealsdk.logging.info(f"{TAG} Merger: used again, the endless lasers end with this one")


def merger_key_during_move(pc: UObject) -> bool:
    """The action-skill key while Merger's move plays: stop the move and cast, as TPS did; with the setting on and
    inside the window the lasers stay. True when it acted (the key then does nothing else)."""
    global _merger_infinite, _merger_infinite_pawn, _merger_move_at, _merger_cancelled_at
    if not _merger_move_at or _merger_infinite:
        return False
    since = time.monotonic() - _merger_move_at
    if since > MERGER_MOVE_SECONDS:
        return False                     # the move is over: the key is the key
    from . import digijack
    pawn = pc.Pawn
    if pawn is None or digijack._active:
        return False                     # the Digi-Jacks are out: the key is On My Mark / the recall
    try:
        if pc.IsActionSkillOnCooldown():
            return False                 # nothing to cast, so nothing cancels Merger (as in TPS)
    except Exception:  # noqa: BLE001
        pass
    glitch = _merger_infinite_on() and MERGER_GLITCH_WINDOW[0] <= since <= MERGER_GLITCH_WINDOW[1]
    if glitch:
        _merger_infinite = True          # before the stop: its destroy is held back and the beams go out anyway
        _merger_infinite_pawn = pawn._path_name()
    else:
        _merger_cancelled_at = time.monotonic()
    _merger_move_at = 0.0
    try:
        pawn.ServerSpecialMove_StopAny()
    except Exception:  # noqa: BLE001
        _log_once("merger:stop", "Merger: stopping the move failed: " + traceback.format_exc().splitlines()[-1])
    # Cast from the next frames, not now: in the same frame the skill still saw the move playing and nothing deployed
    # (user, 2026-10-01: the log said cast, no Digi-Jacks came)
    global _merger_cast_from, _merger_cast_until, _merger_cast_tries
    _merger_cast_from = time.monotonic() + 0.03
    _merger_cast_until = _merger_cast_from + MERGER_CAST_RETRY_SECONDS
    _merger_cast_tries = 0
    unrealsdk.logging.info(f"{TAG} Merger: action skill {since:.2f}s into Merger: move stopped, casting"
                           + ("; the lasers keep firing until Merger is used again" if glitch else ""))
    return True


MERGER_CAST_RETRY_SECONDS = 0.5
# Why nothing deployed: the action skill's first constraint, WeaponActionAvailableExpressionEvaluator, is checked on
# activation, and during Merger (and while the gun comes back up after it) Jack's weapon actions are unavailable, so
# every cast was refused (41 tries in half a second, 2026-10-01). TPS let this cast through; for our own cast only, that
# constraint's on-activation check is off for the length of the call.
ACTION_SKILL_DEF = "GD_Doppelganger_Skills.Action.Skill_DeathTrap"
_constraint_note_logged = False


def _weapon_constraint_on_activation(on: bool) -> bool:
    """Set the weapon-action constraint's on-activation check; True when it is set as asked."""
    global _constraint_note_logged
    skill = _obj(ACTION_SKILL_DEF)
    if skill is None:
        return False
    try:
        cons = skill.SkillConstraints
        for k in range(len(cons)):
            ev = cons[k].Evaluator
            if ev is None or "WeaponActionAvailable" not in ev.Class.Name:
                continue
            cons[k].bApplyConstraintOnActivatation = on
            if bool(skill.SkillConstraints[k].bApplyConstraintOnActivatation) != on:      # not written through: the whole array
                items = list(skill.SkillConstraints)
                items[k].bApplyConstraintOnActivatation = on
                skill.SkillConstraints = items
            ok = bool(skill.SkillConstraints[k].bApplyConstraintOnActivatation) == on
            if not ok and not _constraint_note_logged:
                _constraint_note_logged = True
                unrealsdk.logging.info(f"{TAG} Merger: the weapon-action constraint could not be set to {on}")
            return ok
    except Exception:  # noqa: BLE001
        _log_once("merger:constraint", "Merger: the weapon-action constraint: " + traceback.format_exc().splitlines()[-1])
    return False
_merger_cast_from = 0.0
_merger_cast_until = 0.0
_merger_cast_tries = 0


def merger_cast_tick(pc: UObject) -> None:
    """The cast after Merger's move was stopped: tried each frame until the Digi-Jacks are out or the time is up."""
    global _merger_cast_from, _merger_cast_until, _merger_cast_tries, _merger_late_destroy
    if _merger_late_destroy:
        _merger_late_destroy = False
        if not _merger_infinite and pc.Pawn is not None and _merger_destroy_beams_now(pc.Pawn):
            unrealsdk.logging.info(f"{TAG} Merger: its lasers started after the cancel; destroyed")
    if not _merger_cast_from:
        return
    now = time.monotonic()
    if now < _merger_cast_from:
        return
    from . import digijack
    if digijack._active or now > _merger_cast_until or pc.Pawn is None:
        unrealsdk.logging.info(f"{TAG} Merger: the cast after the stop " + ("deployed" if digijack._active else "did not deploy")
                               + f" ({_merger_cast_tries} tr{'y' if _merger_cast_tries == 1 else 'ies'})")
        _merger_cast_from = 0.0
        return
    _merger_cast_tries += 1
    relaxed = _weapon_constraint_on_activation(False)
    try:
        pc.StartActionSkill()
    except Exception:  # noqa: BLE001
        _merger_cast_from = 0.0
        _log_once("merger:cast", "Merger: the cast after the stop failed: " + traceback.format_exc().splitlines()[-1])
    finally:
        if relaxed:
            _weapon_constraint_on_activation(True)


def _merger_destroy_beams_now(pawn: UObject) -> bool:
    """Merger's own destroy node (every beam of the pawn), run by hand."""
    try:
        node = unrealsdk.find_object("Behavior_DestroyBeams", MERGER_DESTROY)
        node.ApplyBehaviorToContext(ContextObject=pawn, KernelInfo=unrealsdk.make_struct("BehaviorKernelInfo"), SelfObject=pawn,
                                    MyInstigatorObject=pawn, OtherEventParticipantObject=None,
                                    EventData=unrealsdk.make_struct("BehaviorParameters"))
        return True
    except Exception:  # noqa: BLE001
        _log_once("merger:end", "Merger: destroying the lasers failed: " + traceback.format_exc().splitlines()[-1])
        return False


def merger_end_infinite(why: str) -> None:
    """The endless lasers go: their flag first (so the destroy is not held back), then every beam of the pawn."""
    global _merger_infinite, _merger_fire_pending
    if not _merger_infinite:
        return
    _merger_infinite = _merger_fire_pending = False
    pc = _pc()
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return
    if _merger_destroy_beams_now(pawn):
        unrealsdk.logging.info(f"{TAG} Merger: the endless lasers end ({why})")


@hook("WillowGame.Behavior_DestroyBeams:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_merger_destroy")
def _on_merger_destroy_beams(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    global _merger_fire_pending, _merger_move_at
    try:
        if obj._path_name().replace(":", ".") != MERGER_DESTROY:
            return None
    except Exception:  # noqa: BLE001
        return None
    if not _merger_infinite:
        _merger_move_at = 0.0            # the move is over (its 4.60 event, or stopped): the key is the key again
        return None
    if not _merger_move_beams:
        _merger_fire_pending = True      # the cast stopped the move before its beams: they go out anyway
    return Block


def merger_infinite_tick(pc: UObject) -> None:
    global _merger_infinite, _merger_fire_pending, _merger_beams_until
    if not _merger_infinite:
        return
    pawn = pc.Pawn
    if pawn is None or pawn._path_name() != _merger_infinite_pawn:
        _merger_infinite = False         # a new pawn (death, a level): its beams went with the old one
        _merger_fire_pending = False
        return
    _merger_beams_until = max(_merger_beams_until, time.monotonic() + 1.0)      # keep steering them
    if not _merger_fire_pending:
        return
    _merger_fire_pending = False
    fired = 0
    for path in MERGER_FIRE_NODES:
        try:
            node = unrealsdk.find_object("Behavior_FireBeam", path)
            node.ApplyBehaviorToContext(ContextObject=pawn, KernelInfo=unrealsdk.make_struct("BehaviorKernelInfo"), SelfObject=pawn,
                                        MyInstigatorObject=pawn, OtherEventParticipantObject=None,
                                        EventData=unrealsdk.make_struct("BehaviorParameters"))
            fired += 1
        except Exception:  # noqa: BLE001
            _log_once("merger:glitchfire", "Merger: firing the endless lasers failed: " + traceback.format_exc().splitlines()[-1])
    unrealsdk.logging.info(f"{TAG} Merger: the cast stopped the move before its lasers; {fired} fired anyway")


_merger_manager_path: str | None = None
_merger_impact_path: str | None = None
MERGER_IMPACT_NAMES = ("Bullet_Laser_Shock", "Bullet_Shock", "Bullet_Shock_Impact", "Bullet_Impact_Shock")


def _merger_impact_def() -> UObject | None:
    """TPS's wrist beams carry GD_Cork_Impacts.Bullets.Bullet_Laser_Shock (hit flash/decal/sound);
    ours were imported with none. Use it if it came along, else BL2's own shock bullet impact."""
    global _merger_impact_path
    if _merger_impact_path == "":
        return None
    if _merger_impact_path:
        try:
            return unrealsdk.find_object("WillowImpactDefinition", _merger_impact_path)
        except Exception:
            _merger_impact_path = None
    found = {}
    try:
        for d in unrealsdk.find_all("WillowImpactDefinition", exact=False):
            n = d.Name
            if n in MERGER_IMPACT_NAMES and n not in found:
                found[n] = d
    except Exception:
        pass
    for n in MERGER_IMPACT_NAMES:
        if n in found:
            _merger_impact_path = found[n]._path_name()
            diag.info(f"{TAG} Merger impact definition: {_merger_impact_path}")
            return found[n]
    _merger_impact_path = ""
    diag.info(f"{TAG} Merger impact definition: none of {MERGER_IMPACT_NAMES} is loaded")
    return None


def _merger_manager(pawn: UObject) -> UObject | None:
    global _merger_manager_path
    if _merger_manager_path is not None:
        try:
            mgr = unrealsdk.find_object("WillowLightProjectileManager", _merger_manager_path)
            if mgr.PawnOwner is not None and mgr.PawnOwner.Name == pawn.Name:
                return mgr
        except Exception:
            pass
        _merger_manager_path = None
    for mgr in unrealsdk.find_all("WillowLightProjectileManager", exact=False):
        try:
            if mgr.PawnOwner is not None and mgr.PawnOwner.Name == pawn.Name:
                _merger_manager_path = mgr._path_name()
                return mgr
        except Exception:
            continue
    return None


def _aim_point(pc: UObject, pawn: UObject, max_range: float = 8000.0):
    """Where the crosshair lands: FastTrace bisection along the camera ray (Trace() never hits
    world geometry through the SDK, FastTrace only answers clear/blocked)."""
    cam = pawn.GetPawnViewLocation()
    rot = pc.Rotation
    pitch, yaw = rot.Pitch * math.pi / 32768.0, rot.Yaw * math.pi / 32768.0
    d = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    def at(t):
        return unrealsdk.make_struct("Vector", X=cam.X + d[0] * t, Y=cam.Y + d[1] * t, Z=cam.Z + d[2] * t)
    lo, hi = 60.0, max_range                      # start outside the pawn's own cylinder
    if pc.FastTrace(TraceEnd=at(hi), TraceStart=at(lo)):
        return at(hi)
    for _ in range(13):
        mid = (lo + hi) / 2
        if pc.FastTrace(TraceEnd=at(mid), TraceStart=at(lo)):
            lo = mid
        else:
            hi = mid
    return at(hi)


def _fp_socket(pawn: UObject, socket: str):
    """World location of a socket on Jack's first-person arms."""
    from .characterslot import _find_hands_component
    comp = _find_hands_component(pawn)
    if comp is None:
        return None
    zero = unrealsdk.make_struct("Vector", X=0, Y=0, Z=0)
    rot0 = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0)
    r = comp.GetSocketWorldLocationAndRotation(InSocketName=socket, OutLocation=zero, OutRotation=rot0)
    if isinstance(r, tuple):
        for v in r:
            if hasattr(v, "X"):
                return v
    return None


_merger_proxy_path: str | None = None


def _merger_proxy(pawn: UObject) -> UObject | None:
    """An invisible Emitter the beams lock on to (BeamLocationData.Actor); moved to the view
    ray every tick, so both beams converge on the crosshair and follow the pitch."""
    global _merger_proxy_path
    proxy = None
    if _merger_proxy_path:
        try:
            proxy = unrealsdk.find_object("Emitter", _merger_proxy_path)
            if proxy.bDeleteMe:
                proxy = None
        except Exception:
            proxy = None
    if proxy is None:
        loc = pawn.Location
        tried = []
        for cname in ("Emitter", "TargetPoint", "Note", "DynamicSMActor"):
            try:
                cls = unrealsdk.find_class(cname)
                proxy = pawn.Spawn(SpawnClass=cls, SpawnOwner=pawn, SpawnTag="None", SpawnLocation=loc, SpawnRotation=pawn.Rotation,
                                   ActorTemplate=None, bNoCollisionFail=True)
                tried.append(f"{cname}:{proxy.Name if proxy is not None else None}")
                if proxy is not None:
                    break
            except Exception as e:
                tried.append(f"{cname}:{e!r}"[:80])
        _merger_proxy_path = proxy._path_name() if proxy is not None else None
        _log_once("merger:proxy:" + str(proxy is not None), f"Merger: beam target proxy spawn {tried}")
    return proxy


MERGER_BODY = "Quince_Char_Doppelganger.Mesh.Skel_Doppelganger_Body"
MERGER_AIM_SOCKETS = {"L_WristBeam_Muzzle": "FX_Mark", "R_WristBeam_Muzzle": "MissionDirectParticle"}   # Root-bone sockets of Jack's body, re-aimed while the beams run ('Spawn' never took a rotation)
_merger_socket_backup: dict[str, tuple] = {}
_merger_yaw_trim: dict[str, int] = {}          # (old feedback trim; kept for the fallback path only)
_merger_bone_failed = False                    # GetBoneLocation/GetBoneQuaternion unavailable -> pawn-frame fallback


def _quat_axes(q):
    """Rows = the X/Y/Z axes of a UE3 quaternion's rotation, in world space."""
    x, y, z, w = float(q.X), float(q.Y), float(q.Z), float(q.W)
    x2, y2, z2 = x + x, y + y, z + z
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2
    return ((1.0 - (yy + zz), xy + wz, xz - wy),
            (xy - wz, 1.0 - (xx + zz), yz + wx),
            (xz + wy, yz - wx, 1.0 - (xx + yy)))


def _rot_axes(pitch: int, yaw: int, roll: int = 0):
    """Rows = the axes of UE3's FRotationMatrix(Rotator)."""
    k = math.pi / 32768.0
    sp, cp = math.sin(pitch * k), math.cos(pitch * k)
    sy, cy = math.sin(yaw * k), math.cos(yaw * k)
    sr, cr = math.sin(roll * k), math.cos(roll * k)
    return ((cp * cy, cp * sy, sp),
            (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp),
            (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp))


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _axes_to_rotator(ax) -> tuple[int, int, int]:
    """UE3 FMatrix::Rotator() on a row-axes matrix, in Unreal rotation units."""
    X, Y, Z = ax
    pitch = math.atan2(X[2], math.sqrt(X[0] * X[0] + X[1] * X[1]))
    yaw = math.atan2(X[1], X[0])
    sy_axis = _rot_axes(int(pitch * 32768.0 / math.pi), int(yaw * 32768.0 / math.pi), 0)[1]
    roll = math.atan2(_dot(Z, sy_axis), _dot(Y, sy_axis))
    k = 32768.0 / math.pi
    return int(round(pitch * k)) & 0xFFFF, int(round(yaw * k)) & 0xFFFF, int(round(roll * k)) & 0xFFFF


def _place_socket_world(mesh: UObject, so: UObject, origin, pitch: int, yaw: int) -> bool:
    """Set a socket's RelativeLocation/Rotation so that it sits at `origin` facing (pitch, yaw)
    in world space RIGHT NOW, from its bone's actual world transform (GetBoneLocation /
    GetBoneQuaternion, Space 0 = world). The body mesh's yaw lags the pawn's and its Root bone
    rides the jump/strafe animations, which is what made a pawn-frame offset drift against
    the first-person view (2026-09-15)."""
    bone = str(so.BoneName)
    bl = mesh.GetBoneLocation(BoneName=bone, Space=0)
    bq = mesh.GetBoneQuaternion(BoneName=bone, Space=0)
    B = _quat_axes(bq)                                   # bone axes in world
    d = (origin.X - bl.X, origin.Y - bl.Y, origin.Z - bl.Z)
    try:                                                 # the relative location is scaled by the component
        s3 = mesh.Scale3D
        sx, sy_, sz = float(mesh.Scale) * float(s3.X), float(mesh.Scale) * float(s3.Y), float(mesh.Scale) * float(s3.Z)
    except Exception:
        sx = sy_ = sz = 1.0
    so.RelativeLocation = unrealsdk.make_struct("Vector", X=_dot(d, B[0]) / (sx or 1.0), Y=_dot(d, B[1]) / (sy_ or 1.0), Z=_dot(d, B[2]) / (sz or 1.0))
    D = _rot_axes(pitch, yaw, 0)                         # desired socket axes in world
    # relative = desired * bone^-1 (row-vector convention): row i of R_rel = D[i] expressed in bone axes
    R = tuple((_dot(D[i], B[0]), _dot(D[i], B[1]), _dot(D[i], B[2])) for i in range(3))
    p_, y_, r_ = _axes_to_rotator(R)
    so.RelativeRotation = unrealsdk.make_struct("Rotator", Pitch=p_, Yaw=y_, Roll=r_)
    return True


_body_socket_paths: dict[str, str] = {}      # socket name -> object path (a find_all over every SkeletalMeshSocket per beam per tick cost ~45 ms a frame, 2026-09-15)


def _body_socket(name: str) -> UObject | None:
    path = _body_socket_paths.get(name)
    if path is not None:
        try:
            return unrealsdk.find_object("SkeletalMeshSocket", path)
        except Exception:
            _body_socket_paths.pop(name, None)
    for so in unrealsdk.find_all("SkeletalMeshSocket", exact=False):
        try:
            if str(so.SocketName) == name and so._path_name().replace(":", ".").startswith(MERGER_BODY + "."):   # Outer:Sub paths
                _body_socket_paths[name] = so._path_name()
                return so
        except Exception:
            continue
    return None


def _merger_restore_sockets() -> None:
    for name, (loc, rot) in list(_merger_socket_backup.items()):
        so = _body_socket(name)
        if so is not None:
            so.RelativeLocation = unrealsdk.make_struct("Vector", X=loc[0], Y=loc[1], Z=loc[2])
            so.RelativeRotation = unrealsdk.make_struct("Rotator", Pitch=rot[0], Yaw=rot[1], Roll=rot[2])
    _merger_socket_backup.clear()
    _release_own_beams()


# --- Merger's beams drawn by us (user, 2026-10-01) ------------------------------------------------------------------
# TPS's lasers are snappy: they always come from the wrists and always end on the crosshair. Ours twisted on curved
# surfaces and left from all sorts of places in fast turns. The manager draws its beams from its own start (the
# borrowed body sockets, read on its own schedule) to its own trace end (each beam on its own line, and the reach fed
# back from those ends), and nothing in its per-frame update can be hooked. So the beams are drawn here: one copy of
# the beam particle per wrist, from the emitter pool, whose start and end this tick writes every frame (a particle
# reads its parameters after the controller's tick, so they land in the same frame): the start where the gauntlet is
# drawn (_beam_origin), the end where the crosshair lands, shared by both beams. That point comes from the manager's
# own beams, which go on dealing the damage, hidden: they run straight down the crosshair ray from just ahead of the
# camera, so their trace end is the crosshair's impact, enemies and curved surfaces included (a FastTrace search
# came back blocked a few units out and the beams met right in front of Jack, user 2026-10-01).
MERGER_OWN_BEAMS = True                  # `doppelskill beamown on|off`
_own_beam_psc: dict = {}                 # wrist socket -> path of our beam ParticleSystemComponent
_hidden_mgr_psc: set = set()             # paths of the manager's beam components hidden for ours
_own_beam_emitters: dict = {}            # wrist socket -> indices of the template's beam emitters
MERGER_RAY_FORWARD = 60.0                # the damage beams start this far down the crosshair ray (clear of Jack's own cylinder)
_beam_probe_frames = 0                   # `doppelskill beamprobe [frames]`: what the damage beams do, frame by frame
_beam_probe_dumped = False


def _beam_probe_dump(mgr, b0) -> None:
    """Once per probe: every field of one damage beam's state and the manager's reflection/trace settings."""
    def fields(obj, keys=None):
        out_ = []
        for n in sorted(dir(obj)):
            if n.startswith("_") or (keys and not any(k in n.lower() for k in keys)):
                continue
            try:
                v = getattr(obj, n)
            except Exception:  # noqa: BLE001
                continue
            if callable(v) and not hasattr(v, "X"):
                continue
            out_.append(f"{n}={v}")
        return out_
    try:
        unrealsdk.logging.info(f"{TAG} beamprobe beam fields: " + "; ".join(fields(b0)))
        unrealsdk.logging.info(f"{TAG} beamprobe core fields: " + "; ".join(fields(b0.CoreBeamState)))
        unrealsdk.logging.info(f"{TAG} beamprobe manager: " + "; ".join(fields(mgr, ("reflect", "trace", "range", "length", "bounce", "ricochet", "impact"))))
    except Exception as e_:  # noqa: BLE001
        unrealsdk.logging.info(f"{TAG} beamprobe dump failed: {e_!r}")


# Attached (user, 2026-10-01: the start strayed from the wrists looking up and down, strafing and jumping): computing
# the start in the controller's tick means estimating the camera the arms will be drawn with (eye height, bob, the
# pawn's move later in the frame), and the FOV push-out multiplies the error by 3.45. The engine places the arms
# natively (CalcCamera, GetPlayerViewPoint: nothing to hook), so the beam particle is attached to the first-person arms
# at the wrist socket instead: the engine carries it with the arms in the same transform update. All that is set from
# here is its offset from the socket (the push-out, (k-1) x the socket's side/up offset from the camera, written in the
# socket's own frame), which depends on the arms' pose against the camera, not on how the camera moves.
MERGER_ATTACH_BEAMS = True               # `doppelskill beamattach on|off`
_own_beam_attached: dict = {}            # wrist socket -> path of the arms component our beam hangs on
# The push-out was re-measured raw every frame against last tick's camera, which the pawn's move during the frame put
# ~7 units off when strafing; x2.45 and different every frame, the beam shook on top of the arms' smooth motion (user,
# 2026-10-01: "super jittery"). The camera is now where the arms were placed (last tick's eye + the move since), and the
# offset, which only changes with the arms' pose, is smoothed in the socket's frame.
MERGER_PUSH_TAU = 0.25                   # s; `doppelskill beampush x`
_own_beam_push: dict = {}                # wrist socket -> (smoothed local offset, time)
_own_beam_counter = 0


def _rot_axes_r(rot):
    """UE3 FRotationMatrix rows: forward, right, up of a rotator (with roll).

    Named _rot_axes until 2026-10-06, which silently replaced the (pitch, yaw, roll) _rot_axes above that
    _place_socket_world calls: every remote Jack's Merger beam steer failed with a TypeError (logged only with diag),
    and the host fired a joined Jack's beams along his wrists, off to one side."""
    p, y, r = (float(rot.Pitch) * math.pi / 32768.0, float(rot.Yaw) * math.pi / 32768.0, float(rot.Roll) * math.pi / 32768.0)
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return ((cp * cy, cp * sy, sp),
            (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp),
            (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp))


def _drive_attached_beam(pc: UObject, socket: str, b, end) -> bool:
    global _own_beam_counter
    pawn = pc.Pawn
    from .characterslot import _find_hands_component
    hands = _find_hands_component(pawn) if pawn is not None else None
    if hands is None:
        return False
    psc = None
    path = _own_beam_psc.get(socket)
    if path:
        try:
            psc = unrealsdk.find_object("ParticleSystemComponent", path)
        except Exception:  # noqa: BLE001
            psc = None
    src_psc = b.PartRef
    if psc is None:
        if src_psc is None or src_psc.Template is None:
            return False
        try:
            _own_beam_counter += 1
            psc = unrealsdk.construct_object("ParticleSystemComponent", hands, f"DoppelMergerBeam_{_own_beam_counter}", 0, None)
            psc.Template = src_psc.Template
            hands.AttachComponentToSocket(psc, socket)
        except Exception:  # noqa: BLE001
            _log_once("merger:attach", "Merger: attaching our beam to the arms failed, drawing it free: " + traceback.format_exc().splitlines()[-1])
            return False
        try:
            psc.SetScale(NewScale=MERGER_BEAM_SCALE)
            for ip in src_psc.InstanceParameters:
                if int(ip.ParamType) == 1:
                    psc.SetFloatParameter(ParameterName=ip.Name, Param=float(ip.Scalar))
                elif int(ip.ParamType) == 3:
                    psc.SetVectorParameter(ParameterName=ip.Name, Param=ip.Vector)
        except Exception:  # noqa: BLE001
            pass
        psc.ActivateSystem()
        _own_beam_psc[socket] = psc._path_name()
        _own_beam_attached[socket] = hands._path_name()
        idx = []
        try:
            for k, em in enumerate(psc.Template.Emitters):
                td = em.LODLevels[0].TypeDataModule if em is not None and len(em.LODLevels) else None
                if td is not None and "Beam" in td.Class.Name:
                    idx.append(k)
        except Exception:  # noqa: BLE001
            pass
        _own_beam_emitters[socket] = idx
        if _fired.get("merger:attached", 0) < 2:
            _fired["merger:attached"] = _fired.get("merger:attached", 0) + 1
            unrealsdk.logging.info(f"{TAG} Merger: the {socket} beam hangs on the first-person arms ({psc.Template.Name}, beam emitters {idx})")
    try:                                                     # the manager's own beam: hidden while ours shows
        sp = src_psc._path_name() if src_psc is not None else None
        if sp and not src_psc.HiddenGame:
            src_psc.SetHidden(True)
            _hidden_mgr_psc.add(sp)
    except Exception:  # noqa: BLE001
        pass
    # the push-out from the socket, in the socket's frame; the arms' camera is last tick's (they still sit where it put them)
    zero = unrealsdk.make_struct("Vector", X=0, Y=0, Z=0)
    rot0 = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0)
    r = hands.GetSocketWorldLocationAndRotation(InSocketName=socket, OutLocation=zero, OutRotation=rot0)
    sloc = next((v for v in (r if isinstance(r, tuple) else (r,)) if hasattr(v, "X")), None)
    srot = next((v for v in (r if isinstance(r, tuple) else (r,)) if hasattr(v, "Pitch")), None)
    if sloc is not None and srot is not None:
        # Only last tick's camera: the one kept from the previous cast (another place, or another map) put the first
        # frame's beams hundreds of units out and the smoothing then swept them back to the wrists (user, 2026-10-02).
        prev = _merger_prev_cam
        if prev is not None and time.monotonic() - prev[3] > 0.1:
            prev = None
        if prev is not None:
            try:                                         # the pawn moved after that tick: the arms' camera moved with it
                v = pawn.Velocity
                c = (prev[0][0] + v.X * _last_dt, prev[0][1] + v.Y * _last_dt, prev[0][2] + v.Z * _last_dt)
            except Exception:  # noqa: BLE001
                c = prev[0]
            cam_rot = unrealsdk.make_struct("Rotator", Pitch=prev[1], Yaw=prev[2], Roll=0)
        else:
            e = pawn.GetPawnViewLocation()
            c = (e.X, e.Y, e.Z)
            cam_rot = pc.Rotation
        _, Rc, Uc = _rot_axes_r(cam_rot)
        d = (sloc.X - c[0], sloc.Y - c[1], sloc.Z - c[2])
        k1 = merger_fp_scale(pc) - 1.0
        rr, uu = _dot(d, Rc), _dot(d, Uc)
        dw = tuple(k1 * (rr * Rc[i] + uu * Uc[i]) for i in range(3))
        X, Y, Z = _rot_axes_r(srot)
        try:
            sc = float(hands.Scale) * (float(hands.Scale3D.X) + float(hands.Scale3D.Y) + float(hands.Scale3D.Z)) / 3.0
        except Exception:  # noqa: BLE001
            sc = 1.0
        sc = sc if abs(sc) > 1e-3 else 1.0
        local = (_dot(dw, X) / sc, _dot(dw, Y) / sc, _dot(dw, Z) / sc)
        now = time.monotonic()
        old = _own_beam_push.get(socket)
        if old is not None and MERGER_PUSH_TAU > 0.0 and now - old[1] < 0.5:
            a = min(1.0, (now - old[1]) / MERGER_PUSH_TAU)
            local = tuple(o + (n - o) * a for o, n in zip(old[0], local))
        _own_beam_push[socket] = (local, now)
        last_set = _own_beam_push.get(socket + ":set")
        if last_set is None or max(abs(a_ - b_) for a_, b_ in zip(last_set, local)) > 0.05:
            psc.SetTranslation(NewTranslation=unrealsdk.make_struct("Vector", X=local[0], Y=local[1], Z=local[2]))
            _own_beam_push[socket + ":set"] = local
        start = (sloc.X + dw[0], sloc.Y + dw[1], sloc.Z + dw[2])
    else:
        start = (pawn.Location.X, pawn.Location.Y, pawn.Location.Z)
    dn = _norm((end.X - start[0], end.Y - start[1], end.Z - start[2]))
    dv = unrealsdk.make_struct("Vector", X=dn[0], Y=dn[1], Z=dn[2])
    for k in _own_beam_emitters.get(socket, ()):
        try:
            psc.SetBeamTargetPoint(EmitterIndex=k, NewTargetPoint=end, TargetIndex=0)
            psc.SetBeamEndPoint(EmitterIndex=k, NewEndPoint=end)
        except Exception:  # noqa: BLE001
            _log_once("merger:beampoints", "Merger: setting our beam's end failed: " + traceback.format_exc().splitlines()[-1])
    psc.SetVectorParameter(ParameterName="TargetLocation", Param=end)
    psc.SetVectorParameter(ParameterName="SourceTangent", Param=dv)
    psc.SetVectorParameter(ParameterName="TargetTangent", Param=dv)
    psc.SetVectorParameter(ParameterName="Direction", Param=dv)
    return True


def _drive_own_beam(pc: UObject, socket: str, b, start, end) -> None:
    if MERGER_ATTACH_BEAMS and (socket in _own_beam_attached or socket not in _own_beam_psc):
        if _drive_attached_beam(pc, socket, b, end):
            return
    psc = None
    path = _own_beam_psc.get(socket)
    if path:
        try:
            psc = unrealsdk.find_object("ParticleSystemComponent", path)
        except Exception:  # noqa: BLE001
            psc = None
    src_psc = b.PartRef
    if psc is None:
        if src_psc is None or src_psc.Template is None:
            return
        try:
            psc = pc.WorldInfo.MyEmitterPool.SpawnEmitter(EmitterTemplate=src_psc.Template, SpawnLocation=start,
                                                          SpawnRotation=unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0))
        except Exception:  # noqa: BLE001
            psc = None
        if psc is None:
            _log_once("merger:ownspawn", "Merger: our beam particle did not spawn; the manager's stays")
            return
        try:
            psc.SetScale(NewScale=MERGER_BEAM_SCALE)
            for ip in src_psc.InstanceParameters:            # its colour, damage glow and the rest, as the manager set them
                if int(ip.ParamType) == 1:
                    psc.SetFloatParameter(ParameterName=ip.Name, Param=float(ip.Scalar))
                elif int(ip.ParamType) == 3:
                    psc.SetVectorParameter(ParameterName=ip.Name, Param=ip.Vector)
        except Exception:  # noqa: BLE001
            pass
        _own_beam_psc[socket] = psc._path_name()
        idx = []
        try:
            for k, em in enumerate(psc.Template.Emitters):
                td = em.LODLevels[0].TypeDataModule if em is not None and len(em.LODLevels) else None
                if td is not None and "Beam" in td.Class.Name:
                    idx.append(k)
        except Exception:  # noqa: BLE001
            pass
        _own_beam_emitters[socket] = idx
        if _fired.get("merger:ownbeam", 0) < 2:
            _fired["merger:ownbeam"] = _fired.get("merger:ownbeam", 0) + 1
            unrealsdk.logging.info(f"{TAG} Merger: drawing the {socket} beam ourselves ({psc.Template.Name}, beam emitters {idx})")
    try:                                                     # the manager's own beam: hidden while ours shows
        sp = src_psc._path_name() if src_psc is not None else None
        if sp and not src_psc.HiddenGame:
            src_psc.SetHidden(True)
            _hidden_mgr_psc.add(sp)
    except Exception:  # noqa: BLE001
        pass
    d = _norm((end.X - start.X, end.Y - start.Y, end.Z - start.Z))
    dv = unrealsdk.make_struct("Vector", X=d[0], Y=d[1], Z=d[2])
    # The template has no beam-source module: the beam starts at the component itself, which the manager moves by hand
    # every tick and ours stayed where it spawned; its end was fixed too (user, 2026-10-01: the beam stuck where it was
    # first aimed). So the component is moved to the wrist and each beam emitter's points are set as well as the
    # parameters.
    psc.SetTranslation(NewTranslation=start)
    for k in _own_beam_emitters.get(socket, ()):
        try:
            psc.SetBeamSourcePoint(EmitterIndex=k, NewSourcePoint=start, SourceIndex=0)
            psc.SetBeamTargetPoint(EmitterIndex=k, NewTargetPoint=end, TargetIndex=0)
            psc.SetBeamEndPoint(EmitterIndex=k, NewEndPoint=end)
        except Exception:  # noqa: BLE001
            _log_once("merger:beampoints", "Merger: setting our beam's points failed: " + traceback.format_exc().splitlines()[-1])
    psc.SetVectorParameter(ParameterName="SourceLocation", Param=start)
    psc.SetVectorParameter(ParameterName="TargetLocation", Param=end)
    psc.SetVectorParameter(ParameterName="SourceTangent", Param=dv)
    psc.SetVectorParameter(ParameterName="TargetTangent", Param=dv)
    psc.SetVectorParameter(ParameterName="Direction", Param=dv)


def _release_own_beams(sockets=None) -> None:
    for socket in list(_own_beam_psc):
        if sockets is not None and socket not in sockets:
            continue
        path = _own_beam_psc.pop(socket)
        _own_beam_emitters.pop(socket, None)
        hands_path = _own_beam_attached.pop(socket, None)
        _own_beam_push.pop(socket, None)
        _own_beam_push.pop(socket + ":set", None)
        try:
            psc = unrealsdk.find_object("ParticleSystemComponent", path)
            psc.DeactivateSystem()
            if hands_path:
                hands = unrealsdk.find_object("SkeletalMeshComponent", hands_path)
                if hands is not None:
                    hands.DetachComponent(psc)
        except Exception:  # noqa: BLE001
            pass
    if sockets is None:
        for path in list(_hidden_mgr_psc):
            try:
                comp = unrealsdk.find_object("ParticleSystemComponent", path)
                if comp is not None:
                    comp.SetHidden(False)        # a pooled component goes back as it came
            except Exception:  # noqa: BLE001
                pass
        _hidden_mgr_psc.clear()


MERGER_ORIGIN = [60.0, 22.0, -12.0]      # beam start in camera space: forward, side (mirrored per wrist), up; `doppelskill beamorigin f s u`
MERGER_SHIFT = [0.0, 0.0, 0.0]           # common camera-space nudge for both beams; `doppelskill beamshift f s u`
MERGER_BEAM_SCALE = 1.0                  # beam particle component scale (TPS runs it at 1.0; beamdump 2026-09-15); `doppelskill beamscale x`
# Measured in TPS (beamdump, standing still): the beam starts where the first-person wrist socket
# is, with its camera-space side/up components multiplied by ~3.45; i.e. ~113 units out, wide
# and LOW (L (113,-28,-24), R (115,+47,-23)), behind and below the drawn gauntlets, whose
# foreground FOV hides the first stretch; the beam then appears to leave the gauntlet tip. Both
# beams end on the crosshair ray (5000 units out when nothing is hit), so they converge.
MERGER_FP_SCALE = 0.0                    # 0 = from the FOVs (merger_fp_scale); `doppelskill beamk x` fixes it
# beamtrack, 2026-10-01 (TPS and BL2 at the same 110 FOV): TPS starts each beam where the gauntlet shows on screen, the
# first-person socket's side/up offsets times tan(world hFOV/2) / tan(ForegroundFOV/2): 2.47 there (arms at 60), 3.45
# here (the pistol type's arms at 45), so the old constant was right only for 110 FOV, 16:9 and a 45-degree weapon type.
# And TPS's start does not ride the arms' sway: looking up or down the socket swings ~9 units and the start barely
# moves, where ours moved with it x3.45 (-24 -> -51 looking up, -> +12 looking down). The socket's camera-space offset
# is smoothed over MERGER_SWAY_TAU before it is scaled: the sway is left out, the arm animation still followed.
_merger_prev_cam = None                  # (eye location, pitch, yaw, time) at the last Merger tick: the arms' camera
MERGER_SWAY_TAU = 0.0                    # s; `doppelskill beamsway x` (0 = follow the socket every frame, as before)
_merger_sway: dict = {}                  # socket -> (smoothed f, r, u, last time)


def merger_fp_scale(pc: UObject) -> float:
    """tan(horizontal world FOV / 2) / tan(foreground FOV / 2): FOVAngle is BL2's vertical FOV, so the aspect ratio
    turns it horizontal (77.55 vertical at 16:9 is the 110 setting)."""
    if MERGER_FP_SCALE > 0.0:
        return MERGER_FP_SCALE
    try:
        v, fg = float(pc.FOVAngle), float(pc.ForegroundFOV)
        aspect = 16.0 / 9.0
        try:
            hud = pc.myHUD
            if hud is not None and float(hud.SizeY) > 1.0:
                aspect = float(hud.SizeX) / float(hud.SizeY)
        except Exception:  # noqa: BLE001
            pass
        if v > 1.0 and fg > 1.0:
            return math.tan(math.radians(v) / 2) * aspect / math.tan(math.radians(fg) / 2)
    except Exception:  # noqa: BLE001
        pass
    return 3.45
MERGER_CONVERGE_DEFAULT = 5000.0         # TPS's BeamMaxLockOnDistance: aim point when the beam hits nothing
_merger_hit_dist: dict = {}              # beam index -> distance from the camera to its end point; "shared" -> the eased aim distance both beams use
MERGER_CONVERGE_RATE = 12.0              # 1/s: easing rate of the shared aim distance
MERGER_LEAD = 1.0                        # frames of pawn velocity the origin is led by (`doppelskill beamlead x`). Measured 2026-09-15: PlayerTick runs before the pawn's move, the beam manager samples the socket before it too, the camera after; one frame of motion
_merger_motion_lines = 0                 # per activation: lag measurement lines while moving
_merger_draw_lines = 0                   # per activation: rendered-camera mismatch lines
_merger_draw_sample: dict[str, tuple] = {}
_merger_prev_origin: dict[str, tuple] = {}
_last_dt = 1.0 / 60.0
_last_view_yaw: int | None = None
_last_view_pitch: int | None = None
MERGER_ORIGIN_MODE = "tps"               # "tps": first-person socket, lateral x MERGER_FP_SCALE (measured in TPS); "camera": fixed camera-frame offsets; "socket": the projected first-person socket


def _beam_origin(pc: UObject, pawn: UObject, socket: str):
    """Where a wrist beam should start. The first-person arms are not drawn where their world
    sockets are (foreground FOV, camera-relative placement), so a fixed camera-frame offset
    tuned against the drawn gauntlet tips is the reliable version; the socket projection is
    kept for comparison."""
    if MERGER_ORIGIN_MODE == "socket":
        fp = _fp_socket(pawn, socket)
        return _apparent_fp_point(pc, pawn, fp) if fp is not None else None
    cam0 = pawn.GetPawnViewLocation()
    cam = cam0
    rot = pc.Rotation
    lead = MERGER_LEAD * _last_dt
    if lead:
        try:                                     # lead the pawn's motion by the sampling lag (jumps/strafes dragged the beams behind the view)
            vel = pawn.Velocity
            cam = unrealsdk.make_struct("Vector", X=cam.X + vel.X * lead, Y=cam.Y + vel.Y * lead, Z=cam.Z + vel.Z * lead)
        except Exception:
            pass
    pitch, yaw = rot.Pitch * math.pi / 32768.0, rot.Yaw * math.pi / 32768.0
    F = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    R = (-math.sin(yaw), math.cos(yaw), 0.0)
    U = (-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch))
    if MERGER_ORIGIN_MODE == "tps":
        fp = _fp_socket(pawn, socket)
        if fp is None:
            return None
        # The first-person arms are still where last frame's camera put them when this runs (the engine places them
        # after the controller's tick), so the socket is measured against that camera: against this frame's, a fast
        # turn read as the arms swinging by a frame's worth of rotation (~9 units at 300 deg/s), x3.45 at the start,
        # and the beams left from all sorts of places (user, 2026-10-01; beamtrack showed the one-frame swing).
        now = time.monotonic()
        pc_prev = _merger_prev_cam
        if pc_prev is not None and now - pc_prev[3] < 0.1:
            c, pp, py = pc_prev[0], pc_prev[1] * math.pi / 32768.0, pc_prev[2] * math.pi / 32768.0
            Fp = (math.cos(pp) * math.cos(py), math.cos(pp) * math.sin(py), math.sin(pp))
            Rp = (-math.sin(py), math.cos(py), 0.0)
            Up = (-math.sin(pp) * math.cos(py), -math.sin(pp) * math.sin(py), math.cos(pp))
            d = (fp.X - c[0], fp.Y - c[1], fp.Z - c[2])
            raw = (_dot(d, Fp), _dot(d, Rp), _dot(d, Up))
        else:
            d = (fp.X - cam0.X, fp.Y - cam0.Y, fp.Z - cam0.Z)     # the socket against the un-led camera: its own frame
            raw = (_dot(d, F), _dot(d, R), _dot(d, U))
        prev = _merger_sway.get(socket)
        if MERGER_SWAY_TAU <= 0.0 or prev is None or now - prev[3] > 0.5:
            sm = raw                                          # first frame of these beams (or after a gap): start where it is
        else:
            a = min(1.0, (now - prev[3]) / MERGER_SWAY_TAU)
            sm = tuple(p + (r - p) * a for p, r in zip(prev[:3], raw))
        _merger_sway[socket] = (*sm, now)
        k = merger_fp_scale(pc)
        x = sm[0] + MERGER_SHIFT[0]
        y = sm[1] * k + MERGER_SHIFT[1]
        z = sm[2] * k + MERGER_SHIFT[2]
        return unrealsdk.make_struct("Vector", X=cam.X + x * F[0] + y * R[0] + z * U[0], Y=cam.Y + x * F[1] + y * R[1] + z * U[1], Z=cam.Z + x * F[2] + y * R[2] + z * U[2])
    x, y, z = MERGER_ORIGIN
    y = -y if socket.startswith("L_") else y
    x, y, z = x + MERGER_SHIFT[0], y + MERGER_SHIFT[1], z + MERGER_SHIFT[2]
    return unrealsdk.make_struct("Vector", X=cam.X + x * F[0] + y * R[0] + z * U[0], Y=cam.Y + x * F[1] + y * R[1] + z * U[1], Z=cam.Z + x * F[2] + y * R[2] + z * U[2])


def _apparent_fp_point(pc: UObject, pawn: UObject, fp):
    """The first-person arms are drawn with the controller's ForegroundFOV, the world with
    FOVAngle: a world-space point at a first-person socket shows up nearer the screen centre
    than the arms do. Push the point's camera-lateral components out by
    tan(FOVAngle/2) / tan(ForegroundFOV/2) so a world effect lands on the drawn wrist."""
    try:
        fov_world, fov_fore = float(pc.FOVAngle), float(pc.ForegroundFOV)
        if fov_fore <= 1.0 or fov_world <= 1.0:
            return fp
        k = math.tan(math.radians(fov_world) / 2) / math.tan(math.radians(fov_fore) / 2)
        cam = pawn.GetPawnViewLocation()
        rot = pc.Rotation
        pitch, yaw = rot.Pitch * math.pi / 32768.0, rot.Yaw * math.pi / 32768.0
        F = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        R = (-math.sin(yaw), math.cos(yaw), 0.0)
        U = (-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch))
        d = (fp.X - cam.X, fp.Y - cam.Y, fp.Z - cam.Z)
        x = d[0] * F[0] + d[1] * F[1] + d[2] * F[2]
        y = d[0] * R[0] + d[1] * R[1] + d[2] * R[2]
        z = d[0] * U[0] + d[1] * U[1] + d[2] * U[2]
        y, z = y * k, z * k
        if _fired.get("merger:fov", 0) < 1:
            _fired["merger:fov"] = 1
            diag.info(f"{TAG} Merger: world FOV {fov_world:.1f}, foreground FOV {fov_fore:.1f}, lateral scale {k:.3f}")
        return unrealsdk.make_struct("Vector", X=cam.X + x * F[0] + y * R[0] + z * U[0], Y=cam.Y + x * F[1] + y * R[1] + z * U[1], Z=cam.Z + x * F[2] + y * R[2] + z * U[2])
    except Exception:
        _log_once("merger:fov", "FOV correction failed: " + traceback.format_exc().splitlines()[-1])
        return fp


def _merger_tick(now: float) -> None:
    """Steer the wrist beams the way TPS's bFirstPersonSource did. Measured 2026-09-14: a beam's
    direction is its source SOCKET's rotation (with no socket the manager falls back to the
    pawn's yaw-only rotation, whatever actor is named; a locked target aims at the target
    actor itself; no proxy actor spawns from here). The wrist sockets ride the arms, which the
    dual-laser animation splays (~30 degrees apart). So two sockets on the body's Root bone are
    borrowed for the duration: every tick their relative location is set to the first-person
    wrist position and their relative rotation to the view pitch."""
    global _merger_probe_lines, _merger_bone_failed, _merger_tick_cost
    if now > _merger_beams_until:
        if _own_beam_psc or _hidden_mgr_psc:
            _release_own_beams()
        if _merger_socket_backup:
            _merger_restore_sockets()
            if _merger_tick_cost:
                diag.info(f"{TAG} Merger tick cost: max {max(_merger_tick_cost) * 1000:.1f} ms, mean {sum(_merger_tick_cost) / len(_merger_tick_cost) * 1000:.1f} ms over {len(_merger_tick_cost)} ticks")
                _merger_tick_cost = []
        return
    if not diag.enabled:
        _merger_tick_body(now)
        return
    t0 = time.perf_counter()
    try:
        _merger_tick_body(now)
    finally:
        if len(_merger_tick_cost) < 3600:      # endless Merger never reaches the summary above
            _merger_tick_cost.append(time.perf_counter() - t0)


_merger_tick_cost: list[float] = []


class _ProbeOff(Exception):
    """Skips the Merger motion probe when diagnostic logging is off."""


def _merger_tick_body(now: float) -> None:
    global _merger_probe_lines, _merger_bone_failed, _merger_live_until
    pc = _pc()
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return
    try:
        mgr = _merger_manager(pawn)
        if mgr is None:
            _log_once("merger:nomgr", "Merger: no WillowLightProjectileManager owned by the player pawn")
            return
        base = pawn.Location
        try:                                     # the body mesh hangs below the pawn's centre: Root sits at its feet
            mt = pawn.Mesh.Translation
            root = (base.X + mt.X, base.Y + mt.Y, base.Z + mt.Z)
        except Exception:
            root = (base.X, base.Y, base.Z)
        yaw = pawn.Rotation.Yaw * math.pi / 32768.0
        cy, sy = math.cos(yaw), math.sin(yaw)
        pitch = int(pc.Rotation.Pitch)
        view_yaw = int(pc.Rotation.Yaw)
        own = MERGER_OWN_BEAMS and MERGER_ORIGIN_MODE == "tps"
        end_pt = None
        dmg_from = None
        if own:
            cam0 = pawn.GetPawnViewLocation()
            vp, vy = pc.Rotation.Pitch * math.pi / 32768.0, pc.Rotation.Yaw * math.pi / 32768.0
            Fv = (math.cos(vp) * math.cos(vy), math.cos(vp) * math.sin(vy), math.sin(vp))
            lead = MERGER_LEAD * _last_dt
            try:
                vel = pawn.Velocity
                cl = (cam0.X + vel.X * lead, cam0.Y + vel.Y * lead, cam0.Z + vel.Z * lead)
            except Exception:  # noqa: BLE001
                cl = (cam0.X, cam0.Y, cam0.Z)
            # the damage beams run down the crosshair ray (placed just ahead of the camera, below), so where they end
            # is where the crosshair lands: the engine's own trace, enemies and curved surfaces included. The nearest
            # of them, put on this tick's crosshair ray, is where both drawn beams meet.
            dmg_from = unrealsdk.make_struct("Vector", X=cl[0] + Fv[0] * MERGER_RAY_FORWARD, Y=cl[1] + Fv[1] * MERGER_RAY_FORWARD,
                                             Z=cl[2] + Fv[2] * MERGER_RAY_FORWARD)
            reach = None
            ours_sockets = set(MERGER_AIM_SOCKETS.values())
            for b0 in mgr.MyBeams:
                try:
                    c0 = b0.CoreBeamState
                    if not c0.bEnabled or str(c0.SourceData.Socket) not in ours_sockets:
                        continue
                    ep = b0.CurrentEndPoint
                    t = (ep.X - cl[0]) * Fv[0] + (ep.Y - cl[1]) * Fv[1] + (ep.Z - cl[2]) * Fv[2]
                    if MERGER_RAY_FORWARD < t <= MERGER_CONVERGE_DEFAULT + 100.0 and (reach is None or t < reach):
                        reach = t
                except Exception:  # noqa: BLE001
                    continue
            if reach is None:
                reach = MERGER_CONVERGE_DEFAULT
            end_pt = unrealsdk.make_struct("Vector", X=cl[0] + Fv[0] * reach, Y=cl[1] + Fv[1] * reach, Z=cl[2] + Fv[2] * reach)
            global _beam_probe_frames, _beam_probe_dumped
            if _beam_probe_frames > 0:
                _beam_probe_frames -= 1
                if not _beam_probe_dumped:
                    _beam_probe_dumped = True
                    for b0 in mgr.MyBeams:
                        if b0.CoreBeamState.bEnabled and str(b0.CoreBeamState.SourceData.Socket) in ours_sockets:
                            _beam_probe_dump(mgr, b0)
                            break
                Rv = (-math.sin(vy), math.cos(vy), 0.0)
                Uv = (-math.sin(vp) * math.cos(vy), -math.sin(vp) * math.sin(vy), math.cos(vp))
                parts_ = [f"view pitch {vp * 180 / math.pi:.1f}", f"reach {reach:.0f}"]
                for b0 in mgr.MyBeams:
                    try:
                        c0 = b0.CoreBeamState
                        if not c0.bEnabled:
                            continue
                        st0, ep0, dr0 = b0.CurrentStartPoint, b0.CurrentEndPoint, b0.CurrentDirection
                        dst = (st0.X - cl[0], st0.Y - cl[1], st0.Z - cl[2])
                        dep = (ep0.X - cl[0], ep0.Y - cl[1], ep0.Z - cl[2])
                        dp_ = ((int(dr0.Pitch) - int(pc.Rotation.Pitch) + 32768) % 65536 - 32768) * 360 / 65536
                        dy_ = ((int(dr0.Yaw) - int(pc.Rotation.Yaw) + 32768) % 65536 - 32768) * 360 / 65536
                        parts_.append(f"{c0.SourceData.Socket}: start ({_dot(dst, Fv):.0f},{_dot(dst, Rv):.0f},{_dot(dst, Uv):.0f}) "
                                      f"dir off view ({dp_:.1f},{dy_:.1f}) deg, end ({_dot(dep, Fv):.0f},{_dot(dep, Rv):.0f},{_dot(dep, Uv):.0f})")
                    except Exception as e_:  # noqa: BLE001
                        parts_.append(f"beam unreadable: {e_!r}")
                unrealsdk.logging.info(f"{TAG} beamprobe: " + " | ".join(parts_))
        drawn = set()
        for i, b in enumerate(mgr.MyBeams):
            core = b.CoreBeamState
            if not core.bEnabled:
                continue
            socket = str(core.SourceData.Socket)
            if socket in MERGER_SOCKETS:
                _merger_beam_sockets[i] = socket
            socket = _merger_beam_sockets.get(i)
            if socket is None:
                continue
            if not _merger_infinite:                 # the endless lasers' hits count as gun hits, as in TPS
                _merger_live_until = time.monotonic() + MERGER_LIVE_GRACE
            aim_name = MERGER_AIM_SOCKETS[socket]
            so = _body_socket(aim_name)
            if so is None:
                _log_once("merger:socket:" + aim_name, f"Merger: body socket {aim_name} not found")
                continue
            if aim_name not in _merger_socket_backup:
                l, r = so.RelativeLocation, so.RelativeRotation
                _merger_socket_backup[aim_name] = ((l.X, l.Y, l.Z), (r.Pitch, r.Yaw, r.Roll))
            fp = _beam_origin(pc, pawn, socket)
            placed = False
            aim_pitch, aim_yaw = pitch, view_yaw
            place_at = fp
            if own and dmg_from is not None:
                place_at = dmg_from              # the manager's beam (the damage, hidden) runs down the crosshair ray
            elif fp is not None and MERGER_ORIGIN_MODE == "tps":
                # a BL2 free beam runs straight along its socket's rotation, so point each beam at
                # a spot on the crosshair ray: as far as this beam's last impact (5000 out when it
                # hit nothing); the two beams then meet where the crosshair lands, as in TPS
                try:
                    cam = pawn.GetPawnViewLocation()
                    # how far the beams reach: the engine's per-frame end point (LastImpactLocation
                    # only refreshes every TraceInterval = 0.2 s, which re-aimed the beams in visible
                    # steps while strafing); shared by both beams and eased so a change in reach
                    # (wall -> sky) is a glide, not a jump
                    ep = b.CurrentEndPoint
                    measured = math.sqrt((ep.X - cam.X) ** 2 + (ep.Y - cam.Y) ** 2 + (ep.Z - cam.Z) ** 2) if abs(ep.X) + abs(ep.Y) + abs(ep.Z) > 1.0 else MERGER_CONVERGE_DEFAULT
                    measured = max(150.0, min(measured, MERGER_CONVERGE_DEFAULT))
                    _merger_hit_dist[i] = measured
                    target_d = min(v for k, v in _merger_hit_dist.items() if k != "shared")   # nearest beam end (never the eased value itself: that only ratcheted down)
                    dist = _merger_hit_dist.get("shared")
                    if dist is None:
                        dist = target_d
                    else:
                        dist += (target_d - dist) * min(1.0, _last_dt * MERGER_CONVERGE_RATE)
                    _merger_hit_dist["shared"] = dist
                    vp, vy = pc.Rotation.Pitch * math.pi / 32768.0, pc.Rotation.Yaw * math.pi / 32768.0
                    tx = cam.X + math.cos(vp) * math.cos(vy) * dist - fp.X
                    ty = cam.Y + math.cos(vp) * math.sin(vy) * dist - fp.Y
                    tz = cam.Z + math.sin(vp) * dist - fp.Z
                    aim_yaw = int(round(math.atan2(ty, tx) * 32768.0 / math.pi))
                    aim_pitch = int(round(math.atan2(tz, math.sqrt(tx * tx + ty * ty)) * 32768.0 / math.pi))
                except Exception:
                    _log_once("merger:aim", "Merger aim failed: " + traceback.format_exc().splitlines()[-1])
            if place_at is not None and not _merger_bone_failed:
                try:
                    placed = _place_socket_world(pawn.Mesh, so, place_at, aim_pitch, aim_yaw)
                except Exception:
                    _merger_bone_failed = True
                    _log_once("merger:bone", "Merger: bone-frame socket placement unavailable, pawn-frame fallback: " + traceback.format_exc().splitlines()[-1])
            if _beam_probe_frames > 0 and not placed:
                unrealsdk.logging.info(f"{TAG} beamprobe: {aim_name} placed in the pawn frame (bone-frame placement failed)")
            if not placed:
                pitch = aim_pitch
                if place_at is not None:
                    dx, dy, dz = place_at.X - root[0], place_at.Y - root[1], place_at.Z - root[2]
                    so.RelativeLocation = unrealsdk.make_struct("Vector", X=dx * cy + dy * sy, Y=-dx * sy + dy * cy, Z=dz)   # Root frame
                # the beam's last direction tells how far the Root frame's yaw is from the view's
                trim = _merger_yaw_trim.get(aim_name, 0)
                try:
                    if str(core.SourceData.Socket) == aim_name:
                        err = ((view_yaw - int(b.CurrentDirection.Yaw) + 32768) % 65536) - 32768
                        if abs(err) < 12000:
                            trim = int(trim + 0.5 * err)
                            _merger_yaw_trim[aim_name] = trim
                except Exception:
                    pass
                so.RelativeRotation = unrealsdk.make_struct("Rotator", Pitch=pitch, Yaw=trim, Roll=0)
            if own and fp is not None and end_pt is not None:
                try:
                    _drive_own_beam(pc, socket, b, fp, end_pt)
                    drawn.add(socket)
                except Exception:
                    _log_once("merger:own", "Merger: drawing our beam failed: " + traceback.format_exc().splitlines()[-1])
            try:
                part = b.PartRef                                   # the beam's ParticleSystemComponent
                if part is not None and abs(float(part.Scale) - MERGER_BEAM_SCALE) > 0.01:
                    part.SetScale(NewScale=MERGER_BEAM_SCALE)
                    _log_once("merger:scale:" + str(MERGER_BEAM_SCALE), f"Merger beam particle {part.Class.Name} {part.Name} scale -> {MERGER_BEAM_SCALE} (template {part.Template.Name if part.Template is not None else None})")
            except Exception:
                _log_once("merger:scale", "beam particle scale failed: " + traceback.format_exc().splitlines()[-1])
            src, tgt = core.SourceData, core.TargetData
            if str(src.Socket) != aim_name:
                src.Actor = pawn
                src.Socket = aim_name
                src.bUseWeapon = False
                src.Offset = unrealsdk.make_struct("Vector", X=0.0, Y=0.0, Z=0.0)
                tgt.Actor = None
                tgt.Socket = "None"
                tgt.bUseWeapon = False
                tgt.Offset = unrealsdk.make_struct("Vector", X=0.0, Y=0.0, Z=0.0)
                core.bLocked = False
                _merger_hit_dist.clear()
                try:                                     # TPS: Bullet_Laser_Shock impacts, no impact audio (beamdump 2026-09-15)
                    core.bPlayImpactAudio = False
                    imp = _merger_impact_def()
                    if imp is not None and core.ImpactDefinition is None:
                        core.ImpactDefinition = imp
                except Exception:
                    _log_once("merger:impact", "Merger impact setup failed: " + traceback.format_exc().splitlines()[-1])
            if _merger_probe_lines < 12 and diag.enabled:
                diag.info(f"{TAG} Merger beam[{i}] {socket}->{aim_name} ({'bone' if placed else 'pawn'} frame): origin {_v(fp)} view {_v(pc.Rotation)} | engine start {_v(b.CurrentStartPoint)} dir {_v(b.CurrentDirection)}")
                _merger_probe_lines += 1
                try:                                     # how the manager positions the beam particle: attached to a socket, or moved by hand?
                    part = b.PartRef
                    if part is not None:
                        att = []
                        from .characterslot import _find_hands_component
                        for host_name, host in (("Mesh", pawn.Mesh), ("hands", _find_hands_component(pawn))):
                            if host is None:
                                continue
                            for a in host.Attachments:
                                try:
                                    if a.Component is not None and a.Component.Name == part.Name:
                                        att.append(f"{host_name}@{a.BoneName} rel {_v(a.RelativeLocation)}")
                                except Exception:
                                    continue
                        diag.info(f"{TAG} Merger beam[{i}] particle {part.Name}: owner {part.Owner.Name if part.Owner is not None else None}, translation {_v(part.Translation)} abs {part.AbsoluteTranslation}, attachments {att}")
                except Exception:
                    _log_once("merger:attprobe", "Merger attachment probe failed: " + traceback.format_exc().splitlines()[-1])
            # lag measurement while moving: where the engine's start point sits against the origin
            # we asked for last tick and this tick, along the velocity (in frames) and across it
            global _merger_motion_lines, _merger_draw_lines
            try:
                if not diag.enabled:
                    raise _ProbeOff
                vel = pawn.Velocity
                speed = math.sqrt(vel.X * vel.X + vel.Y * vel.Y + vel.Z * vel.Z)
                if fp is not None and speed > 150.0 and _merger_motion_lines < 60:
                    sp = b.CurrentStartPoint
                    prev = _merger_prev_origin.get(socket)
                    vh = (vel.X / speed, vel.Y / speed, vel.Z / speed)
                    def _decomp(o):
                        d = (sp.X - o[0], sp.Y - o[1], sp.Z - o[2])
                        along = _dot(d, vh)
                        perp = math.sqrt(max(0.0, _dot(d, d) - along * along))
                        return along / (speed * _last_dt), perp
                    now_f, now_p = _decomp((fp.X, fp.Y, fp.Z))
                    prev_txt = ""
                    if prev is not None:
                        pf, pp = _decomp(prev)
                        prev_txt = f" vs prev origin {pf:+.2f} frames along, {pp:.1f} across"
                    try:
                        pov = pc.PlayerCamera.CameraCache.POV
                        cam = pawn.GetPawnViewLocation()
                        cam_txt = f" | camera-eye delta ({pov.Location.X - cam.X:+.1f},{pov.Location.Y - cam.Y:+.1f},{pov.Location.Z - cam.Z:+.1f}) roll {pov.Rotation.Roll}"
                    except Exception:
                        cam_txt = ""
                    diag.info(f"{TAG} Merger lag {socket}: speed {speed:.0f} dt {_last_dt * 1000:.1f}ms phys {pawn.Physics}: engine start vs this origin {now_f:+.2f} frames along, {now_p:.1f} across{prev_txt}{cam_txt} | aim dist {_merger_hit_dist.get('shared', 0):.0f} (this beam {_merger_hit_dist.get(i, 0):.0f})")
                    _merger_motion_lines += 1
                if fp is not None:
                    _merger_prev_origin[socket] = (fp.X, fp.Y, fp.Z)
                # the drawn mismatch: CameraCache.POV is the camera the LAST frame was rendered with,
                # so last frame's engine start point is compared with where that camera put the wrist
                # (the socket's camera-frame offset carried over), along the velocity in frames
                if fp is not None and speed > 150.0:
                    last = _merger_draw_sample.get(socket)
                    pov = pc.PlayerCamera.CameraCache.POV
                    if last is not None and _merger_draw_lines < 40:
                        (ox, oy, oz), (sx, sy_, sz), lvh, lspeed, ldt = last
                        pr, py = pov.Rotation.Pitch * math.pi / 32768.0, pov.Rotation.Yaw * math.pi / 32768.0
                        Fc = (math.cos(pr) * math.cos(py), math.cos(pr) * math.sin(py), math.sin(pr))
                        Rc = (-math.sin(py), math.cos(py), 0.0)
                        Uc = (-math.sin(pr) * math.cos(py), -math.sin(pr) * math.sin(py), math.cos(pr))
                        drawn = (pov.Location.X + ox * Fc[0] + oy * Rc[0] + oz * Uc[0], pov.Location.Y + ox * Fc[1] + oy * Rc[1] + oz * Uc[1], pov.Location.Z + ox * Fc[2] + oy * Rc[2] + oz * Uc[2])
                        dd = (sx - drawn[0], sy_ - drawn[1], sz - drawn[2])
                        along = _dot(dd, lvh)
                        perp = math.sqrt(max(0.0, _dot(dd, dd) - along * along))
                        diag.info(f"{TAG} Merger draw {socket}: last frame's beam start vs the wrist as rendered: {along / (lspeed * ldt):+.2f} frames along the motion ({along:+.1f} u), {perp:.1f} u across; lead {MERGER_LEAD} speed {lspeed:.0f} dt {ldt * 1000:.1f}ms")
                        _merger_draw_lines += 1
                    # this frame's sample: the socket offset in the view frame the origin was built in
                    cam_now = pawn.GetPawnViewLocation()
                    vr = pc.Rotation
                    pr, py = vr.Pitch * math.pi / 32768.0, vr.Yaw * math.pi / 32768.0
                    Fv = (math.cos(pr) * math.cos(py), math.cos(pr) * math.sin(py), math.sin(pr))
                    Rv = (-math.sin(py), math.cos(py), 0.0)
                    Uv = (-math.sin(pr) * math.cos(py), -math.sin(pr) * math.sin(py), math.cos(pr))
                    lead_v = MERGER_LEAD * _last_dt
                    dv = (fp.X - cam_now.X - vel.X * lead_v, fp.Y - cam_now.Y - vel.Y * lead_v, fp.Z - cam_now.Z - vel.Z * lead_v)   # the un-led offset
                    sp = b.CurrentStartPoint
                    _merger_draw_sample[socket] = ((_dot(dv, Fv), _dot(dv, Rv), _dot(dv, Uv)), (sp.X, sp.Y, sp.Z), (vel.X / speed, vel.Y / speed, vel.Z / speed), speed, _last_dt)
                elif speed <= 150.0:
                    _merger_draw_sample.pop(socket, None)
            except _ProbeOff:
                pass
            except Exception:
                _log_once("merger:lagprobe", "Merger lag probe failed: " + traceback.format_exc().splitlines()[-1])
        if own and _own_beam_psc:
            gone = [k for k in _own_beam_psc if k not in drawn]
            if gone:
                _release_own_beams(gone)         # the manager's beam ended (its destroy): ours goes with it
        global _merger_prev_cam
        try:
            e = pawn.GetPawnViewLocation()
            _merger_prev_cam = ((e.X, e.Y, e.Z), int(pc.Rotation.Pitch), int(pc.Rotation.Yaw), time.monotonic())
        except Exception:  # noqa: BLE001
            _merger_prev_cam = None
    except Exception:
        _log_once("merger:tick", "Merger steer failed: " + traceback.format_exc().splitlines()[-1])


# The HUD movie's layout container is the unnamed-looking sprite at root depth 1; the
# executable addresses it as "p1" (p1.secondwind.gotoAndPlay, p1.dying1.killRevive.inner.
# killReviveText.text, ...; Borderlands2.exe strings, 2026-09-15). A generic revive message
# ("Corporate Benefits. Leadership!") is written into the FFYL module's killRevive banner,
# p1.dying1.killRevive, and in the 16:9 layout sprite dying1 is placed at depth 98; under
# activeAbility (depth 348), the module the action-skill bar is loaded into. So the message
# is drawn behind the bar. Measured: swapDepths through GFxObject.Invoke works (p1.secondwind
# went -16360 -> 0), so dying1 is lifted to the container's top while a message plays and put
# back afterwards (its full-width bkgd would otherwise sit over the rest of the HUD in FFYL).
REVIVE_MODULE = "_root.p1.dying1"
SECONDWIND_CLIP = "_root.p1.secondwind"
BAR_CLIP = "_root.p1.activeAbility"
REVIVE_RAISE_SECONDS = 4.0
_revive_module_restore: tuple[float, int] | None = None   # (monotonic deadline, original depth)
_revive_logged: set[str] = set()


def _hud_movie(pc: UObject):
    try:
        return pc.myHUD.GetHUDMovie()
    except Exception:
        return None


def _as_depth(clip: UObject | None) -> int | None:
    if clip is None:
        return None
    try:
        return int(clip.Invoke(Member="getDepth", args=[]).N)
    except Exception:
        return None


def _swap_depth(clip: UObject, depth: int) -> None:
    clip.Invoke(Member="swapDepths", args=[unrealsdk.make_struct("ASValue", Type=2, N=float(depth))])   # AS_Number


def _raise_revive_module(movie: UObject) -> str:
    """Lift p1.dying1 (and the second-wind clip) above everything else in the layout
    container for REVIVE_RAISE_SECONDS."""
    global _revive_module_restore
    try:
        p1 = movie.GetVariableObject("_root.p1")
        mod = movie.GetVariableObject(REVIVE_MODULE)
        if p1 is None or mod is None:
            return f"{REVIVE_MODULE} not found in the HUD movie"
        bar_depth = _as_depth(movie.GetVariableObject(BAR_CLIP))
        before = _as_depth(mod)
        nxt = int(p1.GetNextHighestDepth())
        if before is not None and before == nxt - 1:
            if _revive_module_restore is not None:
                _revive_module_restore = (time.monotonic() + REVIVE_RAISE_SECONDS, _revive_module_restore[1])
            return f"{REVIVE_MODULE} already on top (depth {before}; bar {bar_depth})"
        _swap_depth(mod, nxt)
        after = _as_depth(mod)
        if before is not None:
            _revive_module_restore = (time.monotonic() + REVIVE_RAISE_SECONDS, before)
        sw = movie.GetVariableObject(SECONDWIND_CLIP)
        sw_depth = None
        if sw is not None:
            _swap_depth(sw, int(p1.GetNextHighestDepth()))
            sw_depth = _as_depth(sw)
        return f"{REVIVE_MODULE} depth {before} -> {after} (bar {bar_depth}; second-wind clip {sw_depth})"
    except Exception:
        return f"{REVIVE_MODULE} raise failed: " + traceback.format_exc().splitlines()[-1]


def _revive_module_restore_tick(pc: UObject) -> None:
    """Put dying1 back at its timeline depth once the message has played."""
    global _revive_module_restore
    if _revive_module_restore is None or time.monotonic() < _revive_module_restore[0]:
        return
    _, depth = _revive_module_restore
    _revive_module_restore = None
    movie = _hud_movie(pc)
    if movie is None:
        return
    try:
        mod = movie.GetVariableObject(REVIVE_MODULE)
        if mod is None:
            return
        _swap_depth(mod, depth)
        how = f"{REVIVE_MODULE} restored to depth {_as_depth(mod)} (asked {depth})"
    except Exception:
        how = f"{REVIVE_MODULE} restore failed: " + traceback.format_exc().splitlines()[-1]
    if how not in _revive_logged:
        _revive_logged.add(how)
        diag.info(f"{TAG} HUD: {how}")


_leadership_revive_at = 0.0
_leadership_revive_def: UObject | None = None


@hook("WillowGame.WillowHUDGFxMovie:DisplayGenericRevive", Type.PRE, hook_identifier="doppel_leadership_revive")
def _on_generic_revive(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_generic_revive")
    """Leadership's "Corporate Benefits. Leadership!" goes into the HUD's second-wind clip;
    the same clip the engine's own DisplaySecondWind() writes "SECOND WIND" into a moment
    later, so the message was never seen (2026-09-14). Remember it; the banner is repeated
    with our text afterwards."""
    global _leadership_revive_at, _leadership_revive_def
    try:
        d = args.ReviveDef
        how = _raise_revive_module(obj)          # any revive message: its banner is under the action-skill bar
        if d is not None and d.Name == "ReviveMessage_Leadership":
            _leadership_revive_at = time.monotonic()
            _leadership_revive_def = d
            diag.info(f"{TAG} Leadership revive message displayed; {how}")
    except Exception:
        _log_once("revive:hook", "revive message hook failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.WillowHUDGFxMovie:DisplaySecondWind", Type.POST, hook_identifier="doppel_leadership_secondwind")
def _on_second_wind(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_second_wind")
    global _in_revive_replay
    try:
        if _in_revive_replay or _leadership_revive_def is None or time.monotonic() - _leadership_revive_at > 2.0:
            diag.info(f"{TAG} SECOND WIND banner (no Leadership message pending)")
            return
        _in_revive_replay = True
        try:
            obj.DisplayGenericRevive(ReviveDef=_leadership_revive_def, OtherPRI=None, bIsOtherEventParticipant=False)
        finally:
            _in_revive_replay = False
        diag.info(f"{TAG} SECOND WIND banner replaced by the Leadership message")
    except Exception:
        _log_once("revive:secondwind", "second wind hook failed: " + traceback.format_exc().splitlines()[-1])


_in_revive_replay = False


def on_clone_damaged(pawn: UObject, damage: float = 0.0, instigator: UObject | None = None) -> None:
    """Accountability: a Digi-Jack taking damage is a friendly taking damage. TPS's native raiser
    was SkillDefinition.OzOnFriendlyDamaged(ConsumerHandle, SkillInstigator, DamageSource,
    DamagedFriendly, Enemy, Damage, Health, MaxHealth); the graph links SkillInstigator (its
    'Inst' variable) and Enemy, so the whole list is passed. Zero-damage hits and hits from the
    player or the other Digi-Jack (Potential's nova) are not enemy damage."""
    global _last_damage_event
    if damage <= 0.0:
        return
    pc = _cast_pc()
    if pc is None:
        return
    enemy = None
    if instigator is not None:
        try:
            enemy = instigator.Pawn if hasattr(instigator, "Pawn") else instigator
        except Exception:
            enemy = None
    try:
        if enemy is not None and (enemy == pc.Pawn or (enemy.AIClass is not None and "DigiJack" in str(enemy.AIClass.Name))):
            return
    except Exception:
        pass
    now = time.monotonic()
    if now - _last_damage_event < DAMAGE_EVENT_MIN_GAP:
        return
    _last_damage_event = now
    try:
        health, max_health = float(pawn.GetHealth()), float(pawn.GetMaxHealth())
    except Exception:
        health, max_health = 0.0, 0.0
    fire("Accountability", "OzOnFriendlyDamaged", pc=pc, other=pawn,
         params=event_params(pc, None, pawn, enemy, float(damage), health, max_health))


# --- designer attributes ------------------------------------------------------------------------------------------------
def _equipped_weapons(pc: UObject) -> list:
    weapons = []
    try:
        inv = pc.GetPawnInventoryManager()
    except Exception:
        return weapons
    if inv is None:
        return weapons
    for slot in (1, 2, 3, 4):
        try:
            w = inv.GetWeaponInSlot(NewWeaponSlot=slot)
        except Exception:
            try:
                w = inv.GetWeaponInSlot(slot)
            except Exception:
                w = None
        if w is not None:
            weapons.append(w)
    return weapons


def _equipped_items(pc: UObject) -> list:
    items = []
    try:
        inv = pc.GetPawnInventoryManager()
        it = inv.ItemChain if inv is not None else None
        guard = 0
        while it is not None and guard < 16:
            items.append(it)
            it = it.Inventory
            guard += 1
    except Exception:
        pass
    return items


def _manufacturer_name(inv_obj: UObject) -> str:
    try:
        m = inv_obj.DefinitionData.ManufacturerDefinition
        return str(m.Name) if m is not None else ""
    except Exception:
        return ""


# The Hyperion allegiance relic counts as a Hyperion piece for Company Man (user, 2026-09-29): relics carry a relic
# maker, not the gun maker they are loyal to. BL2's allegiance relics are Artifact_AllegianceA..H in manufacturer
# order (A Bandit, B Dahl, C Hyperion, D Jakobs, E Maliwan, F Tediore, G Torgue, H Vladof: read from their effects).
HYPERION_ALLEGIANCE = "GD_Artifacts.A_Item.Artifact_AllegianceC"


def _is_hyperion_allegiance(inv_obj: UObject) -> bool:
    try:
        d = inv_obj.DefinitionData.ItemDefinition
        return d is not None and d._path_name() == HYPERION_ALLEGIANCE
    except Exception:
        return False


def hyperion_gear_count(pc: UObject) -> int:
    n = 0
    for obj in _equipped_weapons(pc) + _equipped_items(pc):
        if _manufacturer_name(obj) == "Hyperion" or _is_hyperion_allegiance(obj):
            n += 1
    return n


# `doppelskill relics`: a ring of relics round the player for testing Company Man's allegiance case
TEST_RELICS = ("A_AllegianceC", "A_AllegianceC", "A_AllegianceC_Rare", "A_AllegianceB", "A_AllegianceE", "A_Vitality", "A_Proficiency")


def drop_test_relics(pc: UObject) -> str:
    pawn = pc.Pawn
    try:
        level = int(pc.PlayerReplicationInfo.ExpLevel)
    except Exception:
        level = 1
    kernel = unrealsdk.find_class("ItemPool").ClassDefaultObject
    dropped = []
    for k, leaf in enumerate(TEST_RELICS):
        try:
            bal = unrealsdk.find_object("InventoryBalanceDefinition", f"GD_Artifacts.A_Item.{leaf}")
            ret = kernel.SpawnBalancedInventoryFromInventoryBalanceDefinition(InvBalanceDefinition=bal, Quantity=1, GameStage=level,
                                                                              AwesomeLevel=0, ContextSource=pawn, SpawnedInventory=[])
            spawned = ret[1] if isinstance(ret, tuple) else []
            a = 2 * math.pi * k / len(TEST_RELICS)
            for inv in spawned or []:
                loc = unrealsdk.make_struct("Vector", X=pawn.Location.X + math.cos(a) * 160, Y=pawn.Location.Y + math.sin(a) * 160,
                                            Z=pawn.Location.Z + 40)
                vel = unrealsdk.make_struct("Vector", X=math.cos(a) * 40, Y=math.sin(a) * 40, Z=150)
                inv.DropFrom(StartLocation=loc, StartVelocity=vel, bTorque=False, Torque=unrealsdk.make_struct("Vector", X=0, Y=0, Z=0),
                             bForceNoRBPhysics=False)
                dropped.append(leaf + (" (Hyperion allegiance)" if _is_hyperion_allegiance(inv) else ""))
        except Exception:
            dropped.append(f"{leaf} failed: " + traceback.format_exc().splitlines()[-1][:80])
    return f"{TAG} relics dropped at level {level}: {', '.join(dropped)}; Company Man counts {hyperion_gear_count(pc)} Hyperion piece(s) now"


def ammo_fullness(pc: UObject) -> float:
    """Average magazine fullness of the equipped weapons (TPS's EquippedWeaponsAverageAmmoFullness)."""
    total, count = 0.0, 0
    for w in _equipped_weapons(pc):
        try:
            clip = float(w.ClipSize)
            if clip > 0:
                total += max(0.0, min(1.0, float(w.ReloadCnt) / clip))
                count += 1
        except Exception:
            continue
    return total / count if count else 1.0


def _refresh_attributes(pc: UObject) -> None:
    global _gear_dirty, _gear_last, _gear_next_check, _ammo_last, _ammo_next_check
    now = time.monotonic()
    pawn = pc.Pawn
    if pawn is None:
        return
    if _gear_dirty or now >= _gear_next_check:
        _gear_dirty = False
        _gear_next_check = now + GEAR_REFRESH_SECONDS
        if grade("CompanyMan", pc) > 0:          # the grade read is the expensive part: only when a refresh is due
            # keyed by the pawn as well: a load can swap the pawn after a write, and the new one starts at 0 while
            # the count had not changed, so Company Man's icon stayed off until a Hyperion item was re-equipped
            # (players, 2026-09-29)
            key = (pawn._path_name(), hyperion_gear_count(pc))
            if key != _gear_last:
                _gear_last = key
                if set_attr(ATT_HYPERION_PIECES, pawn, key[1]):
                    diag.info(f"{TAG} Company Man: {key[1]} Hyperion piece(s) equipped")
    if now >= _ammo_next_check:
        _ammo_next_check = now + AMMO_REFRESH_SECONDS
        if grade("TaxationofTradeRoutes", pc) > 0:
            f = ammo_fullness(pc)
            path = pawn._path_name()
            if _ammo_last is None or path != _ammo_last[0] or abs(f - _ammo_last[1]) > 0.01:      # per pawn, as above
                _ammo_last = (path, f)
                set_attr(ATT_AMMO_FULLNESS, pawn, f)


# --- hooks -----------------------------------------------------------------------------------------------------------------
# --- vehicle crew animations keyed by the pawn's name ------------------------------------------------------------
# Jack's pawn archetype is named Pawn_Mechromancer (it began as Gaige's), and the DLC vehicles that look up their
# crew animations by the pawn's name (VehicleCrewAnimSetLookupTable.Mappings: CharacterName -> TheAnimSet; the
# Scarlett sandskiffs' GD_Orchid_Hovercraft.AnimSets.AnimSets_Hovercraft) gave him Gaige's female-rig sets on his
# male rig (players: bad posture in the sandskiffs, 2026-09-29). v84 settled the vehicles his body class maps
# (DLCVehicleAnimSetMappings) on Axton's sets, as his rig has every Soldier track bone; these tables are the same
# fix at runtime: while the local player is Jack, a table with both a Pawn_Mechromancer and a Pawn_Soldier entry
# gives the Mechromancer entry Axton's set, and gets its own back when anyone else is played. (A Gaige in the same
# co-op game as a Jack would share the table and see Axton's poses.)
VEHICLE_TABLE_SECONDS = 2.0
_vehicle_tables: dict = {}       # table path -> the Mechromancer entry's own AnimSet path
_vehicle_next = 0.0
_vehicle_patched_logged: set = set()


# The tables were found with a scan of every object in the game (find_all) every two seconds: a ~20 ms hitch every
# two seconds, all game long (doppelperf, 2026-10-01). The scan now runs every VEHICLE_SCAN_SECONDS (a vehicle's
# package loads with its vehicle, and the first scan after a load picks it up); in between only the tables already
# found are checked, by path.
VEHICLE_SCAN_SECONDS = 30.0
_vehicle_scan_next = 0.0
_vehicle_table_paths: list = []


def _vehicle_anim_tick(ours: bool) -> None:
    global _vehicle_next, _vehicle_scan_next
    now = time.monotonic()
    if now < _vehicle_next:
        return
    _vehicle_next = now + VEHICLE_TABLE_SECONDS
    tables = []
    if now >= _vehicle_scan_next:
        _vehicle_scan_next = now + VEHICLE_SCAN_SECONDS
        try:
            tables = [t for t in unrealsdk.find_all("VehicleCrewAnimSetLookupTable", exact=False) if not t.Name.startswith("Default__")]
            _vehicle_table_paths[:] = [t._path_name() for t in tables]
        except Exception:  # noqa: BLE001
            return
    else:
        for path in list(_vehicle_table_paths):
            try:
                t = unrealsdk.find_object("VehicleCrewAnimSetLookupTable", path)
            except Exception:  # noqa: BLE001
                t = None
            if t is None:
                _vehicle_table_paths.remove(path)
            else:
                tables.append(t)
    for table in tables:
        try:
            path = table._path_name()
            maps = list(table.Mappings)
            names = [str(m.CharacterName) for m in maps]
            if "Pawn_Mechromancer" not in names or "Pawn_Soldier" not in names:
                continue
            k, soldier = names.index("Pawn_Mechromancer"), maps[names.index("Pawn_Soldier")].TheAnimSet
            current = maps[k].TheAnimSet
            if ours:
                if soldier is None or current is soldier:
                    continue
                _vehicle_tables.setdefault(path, current._path_name() if current is not None else "")
                want = soldier
            else:
                if path not in _vehicle_tables:
                    continue
                own = _vehicle_tables.pop(path)
                want = unrealsdk.find_object("AnimSet", own) if own else None
            new = []
            for i, m in enumerate(maps):
                e = unrealsdk.make_struct("VehicleCrewAnimSetMapping", CharacterName=m.CharacterName,
                                          TheAnimSet=want if i == k else m.TheAnimSet)
                new.append(e)
            table.Mappings = new
            if ours and path not in _vehicle_patched_logged:
                _vehicle_patched_logged.add(path)
                diag.info(f"{TAG} vehicle crew anims: {table.Name} gives Jack {want.Name} (was {current.Name if current else None})")
        except Exception:  # noqa: BLE001
            _log_once("vehicleanims:" + str(getattr(table, "Name", "?")), "vehicle crew anim table fix failed: "
                      + traceback.format_exc().splitlines()[-1])


def player_tick(obj: UObject, dt: float, pawn: UObject | None, ours: bool) -> None:
    """From playertick (was its own POST PlayerTick hook)."""
    global _last_dt, _frame_no
    _frame_no += 1
    if 0.0 < dt < 0.5:
        _last_dt = dt
    # the imported particles' runtime materials, from the first tick on any instance: a Gaige client
    # draws a remote Jack's Potential nova and barrage comet from the same package (2026-09-17: the
    # tick existed but nothing called it; the nova's crackle sheets drew the package stand-in)
    diag.crumb("skills: fx materials")
    t = time.perf_counter()
    _fx_materials_tick()
    t = diag.section("skills.tick fx_materials", t)
    if ours:
        _lean_tick()
        _nova_tick()
        _quote_tick()
    t = diag.section("skills.tick lean+nova+quote", t)
    _vehicle_anim_tick(ours)
    t = diag.section("skills.tick vehicle anim", t)
    if ours:
        _dialog_addr_tick(obj)
        t = diag.section("skills.tick dialog addr", t)
    try:
        diag.crumb("skills: allies (every Jack)")
        ally_tick_all(obj)                 # each Jack's Teamwork / Commitment, a joined Jack's on any host
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import progression
        progression.max_grade_tick()       # Jack's skill caps follow a cap mod (Limitless), every ten seconds
    except Exception:  # noqa: BLE001
        pass
    if not ours:
        # the level gate lives in three Startup objects every class shares; this return used to come
        # before progression's tick, so after a Jack session everyone else kept his first-point level
        # (user, 2026-09-25)
        from . import progression
        progression.apply_gate(obj)
        return
    _ci_cash_tick(obj)
    t = diag.section("skills.tick ci cash", t)
    death_stage_tick()                           # the death graph and Leadership, a frame or two after a Digi-Jack dies
    t = diag.section("skills.tick death stages", t)
    global _health_snapshot_due
    if _health_snapshot_due and time.monotonic() >= _health_snapshot_due:
        _health_snapshot_due = 0.0
        _snapshot_clone_health("after")
    diag.crumb("skills: refresh attributes")
    try:
        _refresh_attributes(obj)
    except Exception:
        _log_once("tick", "attribute refresh failed: " + traceback.format_exc().splitlines()[-1])
    t = diag.section("skills.tick refresh_attributes", t)
    diag.crumb("skills: delegation")
    _delegation_tick(time.monotonic())
    t = diag.section("skills.tick delegation", t)
    diag.crumb("skills: merger")
    _merger_tick(time.monotonic())
    t = diag.section("skills.tick merger", t)
    diag.crumb("skills: kill skills")
    _kill_skill_tick(time.monotonic())
    diag.crumb("skills: com sweep")
    _com_sweep_tick(time.monotonic())
    _comskills_tick(time.monotonic())
    diag.crumb("skills: revive module")
    _revive_module_restore_tick(obj)
    t = diag.section("skills.tick killskill+sweep+revive", t)
    try:
        diag.crumb("skills: on my mark")
        _omm_tick(obj)
        t = diag.section("skills.tick omm", t)
        diag.crumb("skills: projectile tracker")
        _projectile_tracker_tick()
        t = diag.section("skills.tick projectile_tracker", t)
        diag.crumb("skills: laser surplus")
        _laser_surplus_census()
        t = diag.section("skills.tick laser_surplus_census", t)
        diag.crumb("skills: disillusion")
        _disillusion_tick(obj)
        diag.crumb("skills: screen fx")
        _screen_fx_bar_tick(obj)
        diag.crumb("skills: key hold")
        _skill_key_hold_tick(obj)
        t = diag.section("skills.tick disillusion+fxbar+keyhold", t)
        if not _ls_fixed:                    # the once-per-session package fix-ups (laser, Integrity burst) from the first tick, not the first throw
            _laser_surplus_fixups()
        if not _slag_fixed:
            msg = fix_sponsored_slag()
            if msg:
                unrealsdk.logging.info(f"{TAG} {msg}")
    except Exception:
        _log_once("omm:tick", "On My Mark tick failed: " + traceback.format_exc().splitlines()[-1])
    try:
        diag.crumb("skills: inspire")
        t = time.perf_counter()
        inspire_screen_tick(obj)             # the Inspire word, once its line has finished
        _rainbow_tick()                      # and the trail's colour, swept while it is on screen
        t = diag.section("skills.tick inspire+rainbow", t)
        diag.crumb("skills: collaborate")
        collaborate_tick()                   # the clones' share of Collaborate, when the stacks move
        t = diag.section("skills.tick collaborate", t)
        from . import progression
        progression.tick()                   # the single-point row rule, once per session
        diag.crumb("skills: progression")
        progression.points_tick(obj)         # and the unspent-point count, when a setting changed
        t = diag.section("skills.tick progression", t)
        diag.crumb("skills: amp")
        amp_tick()                           # the clones' amp shields, spent outside the damage stack
        winning_tick()                       # and Winning's restore when a kill refreshed it
        mip_spend_tick(obj)                  # and a cash drop that stayed down: Money Is Power's spend
        mip_load_reset_tick(obj)             # and the stacks a save load must not carry over
        orphan_self_tick(obj)                # and the Orphan Maker case's Sponsored By raise, after its hit landed
        deadly_bloom_tick()                  # and a Deadly Bloom clone's death nova after its break nova
        merger_infinite_tick(obj)            # and the endless Merger lasers, when the setting armed them
        merger_cast_tick(obj)                # and the cast that cancels Merger, a frame after the move stopped
        omm_queue_tick(obj)                  # and the rest of an On My Mark volley, a few grenades a frame
        shield_watch_tick(obj)               # `doppelskill shieldwatch`, while it runs
        diag.section("skills.tick amp+winning", t)
    except Exception:
        _log_once("progression:tick", "progression tick failed: " + traceback.format_exc().splitlines()[-1])
    global _last_weapon_path
    if _last_weapon_path is None:
        try:
            w = obj.Pawn.Weapon if obj.Pawn is not None else None
            if w is not None:
                _last_weapon_path = w._path_name()      # so the first swap after a load already counts
        except Exception:
            pass
    diag.crumb("skills: tick out")


@hook("WillowGame.WillowPlayerController:GFxMenuClosed", Type.POST)
def _on_menu_closed(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_menu_closed")
    global _gear_dirty
    if not _ours(obj):
        return
    _gear_dirty = True
    forget_action_skill_keys()               # the options menu is one of these: the bind may have changed
    if grade("MoneyIsPower", obj) > 0:
        _mip_note("menu", f"menu closed, OzOnMenuClosed raised ({_mip_context(obj)})")
    fire("SponsoredBy", "OzOnMenuClosed", pc=obj)
    fire("MoneyIsPower", "OzOnMenuClosed", pc=obj)


def _cash(pc: UObject) -> int | None:
    try:
        return int(list(pc.PlayerReplicationInfo.CurrencyOnHand)[0])
    except Exception:
        pass
    try:
        return int(pc.PlayerReplicationInfo.GetCurrencyOnHand(0))
    except Exception:
        return None


CURRENCY_CASH = 0       # ECurrency: cash, then Eridium, Seraph crystals, Torgue tokens
_currency_logged = 0


@hook("WillowGame.WillowPlayerController:OnCurrencyChanged", Type.POST)
def _on_currency_changed(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_currency_changed")
    """Money Is Power: a stack per cash pickup, stacks lost on money spent (TPS's OnPickup /
    OnPaidCashForUse). The engine hands the delta in: OnCurrencyChanged(ChangedCurrency
    {FormOfCurrency, StatName, CurrentAmount, LastKnownAmount}); the replicated
    CurrencyOnHand read before this lagged and reported pickups as spends."""
    global _currency_logged
    if not _ours(obj):
        return
    try:
        c = args.ChangedCurrency
        form, now_amount, last_amount = int(c.FormOfCurrency), int(c.CurrentAmount), int(c.LastKnownAmount)
        stat = str(c.StatName)
    except Exception:
        _log_once("cash", "OnCurrencyChanged args unreadable: " + traceback.format_exc().splitlines()[-1])
        return
    if _currency_logged < 6:
        _currency_logged += 1
        diag.info(f"{TAG} currency {form}: {last_amount} -> {now_amount}")
    if form != CURRENCY_CASH or grade("MoneyIsPower", obj) <= 0:
        return
    # challenge popups for collecting money reset the stacks (players, 2026-09-29): every cash change is noted
    # with its stat, next to the challenge and menu notes below, to see which path it takes
    _mip_note("cash", f"cash change {last_amount} -> {now_amount} (stat {stat})")
    # Pickups come from the pickup actor itself (_on_picked_up); a drop is a spend only if it stays down.
    _mip_cash["now"] = now_amount
    if now_amount < last_amount and _mip_cash["pending"] is None:
        _mip_cash["pending"] = {"from": last_amount, "to": now_amount, "at": time.monotonic(), "stat": stat}


# A cash drop starts Money Is Power's countdown (OnPaidCashForUse -> CE_StartRemovingStacks), and the drop used to be
# taken as a spend the moment it was reported. A player picked up a Hodunk's cash, the money challenge popped, and the
# stacks counted down as if money had been spent (a streamer, reported 2026-09-29; not reproduced here: 600 stacks
# through several challenge popups kept every one). The graph has only two ways to start the countdown, that spend
# and OnBledOut, and nothing else in Jack's package raises it. So the drop is now held for a moment and taken as a
# spend only if the cash is still below where it was, which rides out a count that dips and comes straight back
# (a correction right after a pickup, most likely from a co-op host).
MIP_SPEND_CONFIRM_SECONDS = 0.75
_mip_cash: dict = {"now": None, "pending": None}


def mip_spend_tick(pc: UObject) -> None:
    pending = _mip_cash["pending"]
    if pending is None or time.monotonic() - pending["at"] < MIP_SPEND_CONFIRM_SECONDS:
        return
    _mip_cash["pending"] = None
    now = _mip_cash["now"]
    if now is not None and now < pending["from"]:
        _mip_note("spend", f"cash {pending['from']} -> {now} (stat {pending['stat']}) stayed down: a spend, stacks drain "
                           f"({_mip_context(pc)})")
        fire("MoneyIsPower", "OnPaidCashForUse", pc=pc)
    else:
        _mip_note("dip", f"cash dipped {pending['from']} -> {pending['to']} (stat {pending['stat']}) and came back to {now}: "
                         f"not a spend ({_mip_context(pc)})")


# --- Marginal Benefit: the weapon in hand only (setting, off by default; 2026-10-01) -------------------------------
# Its graph refills every equipped weapon on a grenade throw: Behavior_RefillWeapon calls RefillClip() on the weapon
# it is handed, once per weapon the engine's grenade-override event gives it. With the setting on, every weapon but the
# one in Jack's hands is let go.
MARGINAL_BENEFIT_BPD = "Quince_Doppel_FreeEnterprise.Skills.MarginalBenefit."
_mb_active_option = None


def _mb_active_only() -> bool:
    try:
        return bool(_mb_active_option.value)
    except Exception:  # noqa: BLE001
        return False


@hook("WillowGame.Behavior_RefillWeapon:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_marginal_benefit")
def _on_refill_weapon(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_refill_weapon")
    if not _mb_active_only():
        return None
    try:
        if not obj._path_name().replace(":", ".").startswith(MARGINAL_BENEFIT_BPD):
            return None
        weapon = args.ContextObject
        pc = _pc()
        held = pc.Pawn.Weapon if pc is not None and pc.Pawn is not None else None
        if weapon is None or held is None or weapon._path_name() == held._path_name():
            return None
        return Block                     # a holstered weapon: left as it is
    except Exception:  # noqa: BLE001
        return None


# --- Money Is Power: what wipes the stacks (diagnostic, 2026-09-27) --------------------------------
# The skill's own graph zeroes Att_MoneyIsPower_Stacks on OnDeactivated (Behavior_SimpleMath_1381 writes
# a constant 0.0 into it; read from the Pre-Sequel package with upk_bpd_tail_set.py --list), and spending
# drains them (OnPaidCashForUse, which BL2 never raises itself: only the cash hook above does). Players
# see resets at random and on climbing into Boom Bewm's cannon. These lines say which it was, what the
# player was in and how many stacks there were, a few of each kind per session.
MIP_STACKS = "Quince_Doppel_FreeEnterprise.Misc.Att_MoneyIsPower_Stacks"
MIP_RESET_NODE = "Behavior_SimpleMath_1381"
MIP_LOG_CAP = 20     # raised for the challenge-popup reports (2026-09-29)
MIP_RESTORE_SECONDS = 2.0
_mip_saved: dict = {}          # controller path -> the stacks as the skill switched off: {'when', 'pc', 'body'}
_mip_logged: dict[str, int] = {}


def _mip_note(kind: str, msg: str) -> None:
    n = _mip_logged.get(kind, 0)
    if n < MIP_LOG_CAP:
        _mip_logged[kind] = n + 1
        unrealsdk.logging.info(f"{TAG} Money Is Power: {msg}")


def _mip_context(pc: UObject) -> str:
    """The stack count (read against the controller and against the body, until it is known which
    one the attribute lives on) and what the player is in."""
    try:
        pawn = pc.Pawn
        where = pawn.Class.Name if pawn is not None else "no pawn"
    except Exception:  # noqa: BLE001
        where = "?"
    from . import characterslot
    body = characterslot.character_pawn(pc)
    on_pc, on_body = attr_value(MIP_STACKS, pc), attr_value(MIP_STACKS, body)
    return f"stacks {on_pc} (controller) / {on_body} (body), player in {where}"


@hook("WillowGame.Behavior_SimpleMath:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_mip_reset")
def _on_mip_reset_node(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """The node that zeroes the stacks, whatever switched the skill off."""
    diag.hcrumb("skills._on_mip_reset_node")
    try:
        if obj.Name != MIP_RESET_NODE or obj.Outer.Outer.Name != "MoneyIsPower":
            return
    except Exception:  # noqa: BLE001
        return
    pc = _pc()
    if pc is None or not _ours(pc):
        return
    _mip_note("reset", f"stacks zeroed by the skill switching off ({_mip_context(pc)})")


def _mip_skill_note(skill: UObject, what: str) -> None:
    """Notes the skill switching off / on. Switched off and straight back on (the game refreshing skills, which
    challenge completions may do), the stacks the switch-off zeroed are put back: the skill's graph zeroes them
    on OnDeactivated, meant for the skill really going away."""
    try:
        d = skill.Definition
        if d is None or d.Name != "MoneyIsPower":
            return
    except Exception:  # noqa: BLE001
        return
    pc = _player_controller_of(getattr(skill, "SkillInstigator", None)) or _pc()
    if pc is None or not _ours(pc):
        return
    _mip_note(what, f"skill {what} ({_mip_context(pc)})")
    from . import characterslot
    body = characterslot.character_pawn(pc)
    try:
        who = pc._path_name()
        saved = _mip_saved.get(who)
        if what == "switching off":
            saved = _mip_saved[who] = {"when": time.monotonic(), "pc": attr_value(MIP_STACKS, pc), "body": attr_value(MIP_STACKS, body)}
            if _mip_keep():
                _mip_store(pc, max(saved.get("pc") or 0.0, saved.get("body") or 0.0))
        elif what == "switched on" and saved and time.monotonic() - saved["when"] <= MIP_RESTORE_SECONDS:
            restored = []
            for key, ctx in (("pc", pc), ("body", body)):
                value = saved.get(key)
                if value and ctx is not None and set_attr(MIP_STACKS, ctx, value):
                    restored.append(f"{value:.0f} on the {'controller' if key == 'pc' else 'body'}")
            _mip_saved.pop(who, None)
            if restored:
                _mip_note("restore", "switched straight back on: stacks put back (" + ", ".join(restored) + ")")
    except Exception:  # noqa: BLE001
        _log_once("mip:restore", "Money Is Power restore failed: " + traceback.format_exc().splitlines()[-1])


# The stacks are a DesignerAttributeDefinition (Scope Local): the engine keeps the value per context object, and the
# zero the skill writes as the save-quit tears the world down does not reliably reach the next session. Players who
# saved, quit and went straight back in kept their stacks (user, 2026-09-30); TPS starts every load at none. So reaching
# the main menu (characterslot's FrontendGFxMovie Start hook) and a Jack save load (its FinishSaveGameLoad hook) arm a
# reset, done once his pawn is in play. Both are needed: a quick Continue with the same character loads no save (the
# first test: no FinishSaveGameLoad between the save-quit and the way back in). Fast travel does neither, so the stacks
# still ride through level changes as they do in TPS.
MIP_LOAD_RESET_DELAY = 1.0
_mip_load_reset: dict = {}     # {'armed': monotonic time} while a reset is due


# Setting, off by default (2026-10-01): the stacks survive a save-quit. They are written per character (its save's
# SaveGameId) to sdk_mods/settings when the skill switches off, which a save-quit does first thing, and put back by
# the load check above instead of zeroed.
MIP_KEEP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings", "Doppelganger_mip_stacks.json")
_mip_keep_option = None


def _mip_keep() -> bool:
    try:
        return bool(_mip_keep_option.value)
    except Exception:  # noqa: BLE001
        return False


def _mip_save_key(pc: UObject) -> str | None:
    try:
        save = pc.GetCachedSaveGame()
        return str(int(save.SaveGameId)) if save is not None else None
    except Exception:  # noqa: BLE001
        return None


def _mip_load_all() -> dict:
    try:
        with open(MIP_KEEP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _mip_store(pc: UObject, value: float) -> None:
    key = _mip_save_key(pc)
    if key is None:
        return
    data = _mip_load_all()
    data[key] = float(value or 0.0)
    try:
        os.makedirs(os.path.dirname(MIP_KEEP_FILE), exist_ok=True)
        tmp = MIP_KEEP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, MIP_KEEP_FILE)
    except OSError:
        _log_once("mip:keepwrite", "Money Is Power: the kept stacks could not be written: " + traceback.format_exc().splitlines()[-1])


def _mip_stored(pc: UObject) -> float:
    key = _mip_save_key(pc)
    return float(_mip_load_all().get(key, 0.0) or 0.0) if key is not None else 0.0


def mip_reset_on_load() -> None:
    """From Jack's save load: zero Money Is Power's stacks once his pawn is in play."""
    _mip_load_reset["armed"] = time.monotonic()
    _mip_load_reset.pop("seen", None)
    _mip_saved.clear()         # a switch-off from the session that ended is never put back


def mip_load_reset_tick(pc: UObject) -> None:
    if "armed" not in _mip_load_reset:
        return
    pawn = pc.Pawn
    if pawn is None:
        return
    now = time.monotonic()
    seen = _mip_load_reset.setdefault("seen", now)
    if now - seen < MIP_LOAD_RESET_DELAY:
        return
    _mip_load_reset.clear()
    from . import characterslot
    body = characterslot.character_pawn(pc)
    if _mip_keep():
        kept = _mip_stored(pc)
        if kept:
            put = [label for label, ctx in (("controller", pc), ("body", body)) if ctx is not None and set_attr(MIP_STACKS, ctx, kept)]
            unrealsdk.logging.info(f"{TAG} Money Is Power: {kept:.0f} stacks kept from the last session (on the {', '.join(put) or 'nothing'})")
            return
    zeroed = []
    for label, ctx in (("controller", pc), ("body", body)):
        value = attr_value(MIP_STACKS, ctx) if ctx is not None else None
        if value and set_attr(MIP_STACKS, ctx, 0.0):
            zeroed.append(f"{value:.0f} on the {label}")
    if zeroed:
        unrealsdk.logging.info(f"{TAG} Money Is Power: stacks left over from the last session cleared ("
                               + ", ".join(zeroed) + ")")
    else:
        unrealsdk.logging.info(f"{TAG} Money Is Power: load check, no stacks carried over")


@hook("WillowGame.WillowPlayerController:ClientOnChallengeComplete", Type.POST, hook_identifier="doppel_mip_challenge")
def _on_challenge_complete(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """A challenge level completed (the popup): noted with the stacks, to line up with a reset."""
    diag.hcrumb("skills._on_challenge_complete")
    try:
        if _ours(obj) and grade("MoneyIsPower", obj) > 0:
            _mip_note("challenge", f"challenge completed ({_mip_context(obj)})")
    except Exception:  # noqa: BLE001
        pass


@hook("WillowGame.Skill:Deactivate", Type.PRE, hook_identifier="doppel_mip_deactivate")
def _on_skill_deactivate(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_skill_deactivate")
    _mip_skill_note(obj, "switching off")


@hook("WillowGame.Skill:Activate", Type.POST, hook_identifier="doppel_mip_activate")
def _on_skill_activate(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_skill_activate")
    _mip_skill_note(obj, "switched on")


_pickups_logged = 0


@hook("WillowGame.WillowPickup:PickedUpBy", Type.PRE)
def _on_picked_up(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_picked_up")
    """Money Is Power's stack: a cash pickup taken by our pawn (WillowPickup.PickedUpBy(P), the
    pickup's Inventory is the currency item)."""
    global _pickups_logged
    try:
        pc = _player_controller_of(args.P)
        if pc is None or (pc != _pc() and not _on_server()):
            return
    except Exception:
        return
    path = ""
    try:
        d = obj.GetPickupableInventoryDefinition()     # cash pickups carry no Inventory actor, only the definition
        path = d._path_name() if d is not None else ""
    except Exception:
        d = None
    if not path:
        try:
            inv = obj.GetPickupableInventory()
            path = inv.DefinitionData.ItemDefinition._path_name() if inv is not None else ""
        except Exception:
            pass
    if diag.enabled and _pickups_logged < 6:
        _pickups_logged += 1
        extra = {}
        for attr in ("Inventory", "InventoryClass", "PickupParticles", "InventoryRarityLevel", "bCostsToPickUp", "CostsToPickUpAmount", "CostsToPickUpType"):
            try:
                v = getattr(obj, attr)
                extra[attr] = getattr(v, "Name", v) if v is not None else None
            except Exception as e:
                extra[attr] = f"ERR {e}"
        try:
            extra["mesh"] = obj.GetPickupableMeshActor()
        except Exception as e:
            extra["mesh"] = f"ERR {e}"
        diag.info(f"{TAG} pickup: {obj.Class.Name} def={path!r} {extra}")
    # GD_Currency.A_Item.Currency_Money_Little / _Big (Eridium and crystals are Currency_Eridium_* / Currency_Crystal)
    if "Currency_Money" in path and grade("MoneyIsPower", pc) > 0 and "cash:stat" not in _logged:
        fire("MoneyIsPower", "OnPickup", pc=pc)


@hook("WillowGame.WillowInventoryManager:OnEquipped", Type.POST)
def _on_equipped(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_equipped")
    global _gear_dirty
    _gear_dirty = True


def tree_report(pc: UObject) -> str:
    """What the quick-change station computes for the Respec button, step by step:
    LoadSkillTreeDefinition(SkillTreePath).Root.Children -> PlayerSkillTree.GetBranchState(child)."""
    parts = []
    try:
        tree_path = pc.PlayerClass.SkillTreePath
        parts.append(f"path={tree_path}")
        tree = pc.PlayerSkillTree
        parts.append(f"PlayerSkillTree={tree}")
        loaded = pc.GetWillowGlobals().GetPlayerPawnDataManager().LoadSkillTreeDefinition(SkillTreeDataPath=tree_path, PlayerContext=pc)
        parts.append(f"loaded={loaded}")
        root = loaded.Root if loaded is not None else None
        parts.append(f"root={root}")
        for child in (list(root.Children) if root is not None else []):
            try:
                st = tree.GetBranchState(BranchDef=child)
                if isinstance(st, tuple):
                    st = st[-1]
                parts.append(f"{child.Name}: spent={st.PointsSpentInBranch} max={st.MaxPointsForBranch}")
            except Exception:
                parts.append(f"{child.Name}: GetBranchState failed: " + traceback.format_exc().splitlines()[-1])
        try:
            raw = [f"{s.BranchDefinition.Name if s.BranchDefinition is not None else None}={s.PointsSpentInBranch}" for s in tree.Branches]
        except Exception:
            raw = ["<Branches unreadable: " + traceback.format_exc().splitlines()[-1] + ">"]
        parts.append(f"raw states={raw}")
        parts.append(f"grades: PromotetheRanks={grade('PromotetheRanks', pc)} Bolster={grade('Bolster', pc)} (class mod +{class_mod_points('Bolster', pc)}) Potential={grade('Potential', pc)}")
        parts.append(f"unspent={pc.PlayerReplicationInfo.GeneralSkillPoints}")
    except Exception:
        parts.append("report failed: " + traceback.format_exc().splitlines()[-1])
    return "; ".join(parts)


SKILL_TREE_DEF = "GD_Doppelganger_Streaming.SkillTree.SkillTree_Doppelganger"


def branch_points(pc: UObject) -> list:
    """[(branch name, spent, max)] from the player's own PlayerSkillTree (its Branches carry the
    branch definitions; the SkillTreeDefinition object itself is garbage-collected once the
    tree is built, which is also why the station's loader finds nothing)."""
    out = []
    tree = pc.PlayerSkillTree
    for b in list(tree.Branches):
        d = b.Definition
        if d is None or int(b.ParentBranchIndex) < 0:      # the root (action-skill) branch has no parent
            continue
        st = tree.GetBranchState(BranchDef=d, OutBranchState=unrealsdk.make_struct("SkillTreeBranchStateData"))
        if isinstance(st, tuple):
            st = st[-1]
        out.append((str(d.BranchName), int(st.PointsSpentInBranch), int(st.MaxPointsForBranch)))
    return out


@hook("WillowGame.CustomizationGFxMovie:CacheSkillBranchDescriptions", Type.POST)
def _on_customization_cache(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_customization_cache")
    """
    The quick-change station's Respec entry is enabled from bHasSpentSkillPoints, which this
    function computes after WillowPlayerPawnDataManager.LoadSkillTreeDefinition(SkillTreePath)
   ; and that native loader returns None for our tree (logged 2026-09-13), so the station
    saw 0 points spent in every branch. Recompute from the tree object itself.
    """
    try:
        pc = obj.WPCOwner
        if not _ours(pc):
            return
        pts = branch_points(pc)
        spent = any(n > 0 for _, n, _ in pts)
        obj.bHasSpentSkillPoints = spent
        try:
            obj.SkillBranchDescriptions = [f"{name}: {n}/{m}" for name, n, m in pts]
        except Exception:
            pass
        diag.info(f"{TAG} respec check: engine bHasSpentSkillPoints=False, set {spent}; branches {pts}")
    except Exception:
        _log_once("respec", "respec fix failed: " + traceback.format_exc().splitlines()[-1] + "; " + tree_report(pc))


_kill_logged = 0


@hook("WillowGame.SkillDefinition:OnKilledEnemy", Type.POST)
def _on_skill_killed_enemy(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_skill_killed_enemy")
    """Diagnostic: which kills reach Jack's kill-event skills (does BL2 route Digi-Jack kills here?)."""
    global _kill_logged
    if _kill_logged >= 20:
        return
    try:
        if not str(obj._path_name()).startswith("Quince_Doppel_"):
            return
        _kill_logged += 1
        diag.info(f"{TAG} kill event reached {obj.Name}: instigator {args.SkillInstigator} enemy {args.Enemy}")
    except Exception:
        pass


_heal_logged = 0


_sponsored_fired = 0


@hook("WillowGame.Behavior_CauseDamage:ApplyBehaviorToContext", Type.PRE)
def _on_cause_damage(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_cause_damage")
    try:
        if obj.Name in CI_PAYOUT_NODES and ci_cash_enabled() and _ci_node_is_ours(obj):
            _ci_book_payout(obj, args)
    except Exception:  # noqa: BLE001
        _log_once("ci:cashbook", "Compound Interest cash: payout hook failed: " + traceback.format_exc().splitlines()[-1], always=True)
    # Which of Sponsored By's five element branches actually runs. Everything about the slag branch
    # checks out on paper - the condition reads WeaponDamage_Is_Amp (measured at 1.0 with a slag gun
    # in hand), it routes to a CauseDamage carrying DmgType_Amp_Impact, and its damage inputs are
    # the same variables as the shock branch that works. So the question left is whether the node
    # runs at all, and that can only be answered here (2026-09-23).
    global _sponsored_fired
    try:
        n_ = obj.Name
        if n_ in ("Behavior_CauseDamage_3", "Behavior_CauseDamage_94"):
            op = obj._path_name()
            if op.startswith("Quince_Doppel_GreaterGood.Skills.Delegation"):
                _delegation_counts["heal"] += 1
            elif op.startswith("Quince_Doppel_GreaterGood.Projectiles.Projectile_Delegation"):
                _delegation_counts["bolt damage"] += 1
    except Exception:  # noqa: BLE001
        pass
    if _sponsored_fired < 12:  # noqa: SIM102
        try:
            outer = obj._path_name()
            if "Quince_Doppel" in outer:
                _sponsored_fired += 1
                dt = obj.DamageTypeDefinition
                diag.detail(f"{TAG} skill damage: {outer} -> "
                            f"{dt.Name if dt is not None else 'no damage type'} on "
                            f"{args.ContextObject.Name if args.ContextObject is not None else '?'}")
        except Exception:  # noqa: BLE001
            pass
    """Diagnostic: Take Their Freedom's heal nodes (how many fire per kill, on what)."""
    global _heal_logged
    if _heal_logged >= 24:
        return
    try:
        path = obj._path_name()
        if "TakeTheirFreedom" not in path:
            return
        _heal_logged += 1
        tgt = args.ContextObject
        hp = ""
        try:
            hp = f" hp {tgt.GetHealth():.0f}/{tgt.GetMaxHealth():.0f}"
        except Exception:
            pass
        diag.info(f"{TAG} heal node {path.split('.')[-1]} on {tgt}{hp}")
    except Exception:
        pass


_explode_logged = 0


# --- The Digi-Jacks' death novas (players on 0.9.8.1, 2026-09-28) -----------------------------------------------
# The clone's body-death graph (BodyDeath_DigiJack's BehaviorProviderDefinition_6) sets off Optimism's explosion
# and, with You Have My Shield, the clone's shield nova when it dies. Its native variable records lost 17 object
# references when the graph came over from the Pre-Sequel (they were nulled where no import existed at the time;
# compare the two packages with upk_bpd_tail_set.py --list): Optimism's damage is (enemy non-weapon damage x 2) x
# (the Optimism grade x 0.5) and the grade attribute is gone, so it knocked enemies back for 0 damage; every
# Att_ShieldNovaSize_* is gone, so the death nova had radius 0; and it fires after the clone's shield copy and its
# PlayerMaster are gone, so its damage read 100. The graph copies its variables into the node's properties just
# before this call, so the node is given the right numbers here, every time it runs:
#   Optimism:   damage = Init_EnemyNonWeaponDamage (the engine evaluates it at the clone's level) x the grade
#               (class-mod points included), TPS's formula
#   death nova: damage, radius and status damage from the clone's shield copy if it is still there, otherwise the
#               player's shield (the copy's source), with the nova radius setting on the radius.
# The shield's own nova (broken by enemies) is the shield item's skill, not this graph, and was always right.
CLONE_DEATH_BPD = ("BodyDeath_DigiJack", "BehaviorProviderDefinition_6")
OPTIMISM_EXPLODE_NODE = "Behavior_Explode_2"
INIT_ENEMY_NONWEAPON = "GD_Balance_HealthAndDamage.HealthAndDamage.Init_EnemyNonWeaponDamage"
DEATH_NOVA_ELEMENTS = (("Shock", "Shock"), ("Incendiary", "Fire"), ("Corrosive", "Corrosive"), ("Explosive", "Explosive"))
_death_nova_status: dict = {}      # node path -> the StatusEffectDamage scale the package gives it
_death_nova_definition: dict = {}  # node path -> the path of the explosion the package gives it (the Firehawk swaps it per death)
_death_nova_logged = 0


def _init_value(const: float, init=None, scale: float = 1.0):
    return unrealsdk.make_struct("AttributeInitializationData", BaseValueConstant=float(const), BaseValueAttribute=None,
                                 InitializationDefinition=init, BaseValueScaleConstant=float(scale))


def _fix_clone_death_nova(node: UObject, me: UObject) -> None:
    global _death_nova_logged
    outer = node.Outer
    if outer is None or outer.Name != CLONE_DEATH_BPD[1] or outer.Outer is None or outer.Outer.Name != CLONE_DEATH_BPD[0]:
        return
    pc = _cast_pc()
    if pc is None:
        return
    if node.Name == OPTIMISM_EXPLODE_NODE:
        g = grade("Optimism", pc)
        init = _obj(INIT_ENEMY_NONWEAPON)
        if g <= 0 or init is None:
            return
        node.DamageFormula = _init_value(0.0, init, float(g))      # as the graph's own variable: constant 0 + the init
        note = f"Optimism explosion: enemy non-weapon damage x {g}"
    else:
        d = node.Definition
        element = next((e for key, e in DEATH_NOVA_ELEMENTS if d is not None and key in d.Name), None)
        if element is None:
            return                                   # the Pre-Sequel's ice and singularity novas: nothing in BL2
        pawn = me if getattr(me, "AIClass", None) is not None else getattr(me, "Pawn", None)
        holder, scaled = None, False
        copy_path = _shield_copies.get(pawn._path_name()) if pawn is not None else None
        if copy_path:
            try:
                holder, scaled = unrealsdk.find_object("WillowShield", copy_path), True
            except Exception:  # noqa: BLE001
                holder = None
        if holder is None:
            holder, scaled = pc.Pawn, False
        dmg = attr_value(f"GD_Shields.Misc.Att_ShieldNovaDamage_{element}", holder) or 0.0
        size = attr_value(f"GD_Shields.Misc.Att_ShieldNovaSize_{element}", holder) or 0.0
        if dmg <= 0.0 or size <= 0.0:
            return
        if not scaled:
            size *= nova_factor()
        path = node._path_name()
        # the Firehawk's copy: its own Phoenix explosion instead of the plain fire nova, once (see YHMS_SPIKE_SHIELDS);
        # the node is shared by every clone, so its own explosion goes back on for any other shield
        firehawk = element == "Fire" and _holder_shield_def(holder) == FIREHAWK_SHIELD
        if pawn is not None and time.monotonic() - _special_novas.get(pawn._path_name(), 0.0) < 5.0:
            return True                              # its shield's own nova went off at the death (yhms_special)
        if path not in _death_nova_definition:
            _death_nova_definition[path] = node.Definition._path_name() if node.Definition is not None else None
        want = (_obj(PHOENIX_EXPLOSION) if firehawk else None) or _obj(_death_nova_definition[path] or "")
        if want is not None and (node.Definition is None or node.Definition._path_name() != want._path_name()):
            node.Definition = want
        if path not in _death_nova_status:
            sd = node.StatusEffectDamage
            _death_nova_status[path] = float(sd.BaseValueScaleConstant) if sd.BaseValueAttribute is not None else None
        node.DamageFormula = _init_value(dmg)
        node.DamageRadiusFormula = _init_value(size)
        status_scale = _death_nova_status[path]
        if status_scale is not None:
            node.StatusEffectDamage = _init_value(dmg * status_scale)
        note = (f"death nova ({element}{', Firehawk Phoenix explosion' if firehawk else ''}): damage {dmg:.0f}, radius {size:.0f} "
                f"from the {'shield copy' if scaled else 'player shield'}")
    if _death_nova_logged < 4:
        _death_nova_logged += 1
        unrealsdk.logging.info(f"{TAG} Digi-Jack {note}")


@hook("WillowGame.Behavior_Explode:ApplyBehaviorToContext", Type.PRE)
def _on_explode(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_explode")
    """The Digi-Jacks' death novas get their numbers here (_fix_clone_death_nova); the rest is diagnostic: which
    explosion definitions the Digi-Jack graphs actually run."""
    global _explode_logged
    try:
        from . import digijack
        with digijack.using_clone(args.SelfObject):         # the clone's own Jack's grades (co-op)
            fixed = _fix_clone_death_nova(obj, args.SelfObject)
        if fixed is True:
            return Block
    except Exception:  # noqa: BLE001
        _log_once("deathnova", "Digi-Jack death nova fix failed: " + traceback.format_exc().splitlines()[-1], always=True)
    if _explode_logged >= 12:
        return
    try:
        me = args.SelfObject
        try:
            mp = me._path_name() if me is not None else ""
            if mp in _tracked_projectiles:
                d = obj.Definition
                diag.info(f"{TAG} projectile {_tracked_projectiles[mp][0]}: Behavior_Explode {obj.Name} (provider {obj.Outer.Name}) def {d.Name if d is not None else None} source {obj.DamageSource} at {_v(me.Location)}")
        except Exception:
            pass
        try:
            if "BadassShot" in obj.Outer._path_name():
                n = _grenade_probe.get("badass-explode", 0)
                if n < 6:
                    _grenade_probe["badass-explode"] = n + 1
                    d = obj.Definition
                    diag.info(f"{TAG} badass barrage: {obj.Name} explodes on {me.Name if me is not None else None} (def {d.Name if d is not None else None}, source {obj.DamageSource})")
                return
        except Exception:
            pass
        try:
            if "SlamLaser" in obj.Outer._path_name():
                n = _grenade_probe.get("ls-explode", 0)
                if n < 4:
                    _grenade_probe["ls-explode"] = n + 1
                    d = obj.Definition
                    diag.info(f"{TAG} Laser Surplus: {obj.Name} explodes on {me.Name if me is not None else None} at {_v(me.Location) if me is not None else '?'} (def {d.Name if d is not None else None}, source {obj.DamageSource})")
                return
        except Exception:
            pass
        ai = getattr(me, "AIClass", None)
        owner = None
        if ai is None and "Shield" in str(getattr(getattr(me, "Class", None), "Name", "")):
            owner = getattr(me, "Owner", None)          # a nova from a shield: whose shield is it?
            ai = getattr(owner, "AIClass", None)
        if ai is None and getattr(me, "Pawn", None) is not None:
            ai = getattr(me.Pawn, "AIClass", None)      # the AI graph runs on the clone's mind (Potential)
        if ai is None or "DigiJack" not in str(ai.Name):
            return
        _explode_logged += 1
        d = obj.Definition
        diag.info(f"{TAG} explode: {d.Name if d is not None else None} on {me.Name}"
                  + (f" (shield of {owner.Name})" if owner is not None else "")
                  + f" (node {obj.Name}, provider {obj.Outer.Name})")
    except Exception:
        pass


@hook("WillowGame.Behavior_IncrementPlayerStat:ApplyBehaviorToContext", Type.PRE)
def _on_cash_stat(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_cash_stat")
    """Money Is Power's stack, the reliable way: BL2's cash items (GD_Currency.A_Item.Currency*) run
    OnUsed -> Behavior_IncrementPlayerStat(STAT_PLAYER_DOLLARS_COLLECTED_FROM_CASH_DROPS) when
    picked up; the pickup actor itself has no inventory left by the time PickedUpBy runs."""
    try:
        if "DOLLARS_COLLECTED_FROM_CASH" not in str(obj.StatId):
            return
        ctx = args.ContextObject
        pc = _player_controller_of(ctx) if ctx is not None else _pc()
        if pc is None or not _ours(pc) or (pc != _pc() and not _on_server()):
            return
    except Exception:
        return
    _log_once("cash:stat", "cash pickup seen through the currency item's stat node")
    if grade("MoneyIsPower", pc) > 0:
        fire("MoneyIsPower", "OnPickup", pc=pc)


_last_weapon_path: str | None = None
_other_last_weapon: dict = {}       # co-op host: joined Jacks' previous main weapon, by pawn path
_swaps_logged = 0


@hook("WillowGame.WillowPlayerPawn:WeaponChanged", Type.POST)
def _on_weapon_changed(obj: UObject, args: WrappedStruct, ret: None, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_weapon_changed")
    """Sponsored By: TPS raised OzOnOldWeaponSwapped(SkillInstigator, OldWeapon) on every swap and the
    skill's graph checks the manufacturer of THAT gun ("the last gun you held"). BL2 only has
    OnWeaponSwapped(NewWeapon), which made the boost follow the gun swapped TO, so the graph
    (v45) listens to the old-weapon event and it is raised here with the previous main weapon."""
    global _last_weapon_path, _swaps_logged
    try:
        pc = _player_controller_of(obj)
        local = pc is not None and pc == _pc()
        if pc is None or args.bOffHandWeapon or (not local and not _on_server()):
            return
        new = args.TheWeapon
        new_path = new._path_name() if new is not None else None
        pawn_key = obj._path_name()
    except Exception:
        return
    old_path = _last_weapon_path if local else _other_last_weapon.get(pawn_key)
    if new_path is None or new_path == old_path:
        return
    if local:
        _last_weapon_path = new_path
    else:
        _other_last_weapon[pawn_key] = new_path        # a joined Jack's, on the host
    if old_path is None:
        return
    old = None
    try:
        old = unrealsdk.find_object("WillowWeapon", old_path)
    except Exception:
        pass
    if _swaps_logged < 8:
        _swaps_logged += 1
        try:
            mf = old.DefinitionData.ManufacturerDefinition.Name if old is not None else None
        except Exception:
            mf = "?"
        diag.info(f"{TAG} weapon swap: from {old_path.split('.')[-1] if old_path else None} ({mf}) to {new_path.split('.')[-1]}")
    if old is not None and grade("SponsoredBy", pc) > 0:
        fire("SponsoredBy", "OzOnOldWeaponSwapped", pc=pc, params=event_params(pc, old))


JACKS_CACHE_ACTIVE = "Quince_Doppel_Hero.Skills.JacksCache_Active"
_cache_throws_logged = 0


@hook("WillowGame.WillowPlayerController:CanAffordGrenadeCost", Type.PRE)
def _on_can_afford_grenade(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_can_afford_grenade")
    """Jack's Cache with an empty grenade pouch: TPS's JacksCache_Active also set
    CanThrowGrenadeWithoutAmmo (no such attribute in BL2, sunk on import), so at 0 grenades the
    input never reached the throw-override path the skill listens on. While the cached throw is
    armed, the ammo check says yes; the override skill then does the throw and spends stacks."""
    global _cache_throws_logged
    try:
        if not _ours(obj):
            return None
        sk = active_skill(JACKS_CACHE_ACTIVE, obj)
        if sk is None or int(sk.SkillState) != 1:      # SKILL_Active
            return None
    except Exception:
        return None
    if _cache_throws_logged < 5:
        _cache_throws_logged += 1
        diag.info(f"{TAG} Jack's Cache armed: grenade cost check bypassed")
    return (Block, True)


INTEGRITY_CONDITIONALS = {
    "Quince_Doppel_GreaterGood.Skills.Integrity.BehaviorProviderDefinition_0.Behavior_Conditional_266",
    "Quince_Doppel_GreaterGood.Skills.Integrity.BehaviorProviderDefinition_0.Behavior_Conditional_267",
}
INTEGRITY_CONDITIONAL_NAMES = {p.split(".")[-1] for p in INTEGRITY_CONDITIONALS}
INTEGRITY_HEALTH_FRACTION = 0.4
INTEGRITY_MERGE_NUMBERS = False          # TPS shows the bonus as its own damage number (checked in TPS 2026-09-15); the merge stays available
INTEGRITY_DAMAGE_PER_LEVEL = 0.08        # Att_Integrity_DamagePerLevel
INTEGRITY_FIRST_HIT_MULT = 4.0           # Att_Integrity_BonusMultiplier (the "not damaged previously" branch)
_integrity_logged = 0
_integrity_pending: dict = {}            # enemy name -> the bonus popup expected after the hit
_in_display = False
_merges_logged = 0


@hook("WillowGame.WillowDamageTypeDefinition:DisplayRecentDamageForPlayer", Type.PRE)
def _on_display_damage(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_display_damage")
    """One damage number for an Integrity hit (TPS shows 35, BL2 showed 25 then 10): the graph's
    bonus is a second damage event two frames later, so its popup is a separate record. When the
    conditional has just fired for this enemy, the hit's popup is shown with the bonus folded in
    and the bonus's own popup is swallowed."""
    global _in_display, _merges_logged
    if not INTEGRITY_MERGE_NUMBERS or _in_display or not _integrity_pending:
        return None
    try:
        pc = args.PC
        if pc is None or not _ours(pc):
            return None
        ev = args.DamageEventData
        actor = ev.DamagedActor
        if actor is None:
            return None
        pend = _integrity_pending.get(actor.Name)
        if pend is None:
            return None
        now = time.monotonic()
        if now - pend["t"] > 0.5:
            _integrity_pending.pop(actor.Name, None)
            return None
        amount = float(ev.TotalDamageForDamageType)
        if pend["expected"] is None:
            # the hit itself: show hit + bonus, remember what the bonus popup will say
            bonus = amount * pend["mult"]
            pend["expected"] = bonus
            merged = unrealsdk.make_struct("PlayerRecentDamageEventData", DamagedActor=actor, DamageTypeDefinition=ev.DamageTypeDefinition,
                                           TotalDamageForDamageType=amount + bonus, DamageTime=ev.DamageTime, DamageLocation=ev.DamageLocation,
                                           DamageEventFlags=ev.DamageEventFlags, bDirty=ev.bDirty)
            _in_display = True
            try:
                obj.DisplayRecentDamageForPlayer(PC=pc, DamageEventData=merged)
            finally:
                _in_display = False
            if _merges_logged < 5:
                _merges_logged += 1
                diag.info(f"{TAG} Integrity: damage number {amount:.1f} shown as {amount + bonus:.1f} (bonus {bonus:.1f} folded in)")
            return Block
        expected = pend["expected"]
        if abs(amount - expected) <= max(1.0, 0.15 * expected):
            _integrity_pending.pop(actor.Name, None)
            return Block                     # the bonus's own popup, already counted
    except Exception:
        _log_once("integrity:merge", "Integrity damage-number merge failed: " + traceback.format_exc().splitlines()[-1])
    return None


_integrity_burst_last: dict = {}


_integrity_fx_option = None


def integrity_fx_on() -> bool:
    if _integrity_fx_forced is not None:
        return _integrity_fx_forced
    return _integrity_fx_option is None or bool(_integrity_fx_option.value)


def apply_integrity_fx() -> str:
    """The burst has two spawners and the setting has to reach both. Ours is _integrity_burst, for
    the case the graph's node lands nowhere; the graph's own Behavior_SpawnParticleSystem_103 is
    armed by _laser_surplus_fixups, and gating only the Python one left the setting doing nothing
    visible (user, 2026-09-22). Emptying the node's template is what stops it."""
    want = integrity_fx_on()
    try:
        node = unrealsdk.find_object("Behavior_SpawnParticleSystem", INTEGRITY_BURST_NODE)
    except Exception:  # noqa: BLE001  not resident yet: _laser_surplus_fixups does it later
        return "not resident"
    try:
        have = node.ParticleEffect is not None
        if want == have:
            return "unchanged"
        node.ParticleEffect = unrealsdk.find_object("ParticleSystem", INTEGRITY_BURST) if want else None
        return f"Integrity burst {'on' if want else 'off'}"
    except Exception:  # noqa: BLE001
        return "failed: " + traceback.format_exc().splitlines()[-1][:90]


def _integrity_changed(value) -> None:
    global _integrity_fx_forced
    _integrity_fx_forced = bool(value)
    r = apply_integrity_fx()
    (diag.detail if r in ("unchanged", "not resident") else unrealsdk.logging.info)(f"{TAG} Integrity explosions: {r}")


_integrity_fx_forced = None
_orphan_self_option = None


def make_options() -> list:
    """The settings that live with the skills: the Integrity burst, Compound Interest's cash and how
    much Jack talks."""
    global _integrity_fx_option, _ci_cash_option
    from mods_base import BoolOption
    from . import digijack
    _integrity_fx_option = BoolOption(
        "Integrity explosions", True, "On", "Off",
        description="The burst on an enemy when Integrity's bonus damage lands.",
        **diag.on_change(lambda option, value: _integrity_changed(value)))
    _ci_cash_option = BoolOption(
        "Compound Interest drops cash", False, "On", "Off",
        description="A payout also drops a money pile per 100% on the counter (210% drops 2).")
    global _lean_option, _nova_option
    from mods_base import SpinnerOption
    _lean_option = BoolOption(
        "Lean On Me slag shot", False, "On", "Off",
        description="Lean On Me's bonus shot deals slag instead of corrosive.",
        **diag.on_change(lambda option, value: _lean_changed()))
    _nova_option = SpinnerOption(
        "Digi-Jack nova radius", NOVA_CHOICES[0], list(NOVA_CHOICES),
        description="How far Digi-Jack novas reach next to yours (You Have My Shield).")
    global _orphan_self_option
    _orphan_self_option = BoolOption(
        "Sponsored By on the Orphan Maker", False, "On", "Off",
        description="The Orphan Maker's hit on you can proc Sponsored By, as hits on enemies do.")
    global _omm_direct_option
    _omm_direct_option = BoolOption(
        "More accurate On My Mark", False, "On", "Off",
        description="On My Mark grenades fly fast and straight at the target instead of lobbing.")
    global _mb_active_option, _mip_keep_option, _merger_infinite_option
    _mb_active_option = BoolOption(
        "Marginal Benefit: weapon in hand only", False, "On", "Off", display_name="Marginal Benefit: in hand only",
        description="Marginal Benefit refills only the weapon in your hands.")
    _mip_keep_option = BoolOption(
        "Money Is Power keeps its stacks", False, "On", "Off",
        description="Money Is Power's stacks survive a save and quit.")
    _merger_infinite_option = BoolOption(
        "Infinite Merger (Pre-Sequel bug)", False, "On", "Off",
        description="Cast your action skill just before Merger's lasers appear to keep them firing.")
    return [_integrity_fx_option, _ci_cash_option, _lean_option, _omm_direct_option, _orphan_self_option,
            _mb_active_option, _mip_keep_option, _merger_infinite_option,
            digijack.make_op_option(), _nova_option, digijack.make_chatter_option()]


def _lean_changed() -> None:
    """The option's value is set after this callback returns; apply on the next tick."""
    global _lean_next
    _lean_next = 0.0


# --- Lean On Me: a slag shot instead of corrosive (the user's request, 2026-09-27; off by default) ------
# The bonus shot is Projectile_WristLaserBlast (and the backup energy ball) running its LeanOnMeExtra
# sequence: a hit goes through a corrosive CauseDamage, the spawn shows a corrosive muzzle flash, and it
# explodes with Explosion_LeanOnMe_Digijack_WristLaser (corrosive particles and impact). With the setting
# on, those point at Borderlands 2's slag instead (DmgType_Amp_Impact, and the particles and impact
# Explosion_SlagMaster uses), and the skill's description says so. They are package objects, so the
# swap is checked every few seconds in play: a reload of the package brings the disk values back.
LEAN_SKILL = "Quince_Doppel_Hero.Skills.LeanOnMe"
_LEAN_BLAST = "GD_Quince_JackClone.Projectiles.Projectile_WristLaserBlast.BehaviorProviderDefinition_0."
_LEAN_BALL = "GD_Quince_JackClone.Projectiles.Projectile_BackupEnergyBall.BehaviorProviderDefinition_0."
LEAN_SWAPS = (   # (object path, property, corrosive, slag)
    (_LEAN_BLAST + "Behavior_CauseDamage_165", "DamageTypeDefinition",
     "GD_Corrosive.DamageType.DmgType_Corrosive_Impact", "GD_Amp.DamageType.DmgType_Amp_Impact"),
    (_LEAN_BLAST + "Behavior_CauseDamage_165", "ImpactDefinition",
     "GD_Impacts.ExplosiveImpacts.ExplosiveImpactCorrosive128", "GD_Impacts.ExplosiveImpacts.ExplosiveImpactEridian128"),
    (_LEAN_BALL + "Behavior_CauseDamage_179", "DamageTypeDefinition",
     "GD_Corrosive.DamageType.DmgType_Corrosive_Impact", "GD_Amp.DamageType.DmgType_Amp_Impact"),
    (_LEAN_BALL + "Behavior_CauseDamage_179", "ImpactDefinition",
     "GD_Impacts.ExplosiveImpacts.ExplosiveImpactCorrosive128", "GD_Impacts.ExplosiveImpacts.ExplosiveImpactEridian128"),
    (_LEAN_BLAST + "Behavior_SpawnParticleSystem_167", "ParticleEffect",
     "FX_WEP_Alien.Particles.Part_Snipe_Alien_Corrosive_Muzzleflash", "FX_WEP_Alien.Particles.Part_Snipe_Alien_Slag_Muzzleflash"),
)
LEAN_EXPLOSION = "GD_Quince_JackClone.Explosion.Explosion_LeanOnMe_Digijack_WristLaser"
LEAN_EXPLOSION_SLAG = ("FX_WEP_Explosions.Particles.Slag.Part_WEP_SlagExplosionSmall",
                       "GD_Impacts.ExplosiveImpacts.ExplosiveImpactEridian128")
LEAN_WORDS = ("Bonus Corrosive Shot", "Bonus Slag Shot")
LEAN_CHECK_SECONDS = 5.0
_lean_option = None
_lean_original: dict = {}        # "explosion" -> (particle path, impact path) as the package has them
_lean_state = None               # the element last applied: "slag" / "corrosive"
_lean_next = 0.0


def lean_slag_on() -> bool:
    return bool(_lean_option.value) if _lean_option is not None else False


def _obj(path: str):
    try:
        return unrealsdk.find_object("Object", path)
    except Exception:  # noqa: BLE001
        return None


def apply_lean_element(why: str) -> str:
    """Point Lean On Me's bonus shot at slag or back at corrosive, whichever the setting says."""
    global _lean_state
    slag = lean_slag_on()
    done, missing = 0, []
    for path, prop, corrosive, slag_path in LEAN_SWAPS:
        node, want = _obj(path), _obj(slag_path if slag else corrosive)
        if node is None or want is None:
            missing.append(path.split(".")[-1] if node is None else (slag_path if slag else corrosive).split(".")[-1])
            continue
        try:
            cur = getattr(node, prop)
            if cur is None or cur._path_name() != want._path_name():
                setattr(node, prop, want)
                done += 1
        except Exception:  # noqa: BLE001
            missing.append(f"{path.split('.')[-1]}.{prop}")
    expl = _obj(LEAN_EXPLOSION)
    if expl is not None:
        try:
            scale = expl.ExplosionScale[0]
            if "explosion" not in _lean_original:
                _lean_original["explosion"] = (scale.ExplosionPSTemplate._path_name() if scale.ExplosionPSTemplate is not None else "",
                                               scale.ImpactEffect._path_name() if scale.ImpactEffect is not None else "")
            ps_path, impact_path = LEAN_EXPLOSION_SLAG if slag else _lean_original["explosion"]
            ps, impact = _obj(ps_path), _obj(impact_path)
            cur = (scale.ExplosionPSTemplate._path_name() if scale.ExplosionPSTemplate is not None else "",
                   scale.ImpactEffect._path_name() if scale.ImpactEffect is not None else "")
            if ps is not None and impact is not None and cur != (ps._path_name(), impact._path_name()):
                scale.ExplosionPSTemplate = ps
                scale.ImpactEffect = impact
                expl.ExplosionScale[0] = scale
                done += 1
        except Exception:  # noqa: BLE001
            missing.append("explosion: " + traceback.format_exc().splitlines()[-1][:80])
    else:
        missing.append("Explosion_LeanOnMe_Digijack_WristLaser")
    d = skill_def("LeanOnMe")
    if d is not None:
        try:
            text = str(d.SkillDescription)
            a, b = LEAN_WORDS if slag else LEAN_WORDS[::-1]
            if a in text:
                d.SkillDescription = text.replace(a, b)
                done += 1
        except Exception:  # noqa: BLE001
            missing.append("description")
    state = "slag" if slag else "corrosive"
    if done or state != _lean_state or missing:
        note = f"Lean On Me: {state} shot ({why}): {done} change(s)" + (f"; not found {missing}" if missing else "")
        (unrealsdk.logging.info if done or missing else diag.detail)(f"{TAG} {note}")
    _lean_state = state
    return state


def _lean_tick() -> None:
    global _lean_next
    now = time.monotonic()
    if now < _lean_next:
        return
    _lean_next = now + LEAN_CHECK_SECONDS
    # nothing to put back until the setting has been on at least once this session
    if lean_slag_on() or _lean_state == "slag":
        apply_lean_element("check")


# --- The flavour quotes of the three five-rank skills (user, 2026-09-27) --------------------------------
# v82 made the quotes of Optimism, Compound Interest and Supply and Demand orange card lines: a quote
# presentation in SkillEffectPresentations, as TPS does for its one-rank skills. A five-rank skill's
# tooltip lists its presentations under both Current Level and Next Level, so ranks 1-4 showed the quote
# twice. They go back to the end of the description instead, coloured by BL2's [incendiary] markup (orange):
# a raw <font> inside a description coloured the stat lines below it (2026-09-13), and the markup dictionary
# is read once at startup, so a tag of our own added at runtime printed as text (user, 2026-09-27).
# The tooltip box has a fixed height; Compound Interest's text-less presentation (TPS's accuracy-regen line
# with no description) printed an empty line in both rank sections, which pushed its Next Level line out
# of the box once the quote was back, so text-less presentations of these skills go too.
QUOTE_SKILLS = ("Optimism", "CompoundInterest", "SupplyAndDemand")
QUOTE_TAG = "incendiary"
QUOTE_COLOUR = "#c66613"                     # the quote presentations' prefix colour, how they are recognised
QUOTE_CHECK_SECONDS = 5.0
_quote_next = 0.0


def apply_skill_quotes(why: str) -> None:
    done, missing = [], []
    for leaf in QUOTE_SKILLS:
        d = skill_def(leaf)
        if d is None:
            missing.append(leaf)
            continue
        pres = [x for x in d.SkillEffectPresentations if x is not None]
        quotes = [x for x in pres if x.Attribute is None and QUOTE_COLOUR in str(x.Prefix)]
        blank = [x for x in pres if not str(x.Description).strip()]
        if not quotes and not blank:
            continue
        if quotes:
            text = str(d.SkillDescription)
            quote = str(quotes[0].Description)
            if quote not in text:
                d.SkillDescription = f"{text}<br><br>[{QUOTE_TAG}]{quote}[-{QUOTE_TAG}]"
        gone = {x.Name for x in quotes + blank}
        d.SkillEffectPresentations = [x for x in pres if x.Name not in gone]
        done.append(f"{leaf} (quote {len(quotes)}, blank {len(blank)})")
    if done or missing:
        unrealsdk.logging.info(f"{TAG} skill quotes ({why}): moved {done}" + (f"; not found {missing}" if missing else ""))


def _quote_tick() -> None:
    global _quote_next
    now = time.monotonic()
    if now < _quote_next:
        return
    _quote_next = now + QUOTE_CHECK_SECONDS
    apply_skill_quotes("check")


# --- Digi-Jack nova radius (the user's request, 2026-09-27) --------------------------------------------
# With You Have My Shield a clone wears a copy of the player's shield (share_shield). A nova shield's
# radius is its Att_ShieldNovaSize_<element>, a designer attribute held by the shield item, which the
# nova skill's Behavior_Explode reads. The copy's own value is multiplied here, so only the Digi-Jacks'
# novas grow; the player's shield keeps its radius.
NOVA_SIZE_ATTRS = tuple(f"GD_Shields.Misc.Att_ShieldNovaSize_{e}" for e in ("Corrosive", "Explosive", "Fire", "Shock"))
NOVA_CHOICES = ("1x", "2x", "3x", "4x", "5x")
_nova_option = None


def nova_factor() -> float:
    try:
        return float(str(_nova_option.value).rstrip("x")) if _nova_option is not None else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


# The size is a PreAdd effect of the shield definition on the item itself (slot Special02: 800 + 28 per
# grade for the explosive nova), and it is not in place yet when the copy has just been handed over: read
# then, it was 0 and nothing was scaled (2026-09-27). So the copy is scaled a moment later, from the tick,
# by raising its base value: base + the definition's PreAdd becomes factor x what it was.
NOVA_DELAY = 1.0
NOVA_TRIES = 5
_nova_pending: list = []        # [copy path, due (monotonic), tries]


def scale_copy_nova(copy: UObject) -> None:
    if nova_factor() == 1.0:
        return
    try:
        _nova_pending.append([copy._path_name(), time.monotonic() + NOVA_DELAY, 0])
    except Exception:  # noqa: BLE001
        pass


def _attr_base(path: str, ctx: UObject) -> float:
    a = _attr(path)
    try:
        r = a.GetBaseValue(ContextSource=ctx)
        return float(r[0] if isinstance(r, tuple) else r)
    except Exception:  # noqa: BLE001
        return 0.0


def _nova_tick() -> None:
    if not _nova_pending:
        return
    now = time.monotonic()
    factor = nova_factor()
    for entry in list(_nova_pending):
        path, due, tries = entry
        if now < due:
            continue
        copy = None
        try:
            copy = unrealsdk.find_object("WillowShield", path)
        except Exception:  # noqa: BLE001
            copy = None
        if copy is None or factor == 1.0:
            _nova_pending.remove(entry)
            continue
        scaled = []
        for attr_path in NOVA_SIZE_ATTRS:
            v = attr_value(attr_path, copy)
            if not v:
                continue
            base = _attr_base(attr_path, copy)
            if set_attr(attr_path, copy, base + (factor - 1.0) * v):
                after = attr_value(attr_path, copy)
                scaled.append(f"{attr_path.split('_')[-1]} {v:.0f} -> {after:.0f}" if after is not None else f"{attr_path.split('_')[-1]} {v:.0f} -> ?")
        if scaled:
            _nova_pending.remove(entry)
            _log_once("nova:scaled", f"Digi-Jack nova radius x{factor:g}: " + ", ".join(scaled), always=True)
        elif tries + 1 >= NOVA_TRIES:
            _nova_pending.remove(entry)
            _log_once("nova:none", f"Digi-Jack nova radius x{factor:g}: the shield copy has no nova size after "
                                   f"{NOVA_TRIES} tries (not a nova shield?)", always=True)
        else:
            entry[1], entry[2] = now + NOVA_DELAY, tries + 1


def _integrity_burst(pawn: UObject) -> None:
    """TPS's graph spawns its E-tech burst on the enemy after the bonus lands (Delay_171 ->
    SpawnParticleSystem_103, attached to the context, DrawScale 0.75); with the controller as the
    node's context here it lands nowhere, so the stand-in is spawned on the enemy directly, at most
    once every 0.4 s per enemy."""
    if not integrity_fx_on():
        return
    try:
        now = time.monotonic()
        if now - _integrity_burst_last.get(pawn.Name, 0.0) < 0.4:
            return
        _integrity_burst_last[pawn.Name] = now
        pc = _pc()
        ps = unrealsdk.find_object("ParticleSystem", INTEGRITY_BURST)
        loc = pawn.Location
        spawn = unrealsdk.make_struct("Vector", X=loc.X, Y=loc.Y, Z=loc.Z)
        rot = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0)
        comp = pc.WorldInfo.MyEmitterPool.SpawnEmitter(EmitterTemplate=ps, SpawnLocation=spawn, SpawnRotation=rot, AttachToActor=pawn)
        try:
            comp.SetScale(NewScale=0.75)
        except Exception:
            pass
        n = _fired.get("integrity:burst", 0) + 1
        _fired["integrity:burst"] = n
        if n <= 3:
            diag.info(f"{TAG} Integrity: burst on {pawn.Name}")
    except Exception:
        _log_once("integrity:burst", "Integrity burst failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.Behavior_Conditional:ApplyBehaviorToContext", Type.PRE)
def _on_integrity_conditional(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_integrity_conditional")
    """Integrity's 'enemy below 40% health' test. TPS's HealthStateExpressionEvaluator has
    bUsePercentage/HealthPercentage; BL2's only bDead/bInjured/bHealthy and, given the enemy's
    MIND as context, it says no whatever the flags (traced: bHealthy=True still fired nothing).
    So the conditional is decided here the way its own script does it: measure the enemy's
    health fraction and fire the node's output link 1 (its single condition's id) through the
    kernel when the enemy is low; the script is blocked either way."""
    global _integrity_logged
    if _early_nova and _early_nova_blocks(obj):
        return Block
    try:
        if obj.Name not in INTEGRITY_CONDITIONAL_NAMES:      # every AI graph's conditionals land here: the cheap test first
            return None
        if obj._path_name().replace(":", ".") not in INTEGRITY_CONDITIONALS:      # _path_name writes Outer:Sub.Sub
            return None
        ctx = args.ContextObject
        pawn = getattr(ctx, "Pawn", None) if ctx is not None and not hasattr(ctx, "GetHealth") else ctx
        if pawn is None:
            return None
        hp, mx = float(pawn.GetHealth()), float(pawn.GetMaxHealth())
        low = mx > 0 and hp / mx < INTEGRITY_HEALTH_FRACTION
        if low:
            _kernel().ActivateBehaviorOutputLink(KernelInfo=args.KernelInfo, OutputLinkId=1)
            # for the damage-number merge: bonus = hit x 0.08 x grade (x4 on the first hit, node 266)
            mult = INTEGRITY_DAMAGE_PER_LEVEL * grade("Integrity", _pc()) * (INTEGRITY_FIRST_HIT_MULT if obj.Name.endswith("266") else 1.0)
            _integrity_pending[pawn.Name] = {"t": time.monotonic(), "mult": mult, "expected": None}
            _integrity_burst(pawn)
    except Exception:
        _log_once("integrity:fail", "Integrity health test failed: " + traceback.format_exc().splitlines()[-1])
        return None
    if (low and _integrity_logged < 26) or _integrity_logged < 6:
        _integrity_logged += 1
        diag.info(f"{TAG} Integrity health test ({obj.Name[-3:]}) on {pawn.Name}: {hp:.0f}/{mx:.0f} -> "
                               f"{'BELOW 40%: output link fired' if low else 'above 40%'}")
    return Block


# --- Potential: the action skill while in Fight For Your Life -------------------------------------------------------
_action_skill_keys: set | None = None
_ffyl_casts_logged = 0
IE_PRESSED = 0
IE_RELEASED = 1

# --- Laser Surplus -----------------------------------------------------------------------------
# TPS: "Slam Augment. When you Slam, you shoot a Cone of Lasers in front of you." The skill's own
# graph is EMPTY (40-byte BPD): TPS's native slam activates LaserSurplus_Active, whose graph
# spawns 5 Projectile_SlamLaser in a +-5 degree cone from ChestFront (250 units out) and
# deactivates 0.1 s later. BL2 has no slam, so (user's call, 2026-09-15) the cone fires when a
# grenade is thrown instead: WillowPlayerController.ThrowGrenade(ProjectileDefinition) is script.
_laser_surplus_logged = 0
_laser_surplus_last = 0.0
_grenade_probe: dict = {}                # function -> calls logged (which of BL2's throw functions actually run)
_omm_spawning = False                    # our own On My Mark spawns must not count as the player's throw


# TPS's cone is invisible as imported: Projectile_SlamLaser.InFlightEffects (a TPS laser tracer)
# and Explosion_LaserSurplus.ExplosionPSTemplate (PS_LaserSurplus_Impact) came across as None,
# and the explode nodes' DamageSource is the TPS-only OzWillowDmgSource_Slam. BL2 stand-ins:
LS_PROJECTILE = "Quince_Doppel_FreeEnterprise.Projectiles.Projectile_SlamLaser"
LS_EXPLOSION = "Quince_Doppel_FreeEnterprise.Explosion.Explosion_LaserSurplus"
LS_EXPLODE_NODES = ("Quince_Doppel_FreeEnterprise.Projectiles.Projectile_SlamLaser.BehaviorProviderDefinition_0.Behavior_Explode_12",
                    "Quince_Doppel_FreeEnterprise.Projectiles.Projectile_SlamLaser.BehaviorProviderDefinition_0.Behavior_Explode_13")
LS_TRACER = "FX_WEP_Trails.Particles.Part_Wep_Tracer_Shock"
LS_IMPACT = "FX_WEP_Explosions.Particles.Incendiary.Part_Incendiary_Bullet_Burst"   # orange burst: the TPS flash's SubUV sheet drew as squares here
LS_DAMAGE_SOURCE = "WillowDmgSource_Skill"
LS_SPAWN_NODE = "Quince_Doppel_FreeEnterprise.Skills.LaserSurplus_Active.BehaviorProviderDefinition_0.Behavior_SpawnProjectile_22"
LS_CONE_OFFSET = [120.0, 0.0, -25.0]        # from the view point: forward, right, up (TPS: ChestFront socket + 250 forward); `doppelskill lasercone f r u`
_ls_fixed = False
_ls_census_at = 0.0
_ls_census2_at = 0.0

# The v60 package carries TPS's laser bolt / impact flash / Inspire screen particles with material
# instances CLONED from instances that have a static shader permutation (bHasStaticPermutationResource):
# their permutation map is keyed on the template's Id and is not in any BL2 cache for a TPS-authored
# template, so the emitters drew nothing (2026-09-15). Instances built here at runtime carry no
# permutation and render with the BL2 master's own shaders.
FX_MASTER_ADD = "FX_Shared_MasterShaders.Materials.Additive.Mat_FX_Unlit_Add_MS"
FX_MASTER_TRAN = "FX_Shared_MasterShaders.Materials.Translucent.Mat_Basic_FX_Unlit_Tran_MS"
FX_MASTER_SUBUV = "FX_Shared_MasterShaders.Materials.Additive.SubUV.Mat_FX_Unlit_Add_SubUV_MS"
FX_MASTER_ADD2 = "FX_Shared_MasterShaders.Materials.Additive.Mat_FX_Unlit_Add_2-Sided_MS"   # the only additive master compiled for beam/ribbon emitters (bUsedWithBeamTrails)
FX_MASTERS = {"add": FX_MASTER_ADD, "tran": FX_MASTER_TRAN, "subuv": FX_MASTER_SUBUV, "add2": FX_MASTER_ADD2}   # `doppelskill fxmaster <name> add|tran|subuv|add2`
FX_RUNTIME_MATERIALS = {
    # name: (master, {parameter: texture})
    "LaserTracer": (FX_MASTER_ADD, {"Emissive_GP": "Quince_FX.Textures.Laser_Default_Comp"}),
    # the SubUV master's sampler is `SubUV_Texture` (MaterialExpressionTextureSampleParameterSubUV, default StubWhite_Gray): an
    # `Emissive_GP` override on it is silently ignored and every particle draws the flat white stub (the bolt squares, 2026-09-15)
    "LaserImpact": ("FX_Shared_MasterShaders.Materials.Additive.SubUV.Mat_FX_Unlit_Add_SubUV_MS", {"SubUV_Texture": "FX_Shared_Energy.GreyPack.T_World_MuzzleFlashSubUV_GP_01"}),
    "Sparkle": (FX_MASTER_ADD, {"Emissive_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif"}),
    # Diffing the emitters against the Pre-Sequel's own Quince_Doppel_Streaming_SF showed these two
    # were the wrong material entirely, not the wrong master (2026-09-22).
    #
    # Rainbow_Trail took four goes. Two facts settle it. T_FX_Rainbow_D decodes to a plain vertical
    # gradient, no shape and no alpha: it is the colour ramp M_FX_Inspire_Rainbow samples, not a
    # sprite, so drawing it is a coloured rectangle however it is masked. And Rainbow_Trail is one
    # of only two emitters here declaring SubImages 2x2 with PSA_Square alignment, the other being
    # Star: on any master but the SubUV one the sprite comes out untextured, which is the white
    # squares (user, 2026-09-22), and the SubUV master is what fixed Star. So: the SubUV master over
    # a real sub-UV sheet, the muzzle-flash frames the laser impact already uses, and the rainbow
    # comes from p_color being swept through the hue wheel while the effect plays (_rainbow_tick),
    # which is the nearest a stock master gets to a per-particle ramp lookup.
    # and the shape is the Pre-Sequel's own: its shader takes the particle's alpha from
    # Snow_Splash_UV_Dif sampled at the sprite's UV (sampler s1, doubled and saturated), which is the
    # scattered pieces its trail is made of rather than the soft blobs the muzzle-flash frames gave.
    # Borderlands 2 has that texture, and the cube map the same shader samples for colour, so the
    # port will need no texture imported at all (user: "misty rather than the pieces", 2026-09-22).
    "Rainbow": (FX_MASTER_SUBUV, {"SubUV_Texture": "FX_Fast_Travel.Textures.Snow_Splash_UV_Dif"}),
    # Star is MI_FX_LuckyCharms there, over ButtStallionImpact, which decoded out of the package is
    # a 2x2 sheet: butterfly, heart, star, horseshoe. The Pre-Sequel draws it on the plain
    # translucent master and still gets one charm per particle, so its renderer hands the sprite its
    # sub-UVs; Borderlands 2's does not, and all four showed at once ("the texture is all wrong",
    # user, 2026-09-22). Frame selection here is the SubUV master's job. Borderlands 2 has the
    # texture in the Commander Lilith DLC every copy of the game ships with.
    "StarCharm": (FX_MASTER_SUBUV, {"SubUV_Texture": "FX_Anem_ButtStallion.Textures.ButtStallionImpact"}),
    "InspiredText": (FX_MASTER_TRAN, {"Emissive_GP": "Quince_FX.Textures.T_FX_Inspired", "Opc_GP": "Quince_FX.Textures.T_FX_Inspired"}),
    "DisillusionedText": (FX_MASTER_TRAN, {"Emissive_GP": "Quince_FX.Textures.T_FX_Disillusioned", "Opc_GP": "Quince_FX.Textures.T_FX_Disillusioned"}),
    "SadJack": (FX_MASTER_TRAN, {"Emissive_GP": "Quince_FX.Textures.T_FX_Disillusioned_SadJack_D", "Opc_GP": "Quince_FX.Textures.T_FX_Disillusioned_SadJack_M"}),
    "Tear": (FX_MASTER_TRAN, {"Emissive_GP": "FX_Shared_Energy.Textures.Tex_INC_Trail_M", "Opc_GP": "FX_Shared_Energy.Textures.Tex_INC_Trail_M"}),
    # v65: the Digi-Missile Barrage's comet (TPS Part_FinalBoss_MortarProjectile_Fire): ribbon tail on TPS's
    # ProjectileTrail streaks, flare/embers/burst on the round flare, crackle on the bolt sheet (greyscale composite)
    "MortarTrail": (FX_MASTER_ADD2, {"Emissive_GP": "Quince_FX.Textures.ProjectileTrail", "Opc_GP": "Quince_FX.Textures.ProjectileTrail"}),   # ribbons need a beam-trail master
    "MortarBolts": (FX_MASTER_SUBUV, {"SubUV_Texture": "Quince_FX.Textures.Bolts_Omni"}),
    "MortarFlare": (FX_MASTER_TRAN, {"Emissive_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif", "Opc_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif"}),
    "MortarEmbers": (FX_MASTER_TRAN, {"Emissive_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif", "Opc_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif"}),
    "MortarBurst": (FX_MASTER_TRAN, {"Emissive_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif", "Opc_GP": "FX_Shared_Lighting.Textures.Round_Flare_Sun_Dif"}),
    # On My Mark's painter beam: TPS draws it with FX_CREA_Constructor.Materials.Mat_ConstructorLaser, a BL2 material
    # (the Constructor's laser) that Borderlands 2 cooks only into Axton's package and some maps, so the port left the
    # beam emitter with no material and it drew nothing (user, 2026-09-29: "super buggy / nonexistent"). A beam needs
    # a master compiled for beams (add2); the texture is TPS's laser, as on the wrist bolts. The colour is the
    # emitter's own BeamColor (TPS: cyan-blue).
    "PainterBeam": (FX_MASTER_ADD2, {"Emissive_GP": "Quince_FX.Textures.Laser_Default_Comp", "Opc_GP": "Quince_FX.Textures.Laser_Default_Comp"}),
}
FX_PARTICLE_EMITTERS = {
    # particle system: {EmitterName: runtime material name}; emitters not listed keep what they have
    "FX_CO_Wep_Laser_140512.Meshes.Part_Laser_Blaster_3P": {"ThirdPersonShaft": "LaserTracer"},
    "Quince_FX.Particles.PS_LaserSurplus_Impact": {"MuzzleFlash": "LaserImpact"},
    "Quince_FX.Particles.Part_Inspire_Screen": {"Inspired_ENG": "InspiredText", "Rainbow_Trail": "Rainbow", "GeneratorBody": "Sparkle",
                                                "HorizontalSpark": "Sparkle", "VerticalSpark": "Sparkle", "RotatorSpark": "Sparkle", "Star": "StarCharm"},
    "Quince_FX.Particles.Part_Inspire_DisIllusioned_Screen": {"sad": "SadJack", "Disillusioned_ENG": "DisillusionedText", "Tears": "Tear"},
    # keys are EmitterName OR the emitter object's name (three of this system's emitters are unnamed)
    "Quince_FX.Particles.Part_BadassShot_Fire": {"ribbon_dark": "MortarTrail", "ParticleSpriteEmitter_11": "MortarFlare", "ParticleSpriteEmitter_12": "MortarEmbers",
                                                 "ParticleSpriteEmitter_2": "MortarBurst", "ParticleSpriteEmitter_3": "MortarBolts"},
    # v88: Potential's spawn nova; its two crackle sheets are TPS's Mat_EridianBolts_Omni_INST, the comet's bolt material
    "Quince_FX.Particles.PS_DigiJack_Potential": {"ParticleSpriteEmitter_7": "MortarBolts", "Flat": "MortarBolts"},
    "Quince_Doppel_Hero.Particles.Part_TargetPainterBeam": {"Beam": "PainterBeam"},
}
# The Inspire systems carry one text emitter per language TPS shipped (Inspired_ENG, _DEU, _ESN,
# _FRA, _ITA, _RUS). Only the English one came through the import with a material; the other five
# read `material=None` and `enabled=True` on the live objects (`doppelskill inspirediag`), and an
# emitter with no material draws the engine's default, a flat screen-aligned quad tinted by its own
# colour modules. Nothing here can give them the right word, since TPS's other-language text
# textures were never imported, so they are switched off instead.
def _mute_materialless_emitters() -> tuple:
    """Returns (disabled, already off, unreadable). It reported 0 on a run where inspirediag had
    just listed five live emitters with no material, so it says what it saw rather than nothing."""
    muted, already, bad = 0, 0, 0
    for path in FX_PARTICLE_EMITTERS:
        try:
            ps = unrealsdk.find_object("ParticleSystem", path)
        except Exception:  # noqa: BLE001
            continue
        for em in ps.Emitters:
            if em is None:
                continue
            for lod in (em.LODLevels or ()):
                try:
                    if lod is None or lod.RequiredModule is None or lod.RequiredModule.Material is not None:
                        continue
                    if not lod.bEnabled:
                        already += 1
                        continue
                    lod.bEnabled = False
                    muted += 1
                    diag.detail(f"{TAG} {path.split('.')[-1]}/{em.EmitterName}: no material, emitter disabled")
                except Exception:  # noqa: BLE001
                    bad += 1
                    _log_once("fx:mute:" + path, f"{path.split('.')[-1]}: emitter unreadable while muting: "
                              + traceback.format_exc().splitlines()[-1][:90])
    return muted, already, bad

# The word is supposed to appear as the rainbow sweeps over it, and it appears at once. The emitter
# data is not the reason: `Inspired_ENG` carries EmitterDelay 0 in the Pre-Sequel too, the same as
# ours, so the reveal lives in `M_FX_Inspired`'s shader graph, which samples the rainbow ramp the
# same way `M_FX_Inspire_Rainbow` does (`T_FX_Rainbow_D` decodes to a plain gradient with no shape
# in it, so it is a colour lookup, not a sprite). Nothing built on a stock master can wipe the word
# in, so the emitter is simply held back until the wave has crossed it: Rainbow_Trail starts at 0.1
# and runs 2.2, so a little under two seconds puts the word under the middle of the sweep.
# `doppelskill inspiredelay <seconds>` retunes it live (user, 2026-09-22).
INSPIRE_TEXT_DELAY = 1.8
INSPIRE_TEXT_EMITTERS = ("Inspired_ENG", "Disillusioned_ENG")


def _delay_inspire_text() -> str:
    done = []
    for path in ("Quince_FX.Particles.Part_Inspire_Screen", "Quince_FX.Particles.Part_Inspire_DisIllusioned_Screen"):
        try:
            ps = unrealsdk.find_object("ParticleSystem", path)
        except Exception:  # noqa: BLE001
            continue
        for em in ps.Emitters:
            if em is None or str(em.EmitterName) not in INSPIRE_TEXT_EMITTERS:
                continue
            for lod in (em.LODLevels or ()):
                try:
                    req = lod.RequiredModule
                    if req is None or abs(float(req.EmitterDelay) - INSPIRE_TEXT_DELAY) < 0.01:
                        continue
                    req.EmitterDelay = INSPIRE_TEXT_DELAY
                    done.append(str(em.EmitterName))
                except Exception:  # noqa: BLE001
                    continue
    return f"{len(done)} text emitter(s) held back {INSPIRE_TEXT_DELAY:.2f}s" if done else ""

# The rainbow, without the shader that samples the ramp per particle: the trail's material colour is
# swept through the hue wheel for as long as the effect is on screen. Every live particle shares the
# colour at any instant, so it reads as a sweep through the spectrum rather than a spread across it,
# which is the half of M_FX_Inspire_Rainbow a stock master can reach.
RAINBOW_SWEEP_SECONDS = 2.4
_rainbow_until = 0.0


def start_rainbow_sweep() -> None:
    global _rainbow_until
    if RAINBOW_SWEEP_SECONDS <= 0:
        return
    _rainbow_until = time.monotonic() + RAINBOW_SWEEP_SECONDS


def _hue(t: float) -> tuple:
    """t in 0..1 around the wheel, full value and saturation."""
    import colorsys
    return colorsys.hsv_to_rgb(t % 1.0, 1.0, 1.0)


def _rainbow_tick() -> None:
    if not _rainbow_until:
        return
    now = time.monotonic()
    path = _fx_materials.get("Rainbow")
    if not path:
        return
    if now >= _rainbow_until:
        _reset_rainbow(path)
        return
    t = 1.0 - (_rainbow_until - now) / RAINBOW_SWEEP_SECONDS
    r, g, b = _hue(t)
    boost = FX_COLOR_BOOST.get("Rainbow", 1.0)
    try:
        mic = unrealsdk.find_object("MaterialInstanceConstant", path)
        mic.SetVectorParameterValue(ParameterName="p_color",
                                    Value=unrealsdk.make_struct("LinearColor", R=r * boost, G=g * boost, B=b * boost, A=1.0))
    except Exception:  # noqa: BLE001
        _log_once("fx:rainbow", "rainbow sweep failed: " + traceback.format_exc().splitlines()[-1][:100])
        _rainbow_reset_now()


def _reset_rainbow(path: str) -> None:
    global _rainbow_until
    _rainbow_until = 0.0
    try:
        mic = unrealsdk.find_object("MaterialInstanceConstant", path)
        mic.SetVectorParameterValue(ParameterName="p_color",
                                    Value=unrealsdk.make_struct("LinearColor", R=1.0, G=1.0, B=1.0, A=1.0))
    except Exception:  # noqa: BLE001
        pass


def _rainbow_reset_now() -> None:
    global _rainbow_until
    _rainbow_until = 0.0

# Materials whose own shader we ship in the package's SeekFreeShaderCache (inspire_shaders.py, built
# by digijack_shadercache.py). These are the Pre-Sequel's real materials, not a stock master with a
# texture hung off it, so the emitter points straight at them and the runtime instance is not built
# at all. Falls back with a logged line if the package did not come across.
PORTED_DEFAULT = {"Rainbow": "Quince_FX.Materials.M_FX_Inspire_Rainbow"}
PORTED_MATERIALS = dict(PORTED_DEFAULT)
_PORTED_OFF = False
FX_PACKAGE_MATERIALS = {"SadJack": "MI_FX_SadJackBL2"}      # v60 clone names that differ from the runtime names
FX_MATERIAL_MODE = "runtime"                     # "runtime" (built here) or "package" (the v60 clones); `doppelskill fxmat x`
FX_COLOR_BOOST = {"LaserTracer": 2.5, "LaserImpact": 1.5,
                  # The Pre-Sequel's Inspire shader doubles the mask before it becomes alpha
                  # (`add_sat r0.x, r0.x, r0.x`), so each fragment covers more of its sprite than
                  # the same texture does on a linear master; x2 is that doubling, and the reason
                  # the pieces read as small next to the Pre-Sequel's (`doppelskill fxboost Rainbow x`)
                  "Rainbow": 2.0,   # p_color multiplier: TPS's laser core ran at CoreStrength 4 over a 0..1 texture; `doppelskill laserboost x`
                  # The comet's emitters drive HDR colours (ColorScaleOverLife x10 on the flare, x3..10 on the bolts, x1..5 on the
                  # ribbon; the 128-unit spawn burst has alpha 0 for its whole life) through TPS materials with their own falloff;
                  # on BL2's linear masters a soft flare at x10 saturates into a flat square, so each is scaled back here. `doppelskill fxboost <name> x`
                  "MortarEmbers": 4.0}   # embers: TPS's Mat_GlowingEmbers brightened its (1, 0.1, 0.01) into a clipped orange; the comet's other curves are scaled in the modules (COMET_TONE)
COMET_TONE = (
    # (module, distribution property, factor, only when the table's max is at least): TPS curves -> BL2 masters.
    # The colours are MEANT to clip: (1, 0.1, 0.01) x10 lands at (1, 1, 0.1); a yellow-white core fading through
    # orange to red; which is the TPS look; scaled down to 0.15 it stayed (1.5, 0.15, ...) = plain red (2026-09-15).
    # The flare keeps its full x10 (its shape now comes from opacity, so clipping cannot square it); the additive
    # ribbon and bolts are trimmed just enough not to burn into bars.
    ("ParticleModuleColorScaleOverLife", "ParticleModuleColorScaleOverLife_3", "ColorScaleOverLife", 0.7, 4.0),    # ribbon x1..5 -> x0.7..3.5
    ("ParticleModuleColorScaleOverLife", "ParticleModuleColorScaleOverLife_4", "ColorScaleOverLife", 0.5, 5.0),    # bolts x3..10 -> x1.5..5
)
COMET_DISABLED = ("ParticleSpriteEmitter_2",)          # the 128-unit spawn burst: alpha 0 for its whole life in TPS (never visible there)
_fx_materials: dict[str, str] = {}               # name -> object path of the instance built this session
_fx_serial = 0
_fx_applied = False


def _fx_runtime_material(name: str) -> UObject | None:
    path = _fx_materials.get(name)
    if path:
        try:
            return unrealsdk.find_object("MaterialInstanceConstant", path)
        except Exception:
            _fx_materials.pop(name, None)
    master_path, params = FX_RUNTIME_MATERIALS[name]
    master = unrealsdk.find_object("Material", master_path)
    # outered to the engine's Transient package like any runtime object; NOT to the streaming
    # package's group: after a save-quit that group object can be the old, dying copy while the
    # package reloads, and objects constructed inside it crash the game a second later (2026-09-15)
    outer = unrealsdk.find_object("Package", "Transient")
    global _fx_serial
    _fx_serial += 1
    mic = unrealsdk.construct_object("MaterialInstanceConstant", outer, f"MI_FX_Runtime_{name}_{_fx_serial}")
    mic.SetParent(NewParent=master)
    for pname, tpath in params.items():
        if master_path == FX_MASTER_SUBUV and pname == "Emissive_GP":
            pname = "SubUV_Texture"          # the SubUV master's only sampler (`doppelskill fxmaster x subuv`)
        elif master_path != FX_MASTER_SUBUV and pname == "SubUV_Texture":
            pname = "Emissive_GP"
        tex = unrealsdk.find_object("Texture2D", tpath)
        try:
            # Nothing registers a runtime instance's textures with the texture streamer, so a streamed
            # BL2 texture (Round_Flare_Sun_Dif) stays at its tiny in-package mip: every sprite sampled
            # one average colour and drew as a flat square (the barrage comet, 2026-09-15; the same
            # thing left the clones' hole noise at 64x64). Forced resident, the streamer loads all mips.
            if not tex.bForceMiplevelsToBeResident:
                tex.bForceMiplevelsToBeResident = True
        except Exception:
            _log_once("fx:mips:" + tpath, f"could not force mips resident on {tpath}: " + traceback.format_exc().splitlines()[-1])
        mic.SetTextureParameterValue(ParameterName=pname, Value=tex)
    boost = FX_COLOR_BOOST.get(name)
    if boost is not None:
        try:
            mic.SetVectorParameterValue(ParameterName="p_color", Value=unrealsdk.make_struct("LinearColor", R=boost, G=boost, B=boost, A=1.0))
        except Exception:
            _log_once("fx:boost:" + name, f"p_color on {name} failed: " + traceback.format_exc().splitlines()[-1])
    _fx_materials[name] = mic._path_name()
    return mic


def _fx_apply_materials() -> str:
    """Point the imported particles' emitters at the runtime instances (or back at the v60 clones),
    walking each loaded particle system's emitters by property (module paths were not resolvable)."""
    global _fx_applied
    done, failed, missing = [], [], []
    for ps_path, emitters in FX_PARTICLE_EMITTERS.items():
        try:
            ps = unrealsdk.find_object("ParticleSystem", ps_path)
        except Exception:
            missing.append(ps_path.split(".")[-1])
            continue
        try:
            for em in ps.Emitters:
                if em is None:
                    continue
                name = emitters.get(str(em.EmitterName))
                if name is None:
                    name = emitters.get(str(em.Name))
                if name is None:
                    continue
                for lod in em.LODLevels:
                    req = lod.RequiredModule if lod is not None else None
                    if req is None:
                        continue
                    try:
                        ported = PORTED_MATERIALS.get(name)
                        if ported is not None:
                            try:
                                mat = unrealsdk.find_object("Material", ported)
                            except Exception:  # noqa: BLE001  not resident: fall back, do not throw
                                mat = None
                            # always logged: which of the two a ported material took is the first
                            # thing to know when it renders wrong, and the players who hit it have
                            # diagnostics off (2026-09-22)
                            _log_once("fx:ported:" + name,
                                      f"{name}: {ported} " + ("in use" if mat is not None
                                      else "NOT RESIDENT, falling back to the stock master"), always=True)
                            if mat is not None:
                                req.Material = mat
                                done.append(f"{ps_path.split('.')[-1]}/{em.EmitterName}>{name} (ported)")
                                continue
                        if FX_MATERIAL_MODE == "runtime":
                            req.Material = _fx_runtime_material(name)
                        else:
                            req.Material = unrealsdk.find_object("MaterialInstanceConstant", "Quince_FX.Materials." + FX_PACKAGE_MATERIALS.get(name, "MI_FX_" + name))
                        done.append(f"{ps_path.split('.')[-1]}/{em.EmitterName}>{name}")
                    except Exception:
                        why = traceback.format_exc().splitlines()[-1][:140]
                        failed.append(f"{ps_path.split('.')[-1]}/{em.Name}>{name}: " + why)
                        # always logged: a swallowed failure here leaves the emitter on whatever the
                        # package baked in, which is how the ported rainbow came out as the old
                        # instance with no shader map, drawing black (2026-09-22)
                        _log_once(f"fx:fail:{ps_path}:{name}",
                                  f"{ps_path.split('.')[-1]}/{em.EmitterName}>{name} failed: {why}", always=True)
        except Exception:
            why = traceback.format_exc().splitlines()[-1][:120]
            failed.append(ps_path.split(".")[-1] + ": " + why)
            _log_once("fx:sysfail:" + ps_path, f"{ps_path.split('.')[-1]}: {why}", always=True)
    _fx_applied = not failed and not missing
    muted, already, bad = _mute_materialless_emitters()
    held = _delay_inspire_text()
    return (f"FX materials ({FX_MATERIAL_MODE}): set {done}; not loaded {missing}; failures {failed}"
            + f"; no material: {muted} disabled, {already} already off, {bad} unreadable"
            + ("; " + held if held else ""))


INTEGRITY_BURST_NODE = "Quince_Doppel_GreaterGood.Skills.Integrity.BehaviorProviderDefinition_0.Behavior_SpawnParticleSystem_103"
INTEGRITY_BURST = "FX_WEP_Explosions.Particles.Eridan.Part_EridianExplosionSmall"  # BL2's own, copied in as a forced export (v69: map-cooked only otherwise)


# Both of these ship with TrackedSkillHUDSlot unset, so the buff tray drew them in the same place
# and Jack's Cache covered Sponsored By (player, 2026-09-22). Jack's other tracked skills hold 1..10
# and Gaige's Typecast Iconoclast proves 11 renders, so Jack's Cache moves there.
HUD_SLOTS = {"Quince_Doppel_Hero.Skills.JacksCache": 11, "GD_Doppelganger_Skills.Hero.JacksCache": 11,
             # every other tracked skill keeps its feedback half on the same slot, and the feedback
             # half is the one the tray draws: moving only the parent left the two still overlapping
             # (player, 2026-09-22)
             "Quince_Doppel_Hero.Skills.JacksCache_Feedback": 11,
             "GD_Doppelganger_Skills.Hero.JacksCache_Feedback": 11}


def _hud_slot_fixups() -> list:
    notes = []
    for path, slot in HUD_SLOTS.items():
        try:
            d = unrealsdk.find_object("SkillDefinition", path)
        except ValueError:
            continue
        try:
            if int(d.TrackedSkillHUDSlot) != slot:
                d.TrackedSkillHUDSlot = slot
                notes.append(f"{path.split('.')[-1]} -> HUD slot {slot}")
        except Exception:  # noqa: BLE001
            pass
    return notes


def _laser_surplus_fixups() -> None:
    """Point the imported slam-laser assets at BL2 particles / a BL2 damage source (once)."""
    global _ls_fixed
    if _ls_fixed:
        return
    notes = _hud_slot_fixups()
    try:                                     # Integrity's burst on the enemy (audit 2026-09-15): a BL2 stand-in for the E-tech burst
        r = apply_integrity_fx()
        if r not in ("unchanged", "not resident"):
            notes.append(r)
    except Exception as e:
        notes.append(f"Integrity burst failed: {str(e)[:80]}")
    try:
        pdef = unrealsdk.find_object("ProjectileDefinition", LS_PROJECTILE)
        if pdef.InFlightEffects is None:
            pdef.InFlightEffects = unrealsdk.find_object("ParticleSystem", LS_TRACER)
            notes.append("tracer")
    except Exception as e:
        notes.append(f"tracer failed: {str(e)[:80]}")
    try:
        ex = unrealsdk.find_object("ExplosionDefinition", LS_EXPLOSION)
        scales = ex.ExplosionScale
        if len(scales):
            scales[0].ExplosionPSTemplate = unrealsdk.find_object("ParticleSystem", LS_IMPACT)
            notes.append("impact particle")
    except Exception as e:
        notes.append(f"impact failed: {str(e)[:80]}")
    try:                                     # in first person the body's ChestFront socket sits low and to the side of the camera
        node = unrealsdk.find_object("Behavior_SpawnProjectile", LS_SPAWN_NODE)
        node.bSpawnFromContextViewLocation = True
        node.RelativeLocation = unrealsdk.make_struct("Vector", X=LS_CONE_OFFSET[0], Y=LS_CONE_OFFSET[1], Z=LS_CONE_OFFSET[2])
        notes.append("cone from the view point")
    except Exception as e:
        notes.append(f"cone origin failed: {str(e)[:80]}")
    try:
        cls = unrealsdk.find_class(LS_DAMAGE_SOURCE)
        for path in LS_EXPLODE_NODES:
            node = unrealsdk.find_object("Behavior_Explode", path)
            if node.DamageSource is None:
                node.DamageSource = cls
                notes.append(node.Name + " damage source")
    except Exception as e:
        notes.append(f"damage source failed: {str(e)[:80]}")
    _ls_fixed = True
    diag.info(f"{TAG} Laser Surplus fix-ups: {notes}")


_fx_next_try = 0.0


def _fx_materials_tick() -> None:
    """Until every imported particle system has its runtime materials: retry every 2 s."""
    global _fx_next_try
    if _fx_applied or FX_MATERIAL_MODE != "runtime":
        return
    now = time.monotonic()
    if now < _fx_next_try:
        return
    _fx_next_try = now + 2.0
    how = _fx_apply_materials()
    key = "fx:" + str(_fx_applied)
    if key not in _fired:
        _fired[key] = 1
        diag.info(f"{TAG} {how}")


def _laser_surplus_fire(pc: UObject, why: str) -> None:
    global _laser_surplus_logged, _laser_surplus_last, _ls_census_at
    try:
        if not _ours(pc):
            return
        if grade("LaserSurplus", pc) <= 0:
            _log_once("ls:grade", f"Laser Surplus: {why} but the skill has no points")
            return
        now = time.monotonic()
        if now - _laser_surplus_last < 0.5:
            return
        if active_skill("LaserSurplus_Active", pc) is not None:
            _log_once("ls:active", "Laser Surplus: LaserSurplus_Active still active when the grenade left")
            return
        d = skill_def("LaserSurplus_Active")
        if d is None:
            from . import dlcbundle
            from .characterslot import _STREAMING_PACKAGE_NAME
            try:
                dlcbundle.load_package(_STREAMING_PACKAGE_NAME)
                rooting.after_fallback_load("Laser Surplus")
            except Exception as e:
                _log_once("ls:load", f"Laser Surplus: load_package({_STREAMING_PACKAGE_NAME}) failed: {str(e)[:80]}")
            d = skill_def("LaserSurplus_Active")
        if d is None:
            _log_once("ls:def", "Laser Surplus: LaserSurplus_Active not resident even after loading our packages")
            return
        _laser_surplus_fixups()
        for name in ("LaserTracer", "LaserImpact"):
            _fx_materials.pop(name, None)
        how = _fx_apply_materials()
        if "failures []" not in how:
            _log_once("ls:mats", "Laser Surplus materials: " + how[:300])
        _laser_surplus_last = now
        _ls_census_at = now + 0.25
        ok = pc.GetSkillManager().ActivateSkill(Definition=d, SkillInstigator=pc, SkillGrade=1)
        if _laser_surplus_logged < 6:
            _laser_surplus_logged += 1
            diag.info(f"{TAG} Laser Surplus: {why} -> cone of lasers (ActivateSkill {ok})")
    except Exception:
        _log_once("lasersurplus", "Laser Surplus failed: " + traceback.format_exc().splitlines()[-1])


def _laser_surplus_census() -> None:
    """0.25 s and 1.0 s after activation: did the five slam lasers spawn, are they moving?"""
    global _ls_census_at, _ls_census2_at
    now = time.monotonic()
    if _ls_census_at and now >= _ls_census_at:
        _ls_census_at = 0.0
        _ls_census2_at = now + 0.75
        label = ""
    elif _ls_census2_at and now >= _ls_census2_at:
        _ls_census2_at = 0.0
        label = " (+1.0s)"
    else:
        return
    try:
        pc = _pc()
        pl = pc.Pawn.Location if pc is not None and pc.Pawn is not None else None
        found = []
        for pr in unrealsdk.find_all("WillowProjectile", exact=False):
            try:
                d = pr.Definition
                if d is not None and d.Name == "Projectile_SlamLaser" and not pr.bDeleteMe:
                    dist = math.sqrt((pr.Location.X - pl.X) ** 2 + (pr.Location.Y - pl.Y) ** 2 + (pr.Location.Z - pl.Z) ** 2) if pl is not None else -1
                    found.append(f"{pr.Name} phys {pr.Physics} {dist:.0f}u away vel {_v(pr.Velocity)}")
            except Exception:
                continue
        diag.info(f"{TAG} Laser Surplus census{label}: {len(found)} slam lasers alive: {found[:5]}")
    except Exception:
        _log_once("ls:census", "Laser Surplus census failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.Behavior_SpawnProjectile:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_ls_spawnnode")
def _on_ls_spawn_node(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_ls_spawn_node")
    try:
        if "LaserSurplus" not in obj.Outer._path_name():
            return
        n = _grenade_probe.get("ls-node", 0)
        if n < 4:
            _grenade_probe["ls-node"] = n + 1
            diag.info(f"{TAG} Laser Surplus: {obj.Name} on context {args.ContextObject} sockets {list(obj.FireLocationSocketNames)} projectile {obj.ProjectileDefinition.Name if obj.ProjectileDefinition is not None else None}")
    except Exception:
        pass


def _grenade_probe_log(name: str, obj, args) -> None:
    n = _grenade_probe.get(name, 0)
    if n >= 3:
        return
    _grenade_probe[name] = n + 1
    try:
        arg_txt = ", ".join(f"{k}={getattr(args, k)}" for k in ("ProjectileDefinition", "ProjectileDef", "SpawnLocation") if hasattr(args, k))
    except Exception:
        arg_txt = "?"
    diag.info(f"{TAG} grenade probe: {name} on {obj.Name} ({arg_txt})")


@hook("WillowGame.WillowPlayerController:StartThrowGrenadeSkill", Type.POST, hook_identifier="doppel_ls_startthrow")
def _on_start_throw_grenade_skill(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_start_throw_grenade_skill")
    _grenade_probe_log("StartThrowGrenadeSkill", obj, args)


@hook("WillowGame.WillowPlayerController:GrenadeThrowComplete", Type.POST, hook_identifier="doppel_ls_throwdone")
def _on_grenade_throw_complete(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_grenade_throw_complete")
    _grenade_probe_log("GrenadeThrowComplete", obj, args)


@hook("WillowGame.WillowPlayerController:ThrowGrenade", Type.PRE, hook_identifier="doppel_ls_throw")
def _on_throw_grenade(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_throw_grenade")
    """Once per real throw. ThrowGrenade runs on every press of the key; during a throw already
    in progress too (bThrowingGrenade set), and with an empty pouch; so both are gated out.
    Covers the basic grenade (no WillowGrenadeMod involved) and modded ones alike."""
    try:
        if not _ours(obj):
            return
        if bool(obj.bThrowingGrenade):
            return                                   # spam during the throw animation: the engine ignores it too
        pdef = args.ProjectileDefinition
        try:
            affordable = bool(obj.CanAffordGrenadeCost(ProjectileDefinition=pdef))
        except Exception:
            affordable = False
        if affordable:
            _laser_surplus_fire(obj, "grenade throw")
        else:
            _log_once("ls:nopouch", "Laser Surplus: ThrowGrenade with nothing to throw -> no cone")
    except Exception:
        _log_once("ls:throwhook", "Laser Surplus throw hook failed: " + traceback.format_exc().splitlines()[-1])


@hook("WillowGame.WillowGrenadeMod:SpawnProjectile", Type.POST, hook_identifier="doppel_ls_spawn")
def _on_grenade_mod_spawn(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_grenade_mod_spawn")
    """The grenade mod spawning its projectile: the player's throw, or ours (On My Mark)."""
    if diag.enabled:
        _grenade_probe_log("WillowGrenadeMod.SpawnProjectile" + (" [ours]" if _omm_spawning else ""), obj, args)
    try:
        proj = ret
        if diag.enabled and proj is not None and _grenade_probe.get("spawn-detail", 0) < 3:
            _grenade_probe["spawn-detail"] = _grenade_probe.get("spawn-detail", 0) + 1
            diag.info(f"{TAG} grenade probe: spawned {proj.Class.Name} {proj.Name} Definition={getattr(proj, 'Definition', '?')} vel {_v(proj.Velocity)} Owner={proj.Owner.Name if proj.Owner is not None else None} Instigator={proj.Instigator.Name if proj.Instigator is not None else None}")
    except Exception:
        pass
    if _omm_spawning:
        return
    try:
        if ret is not None:
            _track_projectile(ret, "player grenade")
    except Exception:
        pass


# --- On My Mark --------------------------------------------------------------------------------
# TPS: pressing the action-skill key during Expendable Assets makes the Digi-Jacks throw a copy of
# the player's grenade at the enemy under the crosshair, once per activation. Both of its graphs
# are empty: TPS's native action skill raised ThrowGrenade on the clones' AI graph, whose branch
# ends in OzBehavior_GiveEquipmentToAI (TPS-only). Here: the key press while the skill runs spawns
# the grenade mod's projectile from each live clone, aimed at the crosshair target.
ON_MY_MARK_AK = {"lock": "Ake_Quince_FX_Player_Jack.Skills.Ak_Play_Dlc_Quince_FX_Jack_OnMyMark_Lock",
                 "trace": "Ake_Quince_FX_Player_Jack.Skills.Ak_Play_Dlc_Quince_FX_Jack_OnMyMark_Trace",
                 "trace_stop": "Ake_Quince_FX_Player_Jack.Skills.Ak_Stop_Dlc_Quince_FX_Jack_OnMyMark_Trace"}
# TPS's summon graph marks the target: PS_LaserGuided_Target (BL2's explosion flash + Zer0's dash flash, v68)
# on the marked enemy with the trace sound, destroyed after the throw (Behavior_Delay_459 -> Destroy_13 -> stop)
ON_MY_MARK_MARKER = "Quince_FX.Particles.PS_LaserGuided_Target"
ON_MY_MARK_MARKER_SECONDS = 1.5
_omm_marker_path: str | None = None
_omm_marker_until = 0.0
# TPS also paints the mark with a beam from Jack (user, 2026-09-29: missing): its summon graph's Behavior_FireBeam_13,
# FireMode_TargetPainter (EWWFT_Beam, Part_TargetPainterBeam, lifetime 1 s) from the SnapZoom socket. That graph is
# TPS's native action skill, which never runs here, so the beam is the particle itself: its beam modules read the
# SourceLocation / TargetLocation particle parameters (and the tangents), set from Jack's gun to the mark every tick.
ON_MY_MARK_BEAM = "Quince_Doppel_Hero.Particles.Part_TargetPainterBeam"
ON_MY_MARK_BEAM_SECONDS = 1.0
# The width is the emitter's BeamSize parameter (ParticleModuleSize_0, DPM_Normal onto MaxOutput (10, 3, 3)). Left
# unset it falls back to its Constant, a 2-unit line that vanishes a few metres out (still no beam once it had a
# material, user 2026-09-29); TPS's beam code sets it, so it is set here: 1.0 is the full 10 units.
ON_MY_MARK_BEAM_SIZE = 1.0
# TPS's beam comes up from the bottom middle of the screen and reads a good deal thicker than the 10 units the
# parameter can reach (user's TPS screenshot, 2026-09-29), so the width is the size curve's MaxOutput, widened at
# runtime, and the start is a point under the camera: forward, right, up in camera space. `doppelskill beamwidth w`,
# `doppelskill beamfrom f r u`.
ON_MY_MARK_BEAM_WIDTH = 30.0                  # user-tuned 2026-09-29 (with the start at 10, 0, -40)
ON_MY_MARK_BEAM_FROM = [10.0, 0.0, -40.0]
ON_MY_MARK_BEAM_SIZE_DIST = ON_MY_MARK_BEAM + ".ParticleModuleSize_0.DistributionVectorParticleParameter_2"
_omm_beam: dict = {}              # {"comp": path, "target": pawn path or None, "aim": (x, y, z), "until": t}
_omm_beam_reports = 0             # the first few beams report where they really are (their bounds) to the log


def _is_local_pc(pc) -> bool:
    try:
        return bool(pc.IsLocalPlayerController())
    except Exception:  # noqa: BLE001
        return True


def remote_mark(aim) -> None:
    """A joined Jack's copy: the host says his On My Mark went off at `aim`. The enemy nearest that point, then the
    lock sound, the marker and the painter beam, as the host's player gets them."""
    pc = _pc()
    if pc is None or pc.Pawn is None:
        return
    target, best = None, 250.0
    for pawn in _live_pawns(pc):
        try:
            if bool(pawn.bDeleteMe) or "PlayerPawn" in pawn.Class.Name:
                continue
            loc = pawn.Location
            d = math.dist((loc.X, loc.Y, loc.Z + 30.0), aim)
            if d < best:
                target, best = pawn, d
        except Exception:  # noqa: BLE001
            continue
    _play_ak(pc.Pawn, ON_MY_MARK_AK["lock"])
    _omm_show_marker(pc, target, aim)
    try:
        sk = active_skill("OnMyMark_Feedback", pc)
        if sk is not None:
            sk.Deactivate()
    except Exception:  # noqa: BLE001
        pass


def _omm_show_marker(pc: UObject, target, aim, beam: bool = True) -> None:
    """The target marker on the marked enemy (or at the aim point), plus the trace sound."""
    global _omm_marker_path, _omm_marker_until
    try:
        ps = unrealsdk.find_object("ParticleSystem", ON_MY_MARK_MARKER)
        pool = pc.WorldInfo.MyEmitterPool
        loc = unrealsdk.make_struct("Vector", X=aim[0], Y=aim[1], Z=aim[2])
        rot = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0)
        comp = pool.SpawnEmitter(EmitterTemplate=ps, SpawnLocation=loc, SpawnRotation=rot, AttachToActor=target)
        _omm_marker_path = comp._path_name() if comp is not None else None
        _omm_marker_until = time.monotonic() + ON_MY_MARK_MARKER_SECONDS
        _play_ak(pc.Pawn, ON_MY_MARK_AK["trace"])
    except Exception:
        _log_once("omm:marker", "On My Mark marker failed: " + traceback.format_exc().splitlines()[-1])
    if beam:
        _omm_start_beam(pc, target, aim)


def _omm_beam_ends(pc: UObject):
    """(source, target) of the painter beam: a point under the camera, and the mark.

    The gun's GetMuzzleLoc came back behind the eye (the hidden third-person weapon): the beam ran straight down the
    line of sight and showed only while strafing. From the right wrist it read as thin and off to the side next to
    TPS, where it rises from the bottom middle of the screen (user, 2026-09-29)."""
    pawn = pc.Pawn
    eye, r = pawn.GetPawnViewLocation(), pc.Rotation
    lead = MERGER_LEAD * _last_dt                # led by the pawn's motion, as the wrist beams are
    try:
        v = pawn.Velocity
        eye_at = (eye.X + v.X * lead, eye.Y + v.Y * lead, eye.Z + v.Z * lead)
    except Exception:  # noqa: BLE001
        eye_at = (eye.X, eye.Y, eye.Z)
    pitch, yaw = r.Pitch * math.pi / 32768.0, r.Yaw * math.pi / 32768.0
    F = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    R = (-math.sin(yaw), math.cos(yaw), 0.0)
    U = (-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch))
    x, y, z = ON_MY_MARK_BEAM_FROM
    src = tuple(e + x * f + y * rr + z * u for e, f, rr, u in zip(eye_at, F, R, U))
    dst = _omm_beam["aim"]
    target = _omm_beam.get("target")
    if target:
        try:
            t = unrealsdk.find_object("Pawn", target)
            if t is not None and t.GetHealth() > 0:
                dst = (t.Location.X, t.Location.Y, t.Location.Z + 30.0)
        except Exception:  # noqa: BLE001
            pass
    return src, dst


def _omm_set_beam(comp: UObject, src, dst) -> None:
    d = _norm((dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]))
    vec = lambda v: unrealsdk.make_struct("Vector", X=float(v[0]), Y=float(v[1]), Z=float(v[2]))  # noqa: E731
    comp.SetVectorParameter(ParameterName="SourceLocation", Param=vec(src))
    comp.SetVectorParameter(ParameterName="TargetLocation", Param=vec(dst))
    comp.SetVectorParameter(ParameterName="SourceTangent", Param=vec(d))
    comp.SetVectorParameter(ParameterName="TargetTangent", Param=vec(d))
    comp.SetVectorParameter(ParameterName="BeamSize", Param=vec((ON_MY_MARK_BEAM_SIZE,) * 3))
    comp.SetFloatParameter(ParameterName="SourceStrength", Param=0.0)
    comp.SetFloatParameter(ParameterName="TargetStrength", Param=0.0)


def _omm_beam_width() -> None:
    """The beam's width is its size curve's output for our BeamSize of 1.0: set that output to the width we want."""
    try:
        dist = unrealsdk.find_object("DistributionVectorParticleParameter", ON_MY_MARK_BEAM_SIZE_DIST)
        w = float(ON_MY_MARK_BEAM_WIDTH)
        cur = dist.MaxOutput
        if (cur.X, cur.Y, cur.Z) != (w, w, w):
            dist.MaxOutput = unrealsdk.make_struct("Vector", X=w, Y=w, Z=w)
    except Exception:  # noqa: BLE001
        _log_once("omm:width", "On My Mark beam width not set: " + traceback.format_exc().splitlines()[-1])


def _omm_start_beam(pc: UObject, target, aim, seconds: float = ON_MY_MARK_BEAM_SECONDS) -> None:
    _omm_beam_width()
    try:
        ps = unrealsdk.find_object("ParticleSystem", ON_MY_MARK_BEAM)
        _omm_beam.clear()
        _omm_beam.update(target=target._path_name() if target is not None else None, aim=tuple(aim),
                         until=time.monotonic() + seconds, report_at=time.monotonic() + 0.2)
        src, dst = _omm_beam_ends(pc)
        loc = unrealsdk.make_struct("Vector", X=src[0], Y=src[1], Z=src[2])
        comp = pc.WorldInfo.MyEmitterPool.SpawnEmitter(EmitterTemplate=ps, SpawnLocation=loc,
                                                       SpawnRotation=unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0))
        if comp is None:
            _omm_beam.clear()
            return
        _omm_set_beam(comp, src, dst)
        _omm_beam["comp"] = comp._path_name()
    except Exception:  # noqa: BLE001
        _omm_beam.clear()
        _log_once("omm:beam", "On My Mark beam failed: " + traceback.format_exc().splitlines()[-1])


def _omm_beam_tick(pc: UObject) -> None:
    if not _omm_beam.get("comp"):
        return
    try:
        comp = unrealsdk.find_object("ParticleSystemComponent", _omm_beam["comp"])
        if time.monotonic() >= _omm_beam["until"] or pc.Pawn is None:
            comp.DeactivateSystem()
            _omm_beam.clear()
            return
        src, dst = _omm_beam_ends(pc)
        _omm_set_beam(comp, src, dst)
        if _omm_beam.get("report_at") and time.monotonic() >= _omm_beam["report_at"]:
            _omm_beam["report_at"] = 0.0
            _omm_beam_report(comp, src, dst)
    except Exception:  # noqa: BLE001
        _omm_beam.clear()


def _omm_beam_report(comp: UObject, src, dst) -> None:
    """Where the beam really is, a moment after it started: a beam spanning Jack to the mark has bounds about half
    the distance in radius, centred between them; tiny bounds mean no beam particle was built."""
    global _omm_beam_reports
    if _omm_beam_reports >= 3:
        return
    _omm_beam_reports += 1
    try:
        b = comp.Bounds
        dist = math.dist(src, dst)
        mid = tuple((a + c) / 2 for a, c in zip(src, dst))
        req = None
        try:
            em = comp.Template.Emitters[0]
            req = em.LODLevels[0].RequiredModule.Material
        except Exception:  # noqa: BLE001
            pass
        unrealsdk.logging.info(
            f"{TAG} On My Mark beam: {dist:.0f} uu from ({src[0]:.0f}, {src[1]:.0f}, {src[2]:.0f}); bounds radius "
            f"{b.SphereRadius:.0f} at ({b.Origin.X:.0f}, {b.Origin.Y:.0f}, {b.Origin.Z:.0f}), expected ~{dist / 2:.0f} at "
            f"({mid[0]:.0f}, {mid[1]:.0f}, {mid[2]:.0f}); active {comp.bIsActive}, hidden {comp.HiddenGame}, "
            f"material {req.Name if req is not None else None}")
    except Exception:  # noqa: BLE001
        unrealsdk.logging.info(f"{TAG} On My Mark beam report failed: " + traceback.format_exc().splitlines()[-1])


def omm_beam_test(pc: UObject, seconds: float = 5.0) -> str:
    """`doppelskill beamtest`: the painter beam from Jack's gun to a point 800 units ahead, for a few seconds."""
    eye, r = pc.Pawn.GetPawnViewLocation(), pc.Rotation
    pitch, yaw = r.Pitch * math.pi / 32768.0, r.Yaw * math.pi / 32768.0
    f = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    aim = (eye.X + f[0] * 800.0, eye.Y + f[1] * 800.0, eye.Z + f[2] * 800.0)
    global _omm_beam_reports
    _omm_beam_reports = 0
    _omm_start_beam(pc, None, aim, seconds)
    return (f"{TAG} painter beam on for {seconds:.0f} s to a point 800 units ahead" if _omm_beam.get("comp")
            else f"{TAG} painter beam did not spawn (see the log)")


BFF_ARROW = "Quince_FX.Particles.Part_BestFootForward_Up"
BFF_ARROW_ENABLED = False                # TPS never shows it either (checked in TPS 2026-09-15: the graph's node lands nowhere there too)


def bff_arrow(pc: UObject) -> None:
    """Best Foot Forward's kill arrow (v68): TPS's graph spawns it attached to the skill's context;
    the native node here sees the controller as context and puts it nowhere, so it is spawned on
    the pawn when the extension is detected (digijack's StartTime watch)."""
    try:
        if not BFF_ARROW_ENABLED or pc is None or pc.Pawn is None:
            return
        ps = unrealsdk.find_object("ParticleSystem", BFF_ARROW)
        loc = pc.Pawn.Location
        spawn = unrealsdk.make_struct("Vector", X=loc.X, Y=loc.Y, Z=loc.Z - float(pc.Pawn.CylinderComponent.CollisionHeight))
        rot = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=0, Roll=0)
        pc.WorldInfo.MyEmitterPool.SpawnEmitter(EmitterTemplate=ps, SpawnLocation=spawn, SpawnRotation=rot, AttachToActor=pc.Pawn)
        n = _fired.get("bff:arrow", 0) + 1
        _fired["bff:arrow"] = n
        if n <= 3:
            diag.info(f"{TAG} Best Foot Forward: kill arrow spawned on {pc.Pawn.Name}")
    except Exception:
        _log_once("bff:arrow", "Best Foot Forward arrow failed: " + traceback.format_exc().splitlines()[-1])


def _omm_marker_tick(pc: UObject) -> None:
    global _omm_marker_path, _omm_marker_until
    if not _omm_marker_until or time.monotonic() < _omm_marker_until:
        return
    _omm_marker_until = 0.0
    path, _omm_marker_path = _omm_marker_path, None
    try:
        if path:
            comp = unrealsdk.find_object("ParticleSystemComponent", path)
            comp.DeactivateSystem()
    except Exception:
        pass
    try:
        if pc.Pawn is not None:
            _play_ak(pc.Pawn, ON_MY_MARK_AK["trace_stop"])
    except Exception:
        pass
_omm_used = False                 # thrown this activation
_tracked_projectiles: dict[str, list] = {}   # path -> [label, spawn time, checkpoints left]


def _track_projectile(proj: UObject, label: str, fuse: float = 0.0) -> None:
    if not fuse and not diag.enabled:
        return                                   # without a fuse to enforce, tracking only feeds the checkpoint dumps
    try:
        _tracked_projectiles[proj._path_name()] = [label, time.monotonic(), [0.15, 0.6, 1.5, 3.0] if diag.enabled else [], fuse]
    except Exception:
        pass


def _projectile_tracker_tick() -> None:
    now = time.monotonic()
    for path, rec in list(_tracked_projectiles.items()):
        label, t0, marks, fuse = rec
        if fuse and now - t0 >= fuse:
            rec[3] = 0.0
            try:                                     # our fuse: the mod's graph did not run one for the clones' copies
                pr = unrealsdk.find_object("WillowProjectile", path)
                if not pr.bDeleteMe and not pr.bExploded:
                    loc = pr.Location
                    pr.Explode(HitLocation=unrealsdk.make_struct("Vector", X=loc.X, Y=loc.Y, Z=loc.Z), HitNormal=unrealsdk.make_struct("Vector", X=0.0, Y=0.0, Z=1.0))
                    diag.info(f"{TAG} projectile {label}: exploded by our fallback fuse at +{now - t0:.2f}s (the mod's graph had not)")
            except Exception as e:
                diag.info(f"{TAG} projectile {label}: fuse detonate failed: {str(e)[:100]}")
        if not marks:
            if not rec[3]:
                _tracked_projectiles.pop(path, None)
            continue
        if now - t0 < marks[0]:
            continue
        mark = marks.pop(0)
        try:
            pr = unrealsdk.find_object("WillowProjectile", path)
            d = pr.Definition
            diag.info(f"{TAG} projectile {label} +{mark:.2f}s: {pr.Name} def={d.Name if d is not None else None} phys={pr.Physics} loc {_v(pr.Location)} vel {_v(pr.Velocity)} deleteme={pr.bDeleteMe} hidden={pr.bHidden} owner={pr.Owner.Name if pr.Owner is not None else None} instigator={pr.Instigator.Name if pr.Instigator is not None else None}")
            if mark < 0.2:
                try:
                    d = pr.Definition
                    prov = d.BehaviorProviderDefinition if d is not None else None
                    handle = pr.GetBehaviorConsumerHandle()
                    seqs = {}
                    for seq_name in ("Delivery_Lob", "Delivery_Standard", "Payload_Standard", "Payload_BonusPackage", "Payload_Mirv", "Default"):
                        try:
                            seqs[seq_name] = bool(_kernel().IsBehaviorSequenceEnabled(ConsumerHandle=handle, ProviderDefinition=prov, SequenceName=seq_name))
                        except Exception as e:
                            seqs[seq_name] = str(e)[:30]
                    diag.info(f"{TAG} projectile {label} sequences: {seqs} bExploded={pr.bExploded}")
                except Exception as e:
                    diag.info(f"{TAG} projectile {label} sequences: <{str(e)[:80]}>")
                fields = []
                for n in dir(pr):
                    if n.startswith("_") or not any(k in n for k in ("Explos", "Fuse", "Deton", "LifeSpan", "Lifespan", "Damage", "Sticky", "Bounce", "ExpLevel", "Firing", "Grenade", "bCanBeDamaged", "bTearOff", "Role")):
                        continue
                    try:
                        val = getattr(pr, n)
                        if callable(val):
                            continue
                        fields.append(f"{n}={val}")
                    except Exception:
                        continue
                diag.info(f"{TAG} projectile {label} fields: {'; '.join(fields)[:1500]}")
        except Exception as e:
            diag.info(f"{TAG} projectile {label} +{mark:.2f}s: gone ({str(e)[:60]})")
            _tracked_projectiles.pop(path, None)
_omm_prev_active = False
_omm_logged = 0


def _omm_new_activation(pc: UObject) -> None:
    """Skill just started: the throw is available again; show the tracked HUD icon."""
    global _omm_used
    _omm_used = False
    if grade("OnMyMark", pc) <= 0:
        return
    try:
        d = skill_def("OnMyMark_Feedback")
        if d is not None and active_skill("OnMyMark_Feedback", pc) is None:
            pc.GetSkillManager().ActivateSkill(Definition=d, SkillInstigator=pc, SkillGrade=1)
    except Exception:
        _log_once("omm:feedback", "On My Mark feedback skill failed: " + traceback.format_exc().splitlines()[-1])


def _omm_tick(pc: UObject) -> None:
    global _omm_prev_active
    from . import digijack
    active = bool(digijack._active)
    if active and not _omm_prev_active:
        _omm_new_activation(pc)
    _omm_prev_active = active
    _omm_marker_tick(pc)
    _omm_beam_tick(pc)


def _omm_enemy(pc: UObject, target) -> bool:
    """On My Mark's target must be a living enemy: not a Digi-Jack, not a player, and hostile by the game's own test
    (Pawn.IsEnemy: BL2 goes by allegiance; the team test let friendly NPCs through, user 2026-09-28)."""
    if target is None:
        return False
    try:
        from . import digijack
        path = target._path_name()
        if any(e.get("path") == path for e in digijack.all_clone_entries()):
            return False
        if "PlayerPawn" in target.Class.Name or target.GetHealth() <= 0:
            return False
    except Exception:  # noqa: BLE001
        return False                                   # not a pawn (no health): not a mark
    for a, b in ((pc.Pawn, target), (target, pc.Pawn)):
        try:
            return bool(a.IsEnemy(anObject=b))
        except Exception:  # noqa: BLE001
            continue
    return False


# On the host a joined Jack has no aim assist and no HUD, so the two lookups below found nothing and every On My Mark
# tap of his was "no enemy under the crosshair" (co-op test, 2026-10-06). His copy sends its camera with the tap; the
# enemy nearest that line (in a narrow cone, in sight) is his mark.
_remote_view: dict = {}               # controller path -> ((x, y, z), (pitch, yaw), monotonic time)
REMOTE_AIM_CONE_COS = 0.9955          # about 5.5 degrees either side of his view
REMOTE_AIM_RANGE = 9000.0


def _remote_view_target(pc: UObject):
    try:
        view = _remote_view.get(pc._path_name())
    except Exception:  # noqa: BLE001
        view = None
    if view is None or time.monotonic() - view[2] > 1.0:
        _aim_log(f"no view from {getattr(pc, 'Name', pc)} (have {list(_remote_view)}; view {view})")
        return None
    (ex, ey, ez), (pitch, yaw) = view[0], view[1]
    seen = []                                  # (cos, distance, name, outcome) for the log
    blocked_best, blocked_cos = None, -2.0
    k = math.pi / 32768.0
    d = (math.cos(pitch * k) * math.cos(yaw * k), math.cos(pitch * k) * math.sin(yaw * k), math.sin(pitch * k))
    best, best_cos = None, REMOTE_AIM_CONE_COS
    from . import digijack
    clones = {e.get("path") for e in digijack.all_clone_entries()}
    # WorldInfo.PawnList, never find_all: a global scan returns pawns already torn down, and the GetHealth() call on one
    # of them killed the host (access violation at the joined Jack's tap, co-op test 2026-10-06)
    for pawn in _live_pawns(pc):
        try:
            if bool(pawn.bDeleteMe) or "PlayerPawn" in pawn.Class.Name or pawn.GetHealth() <= 0:   # live list: safe to call
                continue
            if pawn._path_name() in clones:
                continue
            loc = pawn.Location
            v = (loc.X - ex, loc.Y - ey, loc.Z + 30.0 - ez)
            dist = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
            if dist < 1.0 or dist > REMOTE_AIM_RANGE:
                continue
            c = (v[0] * d[0] + v[1] * d[1] + v[2] * d[2]) / dist
            if c <= best_cos:
                seen.append((c, dist, pawn.Name, "outside cone"))
                continue
            if not _omm_enemy(pc, pawn):
                seen.append((c, dist, pawn.Name, "not an enemy"))
                continue
            # In sight is preferred, not required: FastTrace from his camera called every enemy "blocked", in the open
            # and dead on his crosshair (cos 1.000; co-op test 2026-10-06). The tight cone does the choosing.
            clear = True
            try:
                clear = bool(pc.LineOfSightTo(Other=pawn))
            except Exception:  # noqa: BLE001
                pass
            seen.append((c, dist, pawn.Name, "candidate" if clear else "candidate (no line of sight)"))
            if clear:
                best, best_cos = pawn, c
            elif blocked_best is None or c > blocked_cos:
                blocked_best, blocked_cos = pawn, c
        except Exception as e:  # noqa: BLE001
            seen.append((-2.0, 0.0, getattr(pawn, "Name", "?"), f"error {type(e).__name__}: {str(e)[:60]}"))
            continue
    if best is None:
        best = blocked_best
    seen.sort(reverse=True)
    _aim_log(f"view eye ({ex:.0f},{ey:.0f},{ez:.0f}) pitch {pitch} yaw {yaw} -> {getattr(best, 'Name', None)}; "
             + "; ".join(f"{n} cos {c:.3f} at {dd:.0f}: {why}" for c, dd, n, why in seen[:5]))
    return best


_aim_logged_at = 0.0


def _aim_log(line: str) -> None:
    """Co-op On My Mark diagnosis (2026-10-06): what the host saw along a joined Jack's view, once a second at most."""
    global _aim_logged_at
    if time.monotonic() - _aim_logged_at < 1.0:
        return
    _aim_logged_at = time.monotonic()
    try:
        from . import remotejack
        remotejack.coop_log("[Doppelganger] co-op aim: " + line)
    except Exception:  # noqa: BLE001
        pass


def _crosshair_target(pc: UObject):
    """The enemy under the crosshair: the auto-aim target, else the HUD's current targetable, else (a joined Jack on
    the host) the enemy along the view his copy sent with the tap."""
    try:
        t = pc.GetPawnAutoAimTarget()
        if isinstance(t, tuple):
            t = next((x for x in t if hasattr(x, "Location")), None)
        if t is not None and hasattr(t, "Location"):
            return t
    except Exception:
        pass
    try:
        t = pc.myHUD.GetHUDMovie().CurrentTargetable
        if t is not None and hasattr(t, "Location"):
            return t
    except Exception:
        pass
    try:
        return _remote_view_target(pc)
    except Exception:  # noqa: BLE001
        return None


def _view_string(pc) -> str:
    """This machine's camera as 'x,y,z,pitch,yaw' (sent with a co-op tap)."""
    try:
        pov = pc.PlayerCamera.CameraCache.POV
        loc, rot = pov.Location, pov.Rotation
    except Exception:  # noqa: BLE001
        loc, rot = pc.Pawn.GetPawnViewLocation(), pc.Rotation
    return f"{loc.X:.1f},{loc.Y:.1f},{loc.Z:.1f},{int(rot.Pitch)},{int(rot.Yaw)}"


def _norm(v):
    m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


# "More accurate On My Mark" (the user's request, 2026-09-27; off by default): instead of the lob (our loft
# plus the grenade definition's UpwardVelocityBonus), each grenade gets the exact velocity that reaches the
# mark in a short flight time under gravity, so its path is fast and nearly straight; a moving enemy is led
# by its horizontal velocity. The velocity goes on after the grenade's spawn events, which would otherwise
# add the upward bonus back.
OMM_DIRECT_SPEED = 3000.0                  # uu/s the flight time is worked out from
OMM_DIRECT_TIME = (0.15, 0.7)              # seconds, nearest and farthest throws
_omm_direct_option = None


def omm_direct_on() -> bool:
    return bool(_omm_direct_option.value) if _omm_direct_option is not None else False


def _omm_direct_velocity(pc: UObject, proj: UObject, spawn, aim, lead):
    """(velocity, gravity, flight time) that take a grenade thrown from `spawn` to `aim`."""
    sx, sy, sz = spawn.X, spawn.Y, spawn.Z
    ax, ay, az = aim

    def flight(x, y, z):
        dist = math.sqrt((x - sx) ** 2 + (y - sy) ** 2 + (z - sz) ** 2)
        return max(OMM_DIRECT_TIME[0], min(OMM_DIRECT_TIME[1], dist / OMM_DIRECT_SPEED))
    t = flight(ax, ay, az)
    if lead is not None:                         # (vx, vy) of the marked enemy, sampled when the button was pressed
        ax, ay = ax + lead[0] * t, ay + lead[1] * t
        t = flight(ax, ay, az)
    g = abs(float(pc.WorldInfo.WorldGravityZ))
    for scale in (getattr(proj.Definition, "GravityScaling", 1.0), getattr(proj, "CustomGravityScaling", 1.0)):
        try:
            if float(scale) > 0:
                g *= float(scale)
        except Exception:
            pass
    return ((ax - sx) / t, (ay - sy) / t, (az - sz) / t + 0.5 * g * t), g, t


# A grenade per Digi-Jack in one frame was a 77 ms hitch with twelve of them out (Hostile Takeover, 2026-10-01): the
# volley now goes out a few grenades a frame, and what every grenade of a frame shares (the mod, its projectile, its
# numbers, its parts and their sequences) is worked out once per frame instead of once per grenade. Only plain values
# (the aim, the target's velocity, the clones' paths) are kept from one frame to the next.
OMM_THROWS_PER_FRAME = 2
_omm_queue: list = []          # Digi-Jack paths still to throw
_omm_shot: dict = {}           # the press's aim and the target's velocity
_omm_notes: list = []          # what went wrong, for the log
OMM_PARTS = ("ItemDefinition", "AlphaItemPartDefinition", "BetaItemPartDefinition", "GammaItemPartDefinition", "DeltaItemPartDefinition",
             "EpsilonItemPartDefinition", "ZetaItemPartDefinition", "EtaItemPartDefinition", "ThetaItemPartDefinition", "MaterialItemPartDefinition")
OMM_VALUES = (("GrenadeDamage", "Damage"), ("BlastRadius", "DamageRadius"), ("FuseTime", "FuseTime"),
              ("StatusEffectDamage", "StatusEffectDamage"), ("NumberOfChildProjectiles", "NumberOfChildProjectiles"))


def _omm_plan(pc: UObject) -> dict | None:
    """What every On My Mark grenade of this frame shares."""
    mod = pc.GetCurrentGrenadeMod()
    if mod is None:
        return None
    pdef = mod.GetProjectileDefinition()
    if pdef is None:
        return None
    values = []
    for src, dst in OMM_VALUES:
        try:
            values.append((dst, getattr(mod, src)))
        except Exception:  # noqa: BLE001
            continue
    parts, wanted = [], []
    dd = mod.DefinitionData
    for attr in OMM_PARTS:
        try:
            part = getattr(dd, attr)
        except Exception:  # noqa: BLE001
            continue
        if part is None:
            continue
        parts.append(part)
        try:
            for st in part.SetProjectileSequenceState:
                wanted.append((str(st.SequenceName), int(st.Action)))
        except Exception:  # noqa: BLE001
            pass
        try:                                         # the delivery part names the projectile: its sequence carries the fuse
            if part.CustomProjectileDefinition is not None and part.CustomProjectileDefinition.Name == pdef.Name:
                wanted.append(("Delivery_" + part.Name.split("_", 1)[1], CHANGE_ENABLE))
        except Exception:  # noqa: BLE001
            pass
    try:
        level = int(pc.PlayerReplicationInfo.ExpLevel)
    except Exception:  # noqa: BLE001
        level = 1
    try:
        fuse = float(mod.FuseTime) or 1.2
    except Exception:  # noqa: BLE001
        fuse = 1.2
    return {"mod": mod, "pdef": pdef, "values": values, "parts": parts, "wanted": wanted, "level": level, "fuse": fuse,
            "provider": pdef.BehaviorProviderDefinition, "direct": omm_direct_on()}


def _omm_throw(pc: UObject, plan: dict, clone: UObject, aim, lead) -> bool:
    """One Digi-Jack's grenade at the mark."""
    global _omm_spawning
    notes = _omm_notes
    cl = clone.Location
    d = (aim[0] - cl.X, aim[1] - cl.Y, aim[2] - (cl.Z + 60.0))
    dist = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    f = _norm(d)
    direct = plan["direct"]
    if direct:
        direction = f                                    # straight at it; the velocity is set below
    else:
        loft = max(0.15, min(0.55, dist / 2000.0))      # arc it like a thrown grenade
        direction = _norm((f[0], f[1], f[2] + loft))
    spawn = unrealsdk.make_struct("Vector", X=cl.X + direction[0] * 70.0, Y=cl.Y + direction[1] * 70.0, Z=cl.Z + 60.0 + direction[2] * 70.0)
    mod, pdef = plan["mod"], plan["pdef"]
    _omm_spawning = True
    try:
        proj = mod.SpawnProjectile(ProjectileDef=pdef, SpawnLocation=spawn, bSpawnWithNoCollisionFail=True)
    finally:
        _omm_spawning = False
    if isinstance(proj, tuple):
        proj = next((x for x in proj if hasattr(x, "Location")), None)
    if proj is None:
        notes.append(f"{clone.Name}: SpawnProjectile returned None")
        return False
    try:                                         # a bare spawn is an inert lob: give it its definition and the mod's parts
        if getattr(proj, "Definition", None) is None:
            proj.InitializeFromDefinition(InDefinition=pdef, InExpLevel=plan["level"], bSkipOnSpawn=True)
    except Exception as e:  # noqa: BLE001
        notes.append(f"InitializeFromDefinition: {str(e)[:90]}")
    for dst, val in plan["values"]:
        for field in (dst + "BaseValue", dst):
            try:
                setattr(proj, field, val)
            except Exception:  # noqa: BLE001
                pass
    try:
        mod.OnGrenadeSpawned(SpawnedProjectile=proj)
    except Exception as e:  # noqa: BLE001
        notes.append(f"OnGrenadeSpawned: {str(e)[:90]}")
    # the mod's definition and parts hang their graphs (fuse, MIRV children, ...) on the projectile's behavior
    # consumer; the item-level OnGrenadeSpawned above did not (no Behavior_Explode ever ran on our grenades, 2026-09-15)
    try:
        handle = proj.GetBehaviorConsumerHandle()
        for part in plan["parts"]:
            try:
                part.OnGrenadeSpawned(SpawnedProjectile=proj, ConsumerHandle=handle)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{part.Name}!{str(e)[:50]}")
    except Exception as e:  # noqa: BLE001
        notes.append(f"parts: {str(e)[:90]}")
    try:
        proj.Instigator = clone
    except Exception as e:  # noqa: BLE001
        notes.append(f"instigator: {str(e)[:60]}")
    direct_vel = None
    if direct:
        try:
            direct_vel, g, t = _omm_direct_velocity(pc, proj, spawn, aim, lead)
            direction = _norm(direct_vel)
        except Exception as e:  # noqa: BLE001
            notes.append(f"direct aim: {str(e)[:80]}")
    try:
        proj.Init(Direction=unrealsdk.make_struct("Vector", X=direction[0], Y=direction[1], Z=direction[2]))
    except Exception as e:  # noqa: BLE001
        notes.append(f"Init: {str(e)[:80]}")
    try:                                         # the projectile graph's sequences the mod's parts ask for (fuse, payload)
        handle = proj.GetBehaviorConsumerHandle()
        for seq_name, action in plan["wanted"]:
            try:
                _kernel().ChangeBehaviorSequenceActivationStatus(ConsumerHandle=handle, ProviderDefinition=plan["provider"],
                                                                SequenceName=seq_name, ActivationStatusChanage=action)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{seq_name}!{str(e)[:50]}")
    except Exception as e:  # noqa: BLE001
        notes.append(f"sequences: {str(e)[:90]}")
    try:                                         # now that every provider is registered: the spawn event (fuse, payload, MIRV ...)
        proj.DoOnSpawn()
    except Exception as e:  # noqa: BLE001
        notes.append(f"DoOnSpawn: {str(e)[:80]}")
    if direct_vel is not None:
        try:                                     # after the spawn events, which add the definition's upward bonus
            before = proj.Velocity
            proj.Velocity = unrealsdk.make_struct("Vector", X=direct_vel[0], Y=direct_vel[1], Z=direct_vel[2])
            _log_once("omm:direct", f"On My Mark direct throw: gravity {g:.0f} uu/s2, flight {t:.2f} s, "
                      f"speed {math.sqrt(sum(c * c for c in direct_vel)):.0f} (the grenade's own throw was "
                      f"{math.sqrt(before.X ** 2 + before.Y ** 2 + before.Z ** 2):.0f}, up {before.Z:.0f})", always=True)
        except Exception as e:  # noqa: BLE001
            notes.append(f"direct velocity: {str(e)[:80]}")
    _track_projectile(proj, f"On My Mark ({clone.Name})", fuse=plan["fuse"] + 0.4)   # fallback only: a no-op once the graph has exploded it
    return True


def _omm_throw_path(pc: UObject, plan: dict, path: str) -> int:
    from . import digijack
    clone = digijack._resolve_pawn(path)
    if clone is None:
        return 0
    try:
        if clone.GetHealth() <= 0:
            return 0
        return 1 if _omm_throw(pc, plan, clone, _omm_shot["aim"], _omm_shot.get("lead")) else 0
    except Exception:  # noqa: BLE001
        _omm_notes.append(f"{path}: " + traceback.format_exc().splitlines()[-1][:120])
        return 0


def omm_queue_tick(pc: UObject) -> None:
    """The rest of an On My Mark volley, OMM_THROWS_PER_FRAME grenades a frame."""
    global _omm_logged
    if not _omm_queue:
        return
    from . import digijack
    if not digijack._active or pc is None or pc.Pawn is None:
        _omm_queue.clear()
        return
    plan = _omm_plan(pc)
    if plan is None:
        _omm_queue.clear()
        return
    batch = _omm_queue[:OMM_THROWS_PER_FRAME]
    del _omm_queue[:OMM_THROWS_PER_FRAME]
    for path in batch:
        _omm_shot["thrown"] = _omm_shot.get("thrown", 0) + _omm_throw_path(pc, plan, path)
    if not _omm_queue and _omm_logged < 6:
        _omm_logged += 1
        diag.info(f"{TAG} On My Mark: the volley is out, {_omm_shot.get('thrown', 0)} grenade(s) in all; {_omm_notes}")


def _on_my_mark(pc: UObject, force: bool = False) -> str:
    global _omm_used, _omm_logged
    from . import digijack
    if not force and (_omm_used or not digijack._active):
        return "not available"
    plan = _omm_plan(pc)
    if plan is None:
        return "no grenade mod equipped, or it has no projectile"
    target = _crosshair_target(pc)
    if not force and not _omm_enemy(pc, target):
        return "no enemy under the crosshair"          # (user, 2026-09-28: it threw at the air, and at clones)
    if target is not None:
        tl = target.Location
        aim = (tl.X, tl.Y, tl.Z + 30.0)
    else:
        cam = pc.Pawn.GetPawnViewLocation()
        rot = pc.Rotation
        pitch, yaw = rot.Pitch * math.pi / 32768.0, rot.Yaw * math.pi / 32768.0
        aim = (cam.X + math.cos(pitch) * math.cos(yaw) * 1200.0, cam.Y + math.cos(pitch) * math.sin(yaw) * 1200.0, cam.Z + math.sin(pitch) * 1200.0)
    lead = None
    if target is not None:
        try:
            v = target.Velocity
            lead = (float(v.X), float(v.Y))
        except Exception:  # noqa: BLE001
            lead = None
    _omm_notes.clear()
    _omm_shot.clear()
    _omm_shot.update(aim=aim, lead=lead, thrown=0)
    paths = [e["path"] for e in list(digijack._clones) if e.get("death_start") is None and e.get("derez_until") is None]
    thrown = 0
    for path in paths[:OMM_THROWS_PER_FRAME]:
        thrown += _omm_throw_path(pc, plan, path)
    _omm_shot["thrown"] = thrown
    _omm_queue[:] = paths[OMM_THROWS_PER_FRAME:]
    if thrown or _omm_queue:
        _omm_used = True
        _play_ak(pc.Pawn, ON_MY_MARK_AK["lock"])
        if _is_local_pc(pc):
            _omm_show_marker(pc, target, aim)
        else:
            # a joined Jack: the marker here (the beam would run from the host's camera); his copy gets the point
            # and draws marker, beam and sounds from his own view (co-op test 2026-10-06: none of it reached him)
            _omm_show_marker(pc, target, aim, beam=False)
            from . import coopnet
            coopnet.tell_mark(pc, aim)
        try:
            sk = active_skill("OnMyMark_Feedback", pc)
            if sk is not None:
                sk.Deactivate()
        except Exception as e:  # noqa: BLE001
            _omm_notes.append(f"feedback off: {str(e)[:60]}")
    how = (f"On My Mark: {thrown} grenade(s) now and {len(_omm_queue)} over the next frames ({plan['pdef'].Name}) at "
           f"{'enemy ' + target.Name if target is not None else 'the crosshair'}; {_omm_notes}")
    if _omm_logged < 6 and not _omm_queue:
        _omm_logged += 1
        diag.info(f"{TAG} {how}")
    return how


# --- allies (co-op, 2026-09-17) ---------------------------------------------------------------------
#
# TPS's native raisers for the friendly events covered players as well as Digi-Jacks. The clone
# side is bridged above (on_clone_damaged, on_clone_died); this is the player side, from the
# host's view of the other players' pawns: OzOnFriendlyEnterInjuredState (Teamwork, Commitment;
# params SkillInstigator, DamagedFriendly) when an ally goes into Fight For Your Life, and
# OzOnFriendlyDamaged (Accountability) for every hit an ally takes, as for a clone.
# Per Jack (co-op, 2026-10-06): on the host every Jack player's allies are watched, each with his own down flags and
# Commitment loop; a joined Jack's skills run on the host, and the single local-only state left his Teamwork and
# Commitment dead. Runs for any host (a Gaige host with a joined Jack too), not only when this machine plays Jack.
_ally_state: dict[str, dict] = {}       # controller path -> {"injured": {pawn name: bool}, Commitment loop fields}
_ally_next = 0.0
_ally_logged = 0


def _ally_pawns(pc: UObject) -> list:
    """The other players' pawns on pc's team, from the level's live pawn list (a find_all of player pawns ran ten
    times a second here, and a global scan can hand back pawns already torn down)."""
    out = []
    try:
        mine = pc.PlayerReplicationInfo
        team = mine.Team if mine is not None else None
        for p in _live_pawns(pc):
            try:
                if "PlayerPawn" not in p.Class.Name or bool(p.bDeleteMe):
                    continue
                pri = p.PlayerReplicationInfo
                if pri is None or mine is None or pri.Name == mine.Name:
                    continue
                if team is not None and pri.Team is not None and pri.Team.Name != team.Name:
                    continue
                out.append(p)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _ally_rec(pc: UObject) -> dict:
    key = pc._path_name()
    rec = _ally_state.get(key)
    if rec is None:
        rec = _ally_state[key] = {"injured": {}, "down": False, "next_stack": 0.0, "next_decay": 0.0, "stacks": 0}
    return rec


def ally_tick_all(local_pc: UObject) -> None:
    """10 Hz, from every player tick of this machine's player: each Jack's allies (only while another player is in
    the game). On a client only its own Jack, as before."""
    global _ally_next
    now = time.monotonic()
    if now < _ally_next:
        return
    _ally_next = now + 0.1
    try:
        if int(local_pc.WorldInfo.NetMode) == 0:            # NM_Standalone
            return
    except Exception:  # noqa: BLE001
        return
    jacks = _jack_controllers()
    keep = set()
    for pc in jacks:
        try:
            keep.add(pc._path_name())
            _ally_tick_for(pc, now)
        except Exception:  # noqa: BLE001
            continue
    for k in list(_ally_state):
        if k not in keep:
            del _ally_state[k]                               # a player who left, or stopped being Jack


def _ally_tick_for(pc: UObject, now: float) -> None:
    """An ally of this Jack entering Fight For Your Life raises the friendly-injured event on his Teamwork and
    Commitment."""
    global _ally_logged
    rec = _ally_rec(pc)
    current: dict[str, bool] = {}
    for pawn in _ally_pawns(pc):
        try:
            # the injured flag alone: a player in Fight For Your Life sits at 0 health, so "and health > 0" never
            # saw anyone go down (co-op test 2026-10-06); a dead player respawns as a new pawn
            injured = bool(pawn.bIsInjured) and not bool(pawn.bDeleteMe)
        except Exception:  # noqa: BLE001
            continue
        if injured != rec["injured"].get(pawn.Name, False):
            try:
                hp = pawn.GetHealth()
            except Exception:  # noqa: BLE001
                hp = "?"
            _ally_log(f"{pawn.Name} injured {injured} (health {hp}) as seen for {pc.Name}")
        was = rec["injured"].get(pawn.Name, False)
        current[pawn.Name] = injured
        if injured and not was:
            fired = fire("Teamwork", "OzOnFriendlyEnterInjuredState", pc=pc, other=pawn, params=event_params(pc, pawn))
            if _ally_logged < 20:
                _ally_logged += 1
                _ally_log(f"ally {pawn.Name} entered Fight For Your Life (for {pc.Name}: Teamwork grade "
                          f"{grade('Teamwork', pc)}, live {active_skill('Teamwork', pc) is not None}, Commitment grade "
                          f"{grade('Commitment', pc)}): OzOnFriendlyEnterInjuredState -> Teamwork {fired}")
    rec["injured"] = current                         # a pawn that died or respawned drops out with its flag
    _commitment_tick(pc, rec, now)


def _ally_log(line: str) -> None:
    """Co-op Teamwork / Commitment diagnosis (2026-10-06): always on, to the co-op log."""
    try:
        from . import remotejack
        remotejack.coop_log(f"{TAG} co-op ally: " + line)
    except Exception:  # noqa: BLE001
        pass


# Commitment while an ally is down. The graph's own route (OzOnFriendlyEnterInjuredState ->
# Commitment_DownState -> CE_StartAddingStacks) walks the player list with TPS's OzBehavior_ArrayAt
# (stubbed here) to count AlliesInDownState, then CE_AddCommitmentStack adds THAT MANY stacks and
# CE_RemoveAllCommitmentStacks takes the same number back one every 0.1 s; with the walk dead the
# count stays 0, so both events run and change nothing (probed 2026-09-17: stacks 0 -> 0). The two
# events the clone route uses take a plain 1: CE_AddCommitmentStackFromClone (stacks < 999 -> +1) and
# CE_RemoveCommitmentStack (stacks > 0 -> -1). So the loop is run here with those: one stack per
# downed ally a second (TPS's cadence, Behavior_Delay's default 1 s), and once nobody is down the
# stacks this loop added come off one every 0.1 s, the graph's own decay rate.
COMMITMENT_STACK_PERIOD = 1.0
COMMITMENT_DECAY_PERIOD = 0.1
COMMITMENT_STACK_CAP = 999


def _commitment_tick(pc: UObject, rec: dict, now: float) -> None:
    """rec: this Jack's loop (rec["stacks"] = the stacks this loop put on; the clone route's have their own 6 s timers)."""
    downed = sum(1 for v in rec["injured"].values() if v)
    down = downed > 0
    if down and not rec["down"]:
        rec["next_stack"] = now                          # the first stack at once, like CE_StartAddingStacks
    if down:
        if now >= rec["next_stack"] and grade("Commitment", pc) > 0:
            rec["next_stack"] = now + COMMITMENT_STACK_PERIOD
            for _ in range(downed):
                if rec["stacks"] >= COMMITMENT_STACK_CAP:
                    break
                if fire("Commitment", "CE_AddCommitmentStackFromClone", pc=pc):
                    rec["stacks"] += 1
            if rec["stacks"] <= 3:
                _ally_log(f"Commitment stack for {pc.Name}: {rec['stacks']} on ({downed} down)")
    elif rec["stacks"] > 0:
        if rec["down"]:
            rec["next_decay"] = now + COMMITMENT_DECAY_PERIOD
        elif now >= rec["next_decay"]:
            rec["next_decay"] = now + COMMITMENT_DECAY_PERIOD
            rec["stacks"] -= 1
            fire("Commitment", "CE_RemoveCommitmentStack", pc=pc)
    rec["down"] = down


def reset_commitment_loop() -> None:
    """Map change / respec: the graph's OnActivated zeroes the stacks, so every loop's count goes too."""
    _ally_state.clear()


def on_ally_damaged(pawn: UObject, damage: float, instigator: UObject | None) -> None:
    """Accountability: an ally taking damage is a friendly taking damage (TPS's raiser covered players)."""
    if damage <= 0.0:
        return
    for pc in _jack_controllers():
        _ally_damaged_for(pc, pawn, damage, instigator)


def _ally_damaged_for(pc: UObject, pawn: UObject, damage: float, instigator: UObject | None) -> None:
    if pc is None or pawn == pc.Pawn or grade("Accountability", pc) <= 0:
        return
    enemy = None
    try:
        enemy = instigator.Pawn if instigator is not None and hasattr(instigator, "Pawn") else instigator
    except Exception:  # noqa: BLE001
        enemy = None
    try:
        if enemy is not None and enemy == pc.Pawn:
            return
        health, max_health = float(pawn.GetHealth()), float(pawn.GetMaxHealth())
    except Exception:  # noqa: BLE001
        health, max_health = 0.0, 0.0
    fire("Accountability", "OzOnFriendlyDamaged", pc=pc, other=pawn,
         params=event_params(pc, None, pawn, enemy, float(damage), health, max_health))


@hook("WillowGame.WillowPlayerPawn:TakeDamage", Type.PRE, hook_identifier="doppel_ally_damage")
def _on_player_pawn_damaged(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_player_pawn_damaged")
    """Every player pawn's hits reach the host; the other players' are Jack's allies' (same team)."""
    try:
        if int(obj.Role) != 3:                            # only the authority sees real damage
            return
        on_ally_damaged(obj, float(args.Damage), args.InstigatedBy)   # each Jack but the one hit (any Jack, co-op)
    except Exception:  # noqa: BLE001
        _log_once("ally:damage", "ally damage bridge failed: " + traceback.format_exc().splitlines()[-1])


# --- Inspire diagnostic --------------------------------------------------------------------------
_inspire_logged = 0
_dialog_probe: dict = {}
# the four dialog hooks below fire for every NPC line in the world: probes, in diag.DIAG_HOOKS (performance pass 2026-09-15)


def _dialog_probe_log(name: str, obj, args, extra: str = "") -> None:
    n = _dialog_probe.get(name, 0)
    if n >= 10:
        return
    _dialog_probe[name] = n + 1
    diag.info(f"{TAG} dialog probe: {name} on {_path_of(obj)} {extra}")


def _path_of(obj) -> str:
    try:
        return obj._path_name()
    except Exception:
        return repr(obj)


@hook("GearboxFramework.GearboxDialogComponent:TriggerEvent", Type.PRE, hook_identifier="doppel_dlg_trigger")
def _on_dialog_trigger(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_dialog_trigger")
    try:
        tag = getattr(args, "EventTag", None) or getattr(args, "Tag", None)
        _dialog_probe_log("TriggerEvent", obj, args, f"tag={tag.Name if tag is not None else None} args={[n for n in dir(args) if not n.startswith('_')][:8]}")
    except Exception:
        pass


@hook("GearboxFramework.GearboxDialogComponent:Talk", Type.PRE, hook_identifier="doppel_dlg_talk")
def _on_dialog_talk(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_dialog_talk")
    try:
        _dialog_probe_log("Talk", obj, args, f"args={[(n, getattr(args, n)) for n in dir(args) if not n.startswith('_')][:6]}")
    except Exception:
        pass


@hook("GearboxFramework.GearboxDialogAct_Chance:Activate", Type.PRE, hook_identifier="doppel_dlg_chance")
def _on_dialog_chance(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_dialog_chance")
    try:
        if "Doppel" in _path_of(obj):
            _dialog_probe_log("Chance.Activate", obj, args, f"chance={getattr(obj, 'Chance', '?')}")
    except Exception:
        pass


@hook("WillowGame.WillowDialogAct_Talk:Activate", Type.PRE, hook_identifier="doppel_dlg_talkact")
def _on_dialog_talk_act(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_dialog_talk_act")
    try:
        if "Doppel" in _path_of(obj):
            td = list(obj.TalkData)
            _dialog_probe_log("TalkAct.Activate", obj, args, f"lines={[(t.NameTag.Name if t.NameTag is not None else None, t.TalkAkEvent.Name if t.TalkAkEvent is not None else None) for t in td][:3]}")
    except Exception:
        pass


INSPIRE_SCREEN_NODES = {"inspired": "Quince_Doppel_Hero.Skills.Inspire.BehaviorProviderDefinition_0.Behavior_ScreenParticle_6",
                        "disillusioned": "Quince_Doppel_Hero.Skills.Inspire.BehaviorProviderDefinition_0.Behavior_ScreenParticle_7"}


SCREEN_FX_SECONDS = 4.0
_bar_hidden_for_fx_until = 0.0
_bar_fx_hidden = False


def _set_bar_clip_visible(pc: UObject, visible: bool) -> bool:
    try:
        movie = _hud_movie(pc)
        clip = movie.GetVariableObject(BAR_CLIP) if movie is not None else None
        if clip is None:
            return False
        clip.SetVisible(Visible=visible)
        return True
    except Exception:
        _log_once("fx:barclip", "action-skill bar clip visibility failed: " + traceback.format_exc().splitlines()[-1])
        return False


def _screen_fx_bar_tick(pc: UObject) -> None:
    """Keep the action-skill bar hidden while an Inspire screen effect plays, then show it again."""
    global _bar_fx_hidden
    now = time.monotonic()
    if now < _bar_hidden_for_fx_until:
        if not _bar_fx_hidden:
            _bar_fx_hidden = _set_bar_clip_visible(pc, False)
    elif _bar_fx_hidden:
        _bar_fx_hidden = False
        _set_bar_clip_visible(pc, True)


def _inspire_screen(pc: UObject, which: str) -> None:
    """The graph's own ScreenParticle nodes sit on branches BL2 never reaches (the "inspired" one
    behind a TPS-only ArrayAt, the "disillusioned" one behind the graph's own 1-in-50 gate), so show
    them here with the node's own init parameters (v60 wired their templates)."""
    try:
        if not _fx_applied:
            _fx_apply_materials()
        node = unrealsdk.find_object("Behavior_ScreenParticle", INSPIRE_SCREEN_NODES[which])
        params = node.Parameters
        if params.Template is None:
            _log_once("inspire:screen:" + which, f"Inspire screen effect '{which}': node has no template")
            return
        pc.ShowScreenParticle(InitParams=params)
        global _bar_hidden_for_fx_until
        _bar_hidden_for_fx_until = time.monotonic() + SCREEN_FX_SECONDS   # the HUD (bar included) always draws over screen particles
        n = _fired.get("inspire:screen:" + which, 0) + 1
        _fired["inspire:screen:" + which] = n
        if n <= 3:
            diag.info(f"{TAG} Inspire screen effect '{which}' shown ({params.Template.Name})")
    except Exception:
        _log_once("inspire:screen:" + which + ":err", f"Inspire screen effect '{which}' failed: " + traceback.format_exc().splitlines()[-1])


INSPIRE_AK = {"rainbow": "Ake_Quince_FX_Player_Jack.Skills.Ak_Play_Dlc_Quince_FX_Jack_Inspire_Rainbow",
              "disillusioned": "Ake_Quince_FX_Player_Jack.Skills.Ak_Play_Dlc_Quince_FX_Jack_Disillusioned_ShittyFix"}
DISILLUSION_TAG = "GD_Quince_Dialog_Player.VOBD_Player_Doppel.VOBD_Gen_React_Disappointment"
INSPIRATION_TAG = "GD_Quince_Dialog_Player.VOBD_Player_Doppel.VOBD_Gen_Start_Inspiration"
DISILLUSION_CHANCE = 0.5                 # user's call (2026-09-15): TPS's own gate (inspired within 15 s of bleeding out) fired ~once in 50 deaths
_disillusion_was_injured = False
_disillusion_logged = 0


def _play_ak(pawn: UObject, path: str) -> bool:
    try:
        ev = unrealsdk.find_object("AkEvent", path)
        try:
            client = int(pawn.WorldInfo.NetMode) == 3          # a client: replicated would not play here
        except Exception:  # noqa: BLE001
            client = False
        pawn.PlayAkEvent(InSoundCue=ev, bNotReplicated=client, bStopWhenOwnerDestroyed=True)
        return True
    except Exception:
        _log_once("ak:" + path, f"PlayAkEvent({path.split('.')[-1]}) failed: " + traceback.format_exc().splitlines()[-1])
        return False


@hook("GearboxFramework.Behavior_TriggerDialogEvent:ApplyBehaviorToContext", Type.PRE, hook_identifier="doppel_inspire_dialog")
def _on_dialog_event(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_dialog_event")
    """Inspire's graph reaches its dialog lines in BL2, but the "inspired" sound/screen effect sat
    on the CE_InspireOthers_Iterate branch behind OzBehavior_ArrayAt (TPS-only, neutralised): play
    the sound for the local player here. (The screen particle needs its TPS materials re-shadered.)"""
    global _inspire_logged
    try:
        bpd = obj.Outer
        skill = bpd.Outer if bpd is not None else None
        if skill is None or skill.Name != "Inspire":
            return
        tag = obj.EventTag
        name = tag.Name if tag is not None else ""
        if _inspire_logged < 8:
            _inspire_logged += 1
            diag.info(f"{TAG} Inspire dialog event: {name} (node {obj.Name})")
        pc = _pc()
        if pc is not None and pc.Pawn is not None:
            if name == "VOBD_Gen_Start_Inspiration":
                queue_inspire_screen("inspired", "own line")
            elif name == "VOBD_Gen_React_Disappointment":
                queue_inspire_screen("disillusioned", "own line")
    except Exception:
        pass


# TPS does not show the word while Jack is still talking. Its graph (upk_bpd_graph on
# Quince_Doppel_Hero.Skills.Inspire.BehaviorProviderDefinition_0) runs
#
#   CE_InspireOthers -> CompareBool_10 -pin 1-> TriggerDialogEvent_394        (the line)
#                                     -pin 1-> RemoteCustomEvent_15 -> Delay_30065 (4 s)
#   CE_InspireOthers_Iterate -> ArrayAt_7 -> ScreenParticle_6                 (the word)
#                                        -> PostAkEvent_1212                 (the rainbow sting)
#
# so the word and the sting are four seconds behind the line, on the same branch as each other, and
# we were firing all three at once (user, 2026-09-22). The disillusioned half is our own trigger
# rather than TPS's, whose gate almost never fired, so it waits for its line the same way: the
# Disappointment AkEvent's MaxDuration is 3.125.
# Each of the twenty recorded lines behind an event runs for a different length, so a fixed wait is
# wrong however it is chosen; the first attempt took four seconds from Behavior_Delay_30065, which
# turns out to sit on the branch that iterates over the other players rather than on the line
# (user, 2026-09-22). GearboxDialogComponent.IsTalking answers directly (`doppelskill inspirediag`),
# so the word waits for the line itself and the timeout is only there to stop a queue surviving a
# line that never reports finishing.
# IsTalking alone fired the effect about a quarter of a second in: it goes true and false again
# while the act is still starting, so "spoke, then quiet" is satisfied immediately (user,
# 2026-09-22). GearboxDialogComponent.StopTalking is the event that actually ends a line, so that
# is what releases the queue; the poll stays only as a floor, held off until the line has had time
# to be longer than the flicker, and the timeout still catches a line that never reports at all.
# Rewritten 2026-09-29 (user: the word still came ~4 s late at times, or while Jack was still talking). The request
# (the skill's dialog node, going down, another player's line) only books a slot; the line counts as started when
# a dialog component's Talk actually plays the Inspire AkEvent (a line that loses the floor to another never plays,
# and its word used to arrive at the timeout anyway), and it counts as finished when the AkComponent playing that
# event stops playing it (AkComponent.IsPlayingEvent). Before, any component's StopTalking after 0.4 s released it
# (a clone's or an older line's: the word mid-sentence), and a line whose StopTalking never came waited out the
# timeout. Fallbacks: that same dialog component's StopTalking, then the event's MaxDuration (its longest take).
INSPIRE_EVENTS = {"inspired": "Ake_Quince_VOBD.Dlc_Quince_VOBD_PL_Doppelganger.Ak_Play_Dlc_Quince_VOBD_Doppelganger_GEN_Start_Inspiration",
                  "disillusioned": "Ake_Quince_VOBD.Dlc_Quince_VOBD_PL_Doppelganger.Ak_Play_Dlc_Quince_VOBD_Doppelganger_GEN_React_Disappointment"}
INSPIRE_START_WAIT = 2.5             # a booked line that has not started by then never will (another had the floor)
INSPIRE_FIND_WINDOW = 1.5            # how long to look for the AkComponent playing it
INSPIRE_FIND_EVERY = 0.1
_screen_queue: list = []
_inspire_sub_logged = 0


def queue_inspire_screen(which: str, why: str = "", dialog: UObject | None = None) -> None:
    """Book the screen effect and its sting for Jack's next Inspire line; with `dialog`, that line has started.
    A second booking while one of the same kind is still waiting for its line is the same line (the skill's
    graph can raise the dialog event more than once)."""
    now = time.monotonic()
    for e in _screen_queue:
        if e["which"] == which and (e["started"] is None or now - e["started"] < 0.5):
            if dialog is not None and e["started"] is None:
                _inspire_line_started(e, dialog)
            return
    entry = {"which": which, "at": now, "started": None, "dlg": None, "ak": None, "seen": False,
             "next_find": 0.0, "finds": 0, "cap": 0.0, "why": why}
    _screen_queue.append(entry)
    if dialog is not None:
        _inspire_line_started(entry, dialog)
    diag.info(f"{TAG} Inspire ({which}): booked ({why}); the word follows the line")


def _inspire_line_started(entry: dict, dialog: UObject) -> None:
    now = time.monotonic()
    entry["started"] = now
    try:
        entry["dlg"] = dialog._path_name()
    except Exception:  # noqa: BLE001
        entry["dlg"] = None
    try:
        longest = float(unrealsdk.find_object("AkEvent", INSPIRE_EVENTS[entry["which"]]).MaxDuration)
    except Exception:  # noqa: BLE001
        longest = 6.0
    entry["cap"] = now + max(1.0, longest) + 0.3
    diag.info(f"{TAG} Inspire ({entry['which']}): line started {now - entry['at']:.2f}s after it was booked")


def _talk_event_path(act) -> str:
    try:
        data = list(act.TalkData)
        ev = data[0].TalkAkEvent if data else None
        return ev._path_name() if ev is not None else ""
    except Exception:  # noqa: BLE001
        return ""


@hook("GearboxFramework.GearboxDialogComponent:Talk", Type.PRE, hook_identifier="doppel_inspire_talk")
def _on_inspire_talk(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    """A booked Inspire line actually starting (every line in the world passes here: the queue test comes first)."""
    if not any(e["started"] is None for e in _screen_queue):
        return
    try:
        path = _talk_event_path(args.TalkAction)
    except Exception:  # noqa: BLE001
        return
    for entry in _screen_queue:
        if entry["started"] is None and path == INSPIRE_EVENTS[entry["which"]]:
            _inspire_line_started(entry, obj)
            return


def _ak_components_near(dialog_path: str | None, everywhere: bool = True) -> list:
    """AkComponents that may be playing the line: the dialog component's owner's, the player's, then (with
    `everywhere`) all of them."""
    out, seen = [], set()

    def add(comp):
        try:
            key = comp._path_name()
        except Exception:  # noqa: BLE001
            return
        if key not in seen:
            seen.add(key)
            out.append(comp)
    owners = []
    try:
        if dialog_path:
            owners.append(unrealsdk.find_object("GearboxDialogComponent", dialog_path).Owner)
    except Exception:  # noqa: BLE001
        pass
    pc = _pc()
    if pc is not None:
        owners.append(pc.Pawn)
    for owner in owners:
        try:
            for comp in owner.AllComponents:
                if comp is not None and "AkComponent" in comp.Class.Name:
                    add(comp)
        except Exception:  # noqa: BLE001
            pass
    if everywhere:
        try:
            for comp in unrealsdk.find_all("AkComponent", exact=False):
                if not comp.Name.startswith("Default__") and comp.Owner is not None:
                    add(comp)
        except Exception:  # noqa: BLE001
            pass
    return out


def _inspire_subtitle_note(comp: UObject) -> None:
    """What the engine made of the line's subtitle (the echo lines showed none, 2026-09-29)."""
    global _inspire_sub_logged
    if _inspire_sub_logged >= 4:
        return
    _inspire_sub_logged += 1
    try:
        pend = comp.PendingSubtitle
        name = str(pend.OriginalFileName)
        data = len(list(pend.StreamedSubtitleData))
    except Exception as e:  # noqa: BLE001
        name, data = f"unreadable ({e})", -1
    try:
        kill = len(list(comp.SubtitlesPendingKill))
    except Exception:  # noqa: BLE001
        kill = -1
    try:
        owner = comp.Owner.Class.Name if comp.Owner is not None else None
    except Exception:  # noqa: BLE001
        owner = "?"
    unrealsdk.logging.info(f"{TAG} Inspire subtitle: played by {owner}'s AkComponent; OriginalFileName {name!r}, "
                           f"subtitle data {data}, pending kill {kill}")


def _release_inspire_screen(pc: UObject, why: str) -> None:
    for entry in list(_screen_queue):
        _release_one(pc, entry, why)


def _release_one(pc: UObject, entry: dict, why: str) -> None:
    if entry in _screen_queue:
        _screen_queue.remove(entry)
    if pc is None or pc.Pawn is None:
        return
    which = entry["which"]
    started = entry["started"] or entry["at"]
    diag.info(f"{TAG} Inspire ({which}): {why}, {time.monotonic() - started:.1f}s after the line started")
    _play_ak(pc.Pawn, INSPIRE_AK["rainbow" if which == "inspired" else "disillusioned"])
    _inspire_screen(pc, which)
    if which == "inspired":
        start_rainbow_sweep()


INSPIRE_SPEAK_MIN = 0.4              # a StopTalking this soon is the previous line, not ours


@hook("GearboxFramework.GearboxDialogComponent:StopTalking", Type.POST, hook_identifier="doppel_stoptalk")
def _on_stop_talking(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_stop_talking")
    """The line has ended: the word and its sting follow it.

    This used to insist the component was the pawn's own, and never matched: Jack's lines play
    through a WillowDialogEchoActor's component, not the speaker's, which the co-op path already
    knew (_on_remote_inspire). Every release therefore fell through to the timeout, which is the
    word arriving long after the quote (user, 2026-09-22). Any component will do, since the queue
    only holds anything while one of Jack's own Inspire lines is in flight. Since 2026-09-29 only the
    component that started the line counts, and only while the sound itself could not be found."""
    if not _screen_queue:
        return
    try:
        path = obj._path_name()
    except Exception:  # noqa: BLE001
        return
    pc = _pc()
    if pc is None or pc.Pawn is None:
        return
    for entry in list(_screen_queue):
        if entry["started"] is not None and entry["ak"] is None and entry["dlg"] == path \
                and time.monotonic() - entry["started"] >= INSPIRE_SPEAK_MIN:
            _release_one(pc, entry, "line ended (StopTalking of its own component)")


def inspire_screen_tick(pc: UObject) -> None:
    """Follow each started line's sound to its end (see INSPIRE_EVENTS)."""
    if not _screen_queue or pc is None or pc.Pawn is None:
        return
    now = time.monotonic()
    for entry in list(_screen_queue):
        try:
            event = unrealsdk.find_object("AkEvent", INSPIRE_EVENTS[entry["which"]])
        except Exception:  # noqa: BLE001
            event = None
        # look for the sound: before Talk reports the start (the start is then the moment it is found playing),
        # and for a while after it
        looking = entry["started"] is None or now - entry["started"] < INSPIRE_FIND_WINDOW
        if entry["ak"] is None and event is not None and looking and now >= entry["next_find"]:
            entry["next_find"] = now + INSPIRE_FIND_EVERY
            entry["finds"] += 1           # the owner's and the player's every time, every component every third
            for comp in _ak_components_near(entry["dlg"], everywhere=entry["finds"] % 3 == 0):
                try:
                    if comp.IsPlayingEvent(AkEvent=event):
                        entry["ak"], entry["seen"] = comp._path_name(), True
                        if entry["started"] is None:
                            _inspire_line_started(entry, comp)
                            entry["dlg"] = None
                        _inspire_subtitle_note(comp)
                        break
                except Exception:  # noqa: BLE001
                    continue
        if entry["started"] is None:
            if now - entry["at"] > INSPIRE_START_WAIT:
                _screen_queue.remove(entry)
                diag.info(f"{TAG} Inspire ({entry['which']}): the line never started ({entry['why']}); no word")
            continue
        if entry["ak"] is not None and event is not None:
            try:
                playing = bool(unrealsdk.find_object("AkComponent", entry["ak"]).IsPlayingEvent(AkEvent=event))
            except Exception:  # noqa: BLE001
                playing = False
            if playing:
                entry["seen"] = True
            elif entry["seen"]:
                _release_one(pc, entry, "line finished")
                continue
        if now >= entry["cap"]:
            _release_one(pc, entry, "its longest take's length passed")


# Everything Jack says meets at his dialog component, whoever triggered it: the mod's own calls,
# and the Behavior_TriggerDialogEvent nodes in the Pre-Sequel's skill graphs that Sponsored By,
# Leadership, Promote the Ranks and the rest fire. Gating the mod's calls alone covered a fraction
# of what a player hears (user, 2026-09-22), so the setting is applied here. Only the local Jack's
# own component is touched, so no other character loses a line, and Inspire is always let through:
# it is the skill's payoff, and its screen effect waits on the line ending.
CHATTER_EXEMPT = ("VOBD_Gen_Start_Inspiration", "VOBD_Gen_React_Disappointment")
# Whose nodes count as Jack talking. The clones are his voice too and the setting is meant to cover
# them. GD_PlayerShared is deliberately absent: its melee grunt nodes are shared with every other
# character, and a node fires for whoever swung, so holding those back would mute other players.
CHATTER_OWNERS = ("Quince_Doppel_", "GD_Doppelganger_", "GD_Quince_JackClone.")
_chatter_held = 0


def _chatter_holds(name: str) -> bool:
    """Should this line be dropped? Inspire never is."""
    global _chatter_held
    from . import digijack
    chance = digijack.chatter_chance()
    if chance >= 1.0 or any(name.endswith(t) for t in CHATTER_EXEMPT):
        return False
    if chance > 0.0 and random.random() <= chance:
        return False
    _chatter_held += 1
    if _chatter_held in (1, 5, 25, 100):
        unrealsdk.logging.info(f"{TAG} chatter: {_chatter_held} line(s) held back so far (this one {name})")
    return True


# Every talking AI's component comes through the hook below, 40 calls a second in a fight (doppelperf, 2026-09-30),
# and each paid for the controller, the pawn and the component before it could say "not Jack's". The local Jack's
# component address is kept here, refreshed once a second from the tick, and anything else leaves at once.
_jack_dialog_addr = 0
_jack_dialog_next = 0.0


def _dialog_addr_tick(pc: UObject) -> None:
    global _jack_dialog_addr, _jack_dialog_next
    now = time.monotonic()
    if now < _jack_dialog_next:
        return
    _jack_dialog_next = now + 1.0
    try:
        comp = pc.Pawn.DialogComponent if pc.Pawn is not None else None
        _jack_dialog_addr = comp._get_address() if comp is not None else 0
    except Exception:  # noqa: BLE001
        _jack_dialog_addr = 0


@hook("GearboxFramework.GearboxDialogComponent:TriggerEvent", Type.PRE, hook_identifier="doppel_chatter")
def _on_dialog_chatter(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_dialog_chatter")
    """The lines the mod triggers itself, which come through the pawn's own component."""
    try:
        if not _jack_dialog_addr or obj._get_address() != _jack_dialog_addr:
            return None
        pc = _pc()
        # the local player's own component, and only while that player is Jack: with the chatter setting below
        # "Every time", other characters' lines were thinned out too (found 2026-09-29)
        if pc is None or pc.Pawn is None or obj is not pc.Pawn.DialogComponent or not _ours(pc):
            return None
        tag = args.EventTag
        name = str(tag.Name) if tag is not None else ""
    except Exception:  # noqa: BLE001
        return None
    return Block if _chatter_holds(name) else None


@hook("GearboxFramework.Behavior_TriggerDialogEvent:ApplyBehaviorToContext", Type.PRE,
      hook_identifier="doppel_chatter_graph")
def _on_graph_chatter(obj: UObject, args: WrappedStruct, ret, func: BoundFunction):
    diag.hcrumb("skills._on_graph_chatter")
    """And the ones the Pre-Sequel's own skill graphs trigger, which do not reach the component hook
    at all: Sponsored By, Leadership, Promote the Ranks and the rest were still talking with the
    setting off (user, 2026-09-22). Every such node lives under the skill that speaks it, so a node
    outered to one of Jack's skill packages is his line and nobody else's."""
    try:
        bpd = obj.Outer
        skill = bpd.Outer if bpd is not None else None
        if skill is None or not str(skill._path_name()).startswith(CHATTER_OWNERS):
            return None
        tag = obj.EventTag
        name = str(tag.Name) if tag is not None else ""
    except Exception:  # noqa: BLE001
        return None
    return Block if _chatter_holds(name) else None


INSPIRE_TAGS = {"VOBD_Gen_Start_Inspiration": "inspired", "VOBD_Gen_React_Disappointment": "disillusioned"}
_remote_inspire_logged = 0


@hook("GearboxFramework.GearboxDialogComponent:TalkReplicated", Type.PRE, hook_identifier="doppel_remote_inspire")
def _on_remote_inspire(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_remote_inspire")
    """Another player's Jack inspires (or is disillusioned): his line arrives here on every other
    machine with its event tag, and TPS showed the screen effect and played the sting for the
    whole party. The local player's own lines go through _on_dialog_event instead (co-op, 2026-09-17)."""
    global _remote_inspire_logged
    try:
        data = args.NewDialogData
        tag = data.EventTag
        which = INSPIRE_TAGS.get(str(tag.Name)) if tag is not None else None
        if which is None:
            return
        # the component's owner is a WillowDialogEchoActor, not the speaker: Jack's lines are told
        # apart by their event (Ake_Quince_VOBD.Dlc_Quince_VOBD_PL_Doppelganger.*)
        event = data.TalkAkEvent
        if event is None or "Doppelganger" not in event._path_name():
            return
        pc = _pc()
        if pc is None or pc.Pawn is None or _ours(pc):
            return                                # the local Jack's own lines: _on_dialog_event / _disillusion_tick
        if _remote_inspire_logged < 6:
            _remote_inspire_logged += 1
            diag.info(f"{TAG} remote Inspire ({which}): {event.Name}")
        queue_inspire_screen(which, "another player's Jack", dialog=obj)
    except Exception:  # noqa: BLE001
        _log_once("inspire:remote", "remote Inspire failed: " + traceback.format_exc().splitlines()[-1])


_disillusion_next = 0.0


def _disillusion_tick(pc: UObject) -> None:
    """Going down with Inspire: a coin flip on the disappointment line + sound."""
    global _disillusion_was_injured, _disillusion_logged, _disillusion_next
    now = time.monotonic()
    if now < _disillusion_next:          # 10 Hz: two reads a frame for a state that changes once a fight
        return
    _disillusion_next = now + 0.1
    pawn = pc.Pawn
    if pawn is None:
        return
    injured = bool(pawn.bIsInjured)
    if injured and not _disillusion_was_injured and grade("Inspire", pc) > 0:
        roll = random.random()
        if roll < DISILLUSION_CHANCE:
            from . import digijack
            digijack._trigger_dialog(pawn, DISILLUSION_TAG, "disillusion")
            queue_inspire_screen("disillusioned", "went down")
        if _disillusion_logged < 6:
            _disillusion_logged += 1
            diag.info(f"{TAG} Inspire: went down, disillusion roll {roll:.2f} -> {'line' if roll < DISILLUSION_CHANCE else 'nothing'}")
    _disillusion_was_injured = injured


ACTION_SKILL_INPUT_ACTION = "ActionSkill"     # GD_Input.Actions.InputAction_ActionSkill.ActionName


def _action_skill_key_names(pc: UObject) -> set:
    """The keys bound to the action skill.

    `PlayerInput.Bindings` is empty in this build (`doppelcheck keys`: 0 bindings), so reading it
    found nothing and the defaults below were used, which are right only for a player still on F:
    everyone who had rebound it kept having to press F (players, 2026-09-22). BL2 remaps named input
    actions instead, and `WillowPlayerInput.GetKeyForAction` answers for the action's current key."""
    keys = set()
    try:
        pi = pc.PlayerInput
        for only_kb in (True, False):
            try:
                n = pi.GetKeyForAction(ActionName=ACTION_SKILL_INPUT_ACTION, bOnlyKeyboardMouse=only_kb, bNoRemapping=False)
            except Exception:
                n = pi.GetKeyForAction(ACTION_SKILL_INPUT_ACTION, only_kb, False)
            if n is not None and str(n) not in ("", "None"):
                keys.add(str(n))
    except Exception:
        _log_once("keys:getkeyforaction", "GetKeyForAction failed: " + traceback.format_exc().splitlines()[-1])
    try:
        for b in pc.PlayerInput.Bindings:            # the classic route, empty here but harmless
            if "StartActionSkill" in str(b.Command):
                keys.add(str(b.Name))
    except Exception:
        pass
    return keys or {"F", "XboxTypeS_LeftShoulder"}      # BL2's defaults when nothing can be read


def forget_action_skill_keys() -> None:
    """After the options menu: the player may have rebound it."""
    global _action_skill_keys
    _action_skill_keys = None


# TPS: while Expendable Assets runs, a TAP of the action-skill key is On My Mark and a HOLD retracts
# the Digi-Jacks early (Skill_EndActionSkillEarly). Press starts a timer; release before the threshold
# is the tap; the tick fires the retract once the threshold passes (release after that does nothing).
# The threshold is the engine's own button hold time, WillowPlayerInput.ButtonHoldEventTime (0.35 s in both games'
# DefaultInput.ini; TPS's native input marks a button BUTTON_STATE_Held after it). It was 0.75 s here, so the recall
# and its cooldown refund came 0.4 s after TPS's (a player, 2026-10-03).
HOLD_TO_END_SECONDS = 0.35          # fallback when the controller's PlayerInput cannot be read
_hold_seconds_cache = None


def _hold_to_end_seconds(pc: UObject) -> float:
    global _hold_seconds_cache
    if _hold_seconds_cache is None:
        try:
            v = float(pc.PlayerInput.ButtonHoldEventTime)
            _hold_seconds_cache = v if 0.1 <= v <= 2.0 else HOLD_TO_END_SECONDS
        except Exception:  # noqa: BLE001
            return HOLD_TO_END_SECONDS
    return _hold_seconds_cache
_skill_key_down_since = 0.0
_skill_key_hold_done = False


def _skill_key_hold_tick(pc: UObject) -> None:
    global _skill_key_down_since, _skill_key_hold_done
    if not _skill_key_down_since or _skill_key_hold_done:
        return
    if time.monotonic() - _skill_key_down_since < _hold_to_end_seconds(pc):
        return
    _skill_key_hold_done = True
    from . import digijack
    if _casting_remotely(pc) and pc.Pawn is not None and not pc.Pawn.bIsInjured:
        from . import coopnet
        if coopnet.send_input("hold"):
            diag.info(f"{TAG} co-op: recall sent to the host")
        return
    if _casting_here(pc) and pc.Pawn is not None and not pc.Pawn.bIsInjured:
        if digijack.end_action_skill_early("action-skill key held"):
            n = _fired.get("endearly", 0) + 1
            _fired["endearly"] = n
            if n <= 3:
                diag.info(f"{TAG} hold-to-retract: skill key held {_hold_to_end_seconds(pc):.2f}s -> Digi-Jacks retracted")


def relay_input(pc, kind: str) -> None:
    """On the host: a joined Jack's action-skill key while his cast runs (coopnet.doppel_coop_input). Acted on only
    for the player whose cast it is: the sender must be the caster digijack recorded."""
    from . import digijack
    try:
        if pc is None or not _ours(pc):
            return
        if "|" in kind:                                     # "tap|x,y,z,pitch,yaw": his camera, for the mark
            kind, view = kind.split("|", 1)
            try:
                x, y, z, p, w = view.split(",")
                _remote_view[pc._path_name()] = ((float(x), float(y), float(z)), (int(p), int(w)), time.monotonic())
            except Exception:  # noqa: BLE001
                pass
        with digijack.using_key(pc._path_name()):          # the sender's own cast record (co-op phase 2)
            if not digijack._active:
                _log_once("relay:who", f"{TAG} co-op: {kind} from {pc.Name} ignored: his cast is not running")
                return
            pawn = pc.Pawn
            if pawn is None or pawn.bIsInjured:
                return
            if kind == "tap":
                if not _omm_used and grade("OnMyMark", pc) > 0:
                    unrealsdk.logging.info(f"{TAG} co-op: On My Mark for {pc.Name}: {_on_my_mark(pc)}")
            elif kind == "hold":
                if digijack.end_action_skill_early("action-skill key held (co-op)"):
                    unrealsdk.logging.info(f"{TAG} co-op: {pc.Name} recalled his Digi-Jacks")
    except Exception:  # noqa: BLE001
        _log_once("relay:fail", f"{TAG} co-op relay of {kind} failed: " + traceback.format_exc().splitlines()[-1])


def _casting_here(pc) -> bool:
    """A cast this machine's player owns is running here (the host's own, or a solo game's). On a co-op host the
    running cast may be a joined Jack's: the host's own tap and hold must not touch it (holding F on the host recalled
    the joined Jack's Digi-Jacks, 2026-10-06)."""
    from . import digijack
    if not digijack._active:
        return False
    try:
        return not digijack._pc_path or pc is None or pc._path_name() == digijack._pc_path
    except Exception:  # noqa: BLE001
        return True


def _casting_remotely(pc) -> bool:
    """This copy is a client whose cast the host says is running (the cast and clones are the host's)."""
    try:
        from . import coopnet
        return coopnet.remote_cast_active() and coopnet.is_client(pc)
    except Exception:  # noqa: BLE001
        return False


# Other mods' uses of the action-skill tap while the skill runs (Extra Skill Trees' Lateral Transfer: tap while
# aiming at a Digi-Jack to swap places with it). A handler takes the controller and returns True when it took the
# tap; On My Mark then does not fire. They are asked first, in order.
ACTION_TAP_HANDLERS: list = []


@hook("WillowGame.WillowUIInteraction:InputKey", Type.PRE)
def _on_input_key(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("skills._on_input_key")
    """Potential: 'you can use your Action Skill while in Fight For Your Life'. TPS did that
    natively; BL2's downed input mode never lets the action-skill key reach StartActionSkill (the
    press leaves no trace at all), so the raw key is caught here; the same hook the SDK's own
    keybinds use, which fires in every pawn state; and the exec is called directly (confirmed
    working 2026-09-14). The same key while the skill runs: tap = On My Mark, hold = retract."""
    global _action_skill_keys, _ffyl_casts_logged, _skill_key_down_since, _skill_key_hold_done
    try:
        event = int(args.Event)
        if event not in (IE_PRESSED, IE_RELEASED):
            return
        pc = _pc()
        if pc is None or pc.Pawn is None or not _ours(pc):
            return
        if _action_skill_keys is None:
            _action_skill_keys = _action_skill_key_names(pc)
            diag.info(f"{TAG} action skill key(s): {sorted(_action_skill_keys)}")
        if str(args.Key) not in _action_skill_keys:
            return
        from . import digijack
        if event == IE_RELEASED:
            was_down, was_done = _skill_key_down_since, _skill_key_hold_done
            _skill_key_down_since, _skill_key_hold_done = 0.0, False
            if was_down and not was_done and not digijack._active and _casting_remotely(pc) and not pc.Pawn.bIsInjured:
                from . import coopnet
                coopnet.send_input("tap|" + _view_string(pc))   # the host checks On My Mark's points, its one use, the mark
                return
            if was_down and not was_done and _casting_here(pc) and not pc.Pawn.bIsInjured:
                t = time.perf_counter()
                for handler in list(ACTION_TAP_HANDLERS):
                    try:
                        took = handler(pc)
                    except Exception:  # noqa: BLE001
                        took = False
                        _log_once("taphandler", "action-skill tap handler failed: " + traceback.format_exc().splitlines()[-1])
                    t = diag.section("skills.key tap handler " + getattr(handler, "__module__", "?").split(".")[0], t)
                    if took:
                        return
                if not _omm_used and grade("OnMyMark", pc) > 0:      # the tap: On My Mark
                    _on_my_mark(pc)
                    diag.section("skills.key On My Mark (first grenades)", t)
            return
        if not pc.Pawn.bIsInjured:
            if merger_key_during_move(pc):
                return
            if _casting_here(pc) or _casting_remotely(pc):
                _skill_key_down_since, _skill_key_hold_done = time.monotonic(), False
            return
        if grade("Potential", pc) <= 0:
            return
        if _ffyl_casts_logged < 6:
            _ffyl_casts_logged += 1
            diag.info(f"{TAG} Potential: action skill pressed in FFYL -> StartActionSkill()")
        pc.StartActionSkill()
    except Exception:
        _log_once("ffyl:cast", "Potential FFYL cast failed: " + traceback.format_exc().splitlines()[-1])


ALL_HOOKS = (_on_inspire_talk, _on_early_nova_skill_event, _on_menu_closed, _on_challenge_complete, _on_currency_changed, _on_picked_up, _on_cash_stat, _on_equipped, _on_weapon_changed, _on_can_afford_grenade, _on_integrity_conditional, _on_display_damage, _on_input_key, _on_customization_cache, _on_explode, _on_skill_killed_enemy, _on_cause_damage, _on_ci_spawn, _on_ci_destroy, _on_delegation_spawn, _on_merger_beam, _on_generic_revive, _on_second_wind, _on_throw_grenade, _on_grenade_mod_spawn, _on_ls_spawn_node, _on_badass_blast_pre, _on_badass_blast, _on_dialog_event, _on_player_pawn_damaged, _on_remote_inspire, _on_stop_talking, _on_dialog_chatter, _on_graph_chatter, _on_clone_amp_pre, _on_clone_amp_damage, _on_clone_amp_projectile, _on_ci_projectile, _on_ci_compare_object, _on_ci_compare_float, _on_winning_kill, _on_attempt_status, _on_remote_event,
             _on_mip_reset_node, _on_skill_deactivate, _on_skill_activate, _on_yhms_projectile, _on_refill_weapon,
             _on_merger_move_start, _on_merger_destroy_beams, _on_ci_activate)
# probes only: registered while diagnostic logging is on (diag.set_enabled)
diag.DIAG_HOOKS += [_on_start_throw_grenade_skill, _on_grenade_throw_complete, _on_dialog_trigger, _on_dialog_talk, _on_dialog_chance, _on_dialog_talk_act]


# --- console -----------------------------------------------------------------------------------------------------------------
def _command(line: str, cmd_len: int) -> None:
    global FX_MATERIAL_MODE, MERGER_BEAM_SCALE, MERGER_FP_SCALE, MERGER_LEAD, MERGER_ORIGIN_MODE, TTF_BRIDGE, _fx_applied, BADASS_FX_MODE, _badass_fixed
    parts = line[cmd_len:].split()
    pc = _pc()
    out = unrealsdk.logging.info
    try:
        if not parts or parts[0] == "help":
            out(f"{TAG} doppelskill grade <leaf> | attr <path> | set <path> <value> | fire <leaf> <event> | active | gear | shield | sponsored | winning | amp [on|off|mode projectile|damage|multiplier] | level [up|xp|set] | respec | money [piles|reset] | relics | orphandot [fraction] | recallfast [on|off] | beamtest [seconds] | beamwidth <w> | beamfrom <f> <r> <u> | tree | starttime | inspire [inspired|disillusioned] [now]")
        elif parts[0] == "grade":
            out(f"{TAG} {parts[1]}: grade {grade(parts[1], pc)}")
        elif parts[0] == "yhmsshare":
            global YHMS_SHARE
            if len(parts) > 1:
                YHMS_SHARE = parts[1].lower() in ("on", "1", "true")
            out(f"{TAG} You Have My Shield: shield copy to clones {'on' if YHMS_SHARE else 'OFF'}; "
                f"{len(_shield_copies)} cop{'y' if len(_shield_copies) == 1 else 'ies'} tracked")
        elif parts[0] == "comsweep":
            out(f"{TAG} " + com_sweep(pc, parts[1] if len(parts) > 1 else None))
        elif parts[0] == "level":
            from . import progression
            for line in progression.level_command(pc, parts[1:]):
                out(f"{TAG} {line}")
        elif parts[0] == "amp":
            global AMP_ENABLED, AMP_MODE
            if len(parts) > 2 and parts[1] == "mode":
                want = parts[2].lower()
                AMP_MODE = next((m for m in ("projectile", "damage", "multiplier")
                                 if m.startswith(want)), "projectile")
                out(f"{TAG} amp mode: {AMP_MODE}")
            elif len(parts) > 1:
                AMP_ENABLED = parts[1] != "off"
            out(f"{TAG} clone amp shields: {'on' if AMP_ENABLED else 'off'}"
                f"{'' if AMP_ENABLED else ' (the hook returns before it reads anything)'}")
        elif parts[0] == "sponsoredslot":
            kind = parts[1] if len(parts) > 1 else "Triggered"
            out(f"{TAG} " + sponsored_slot(kind, int(parts[2]) if len(parts) > 2 else None))
        elif parts[0] == "shieldfx":
            for line in shield_effects_report(pc):
                out(f"{TAG} {line}")
        elif parts[0] == "delegation":
            for line in delegation_report(pc):
                out(f"{TAG} {line}")
        elif parts[0] == "winning":
            for line in winning_report(pc):
                out(f"{TAG} {line}")
        elif parts[0] == "sponsored":
            for line in sponsored_report(pc):
                out(f"{TAG} {line}")
        elif parts[0] == "shield":
            for line in shield_report(pc):
                out(f"{TAG} {line}")
        elif parts[0] == "tracked":
            out(f"{TAG} " + tracked_report(pc))
        elif parts[0] == "comskills":
            out(f"{TAG} " + comskills_start(float(parts[1]) if len(parts) > 1 else 6.0))
        elif parts[0] == "attr":
            out(f"{TAG} {parts[1]} = {attr_value(parts[1], pc)} (pc) / {attr_value(parts[1], pc.Pawn)} (pawn)")
        elif parts[0] == "set":
            ok = set_attr(parts[1], pc.Pawn, float(parts[2]))
            out(f"{TAG} set {parts[1]} = {parts[2]} on pawn -> {ok}; reads back {attr_value(parts[1], pc.Pawn)}")
        elif parts[0] == "fire":
            out(f"{TAG} fire {parts[1]} {parts[2]} -> {fire(parts[1], parts[2], pc=pc, need_grade=False)}")
        elif parts[0] == "active":
            manager = pc.GetSkillManager()
            names = []
            for s in manager.ActiveSkills:
                try:
                    names.append(f"{s.Definition.Name}({s.SkillInstigator.Name if s.SkillInstigator is not None else '-'}, grade {s.Grade})")
                except Exception:
                    names.append(str(s))
            out(f"{TAG} active skills: {names}")
        elif parts[0] == "gear":
            out(f"{TAG} Hyperion pieces {hyperion_gear_count(pc)}, ammo fullness {ammo_fullness(pc):.2f}, cash {_cash(pc)}")
        elif parts[0] == "recallfast":
            from . import digijack
            if len(parts) > 1:
                digijack.RECALL_FAST_END = parts[1].lower() in ("on", "1", "true", "yes")
            out(f"{TAG} recall ends the skill at once: {'on' if digijack.RECALL_FAST_END else 'off (ends once the clones are gone)'}")
        elif parts[0] == "orphandot":
            global SPONSORED_SELF_DOT
            if len(parts) > 1:
                SPONSORED_SELF_DOT = float(parts[1])
            out(f"{TAG} Sponsored By on the Orphan Maker: element DoT base = {SPONSORED_SELF_DOT:g} x the self-hit")
        elif parts[0] == "relics":
            out(drop_test_relics(pc))
        elif parts[0] == "mergerwindow":
            if len(parts) > 2:
                MERGER_GLITCH_WINDOW[:] = [float(parts[1]), float(parts[2])]
            out(f"{TAG} Infinite Merger window: {MERGER_GLITCH_WINDOW[0]:.2f}-{MERGER_GLITCH_WINDOW[1]:.2f} s after Merger starts "
                f"(its lasers appear at 0.6)")
        elif parts[0] == "spikes":
            # what a Digi-Jack's death does with an Antagonist or Impaler copy, from every live clone, without the death
            from . import digijack
            notes = []
            for entry in list(digijack._clones):
                clone = digijack._resolve_pawn(entry["path"])
                copy = _clone_shield_copy(clone) if clone is not None else None
                note = yhms_special(pc, clone, copy) if clone is not None and copy is not None else None
                notes.append(note or f"{entry['path'].split('.')[-1]}: shield {_shield_def_name(copy) or 'none'}")
            out(f"{TAG} spikes: " + ("; ".join(notes) or "no Digi-Jacks out"))
        elif parts[0] == "shieldwatch":
            out(f"{TAG} " + shield_watch_start(pc, float(parts[1]) if len(parts) > 1 else 30.0))
        elif parts[0] == "beamwidth" and len(parts) > 1:
            global ON_MY_MARK_BEAM_WIDTH
            ON_MY_MARK_BEAM_WIDTH = float(parts[1])
            out(f"{TAG} On My Mark beam width {ON_MY_MARK_BEAM_WIDTH:g}; {omm_beam_test(pc)}")
        elif parts[0] == "beamfrom" and len(parts) > 3:
            ON_MY_MARK_BEAM_FROM[:] = [float(x) for x in parts[1:4]]
            out(f"{TAG} On My Mark beam starts at {ON_MY_MARK_BEAM_FROM} (forward, right, up from the camera); {omm_beam_test(pc)}")
        elif parts[0] == "beamtest":
            out(omm_beam_test(pc, float(parts[1]) if len(parts) > 1 else 5.0))
        elif parts[:2] == ["money", "reset"]:
            # "For the Hoard!" and "Dolla Dolla Bills, Y'all" (GD_Challenges.Economy): their levels are the stat
            # values below, so zeroing the stats puts every level back to be earned (and popped) again
            stats = pc.PlayerStats
            for stat in ("STAT_PLAYER_MOST_DOLLARS_HAD_ON_HAND", "STAT_PLAYER_DOLLARS_COLLECTED_FROM_CASH_DROPS"):
                stats.SetIntStat(StatId=stat, Value=0, bSkipNotify=False)
            out(f"{TAG} For the Hoard! and Dolla Dolla Bills, Y'all reset (their stats are 0); save and reload if the "
                f"challenge screen still shows them done")
        elif parts[0] == "money":
            # cash piles dropped around the player, through Compound Interest's cash dropper (Pool_Money_1 rolls
            # at the player's level): enough pickups to set off the cash challenges' popups
            piles = int(parts[1]) if len(parts) > 1 else 30
            loc = pc.Pawn.Location
            _ci_cash_queue.append((float(loc.X), float(loc.Y), float(loc.Z) + 120.0, None, piles, "doppelskill money"))
            out(f"{TAG} {piles} cash pile(s) on the way; the Money Is Power notes go to the log")
        elif parts[0] == "respec":
            # free respec for testing: the station's own path (ServerPurchaseSkillTreeReset) ends here after charging
            r = pc.ResetSkillTree(bIgnoreProficiencies=True, bIsCharacterLoad=False)
            pri = pc.PlayerReplicationInfo
            pri.GeneralSkillPoints = int(pri.GeneralSkillPoints) + int(r or 0)
            out(f"{TAG} ResetSkillTree() -> {r}; unspent now {pri.GeneralSkillPoints}")
        elif parts[0] == "eval":
            # evaluate a clone-side skill check the way the AI graph does: doppelskill eval Potential
            from . import digijack
            d = skill_def(parts[1])
            ev = unrealsdk.find_object("SkillExpressionEvaluator", "GD_Quince_JackClone.AI.AIDef_DigiJack_BaseAndBadass.AIBehaviorProviderDefinition_0.Behavior_Conditional_31.SkillExpressionEvaluator_0")
            saved = ev.Skill
            ev.Skill = d
            results = []
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is None:
                    continue
                for ctx_name, ctx in (("pawn", pawn), ("mind", pawn.Controller)):
                    try:
                        results.append(f"{ctx_name}={ev.Evaluate(ContextSource=ctx)}")
                    except Exception:
                        results.append(f"{ctx_name}=ERR " + traceback.format_exc().splitlines()[-1])
                break
            results.append(f"pc={ev.Evaluate(ContextSource=pc)}")
            ev.Skill = saved
            out(f"{TAG} SkillExpressionEvaluator({parts[1]}, grade {grade(parts[1], pc)}): {results}")
        elif parts[0] == "cattr":
            # an attribute evaluated on the first live clone (what a clone-context graph variable would read)
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is not None:
                    out(f"{TAG} {parts[1]} on clone {pawn.Name} = {attr_value(parts[1], pawn)}; clone GetMaxHealth={pawn.GetMaxHealth():.0f} "
                        f"shield {pawn.GetShieldStrength():.0f}/{pawn.GetMaxShieldStrength():.0f}; player = {attr_value(parts[1], pc.Pawn)}")
                    break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] == "cskill":
            # the clone damage-bonus skill's inputs, read on the first live clone
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is None:
                    continue
                vals = {}
                for a in ("GD_Quince_JackClone.Misc.Att_DigiJack_HealthBonusFromSkills", "GD_Quince_JackClone.Misc.Att_DigiJack_DamageBonusFromSkills",
                          "GD_Quince_JackClone.Misc.Att_Bolster_TotalHealthBonus", "GD_Quince_JackClone.Misc.Att_PromoteTheRanks_BadassHealthBonus",
                          "D_Attributes.HealthResourcePool.HealthMaxValue", "D_Attributes.HealthResourcePool.HealthCurrentValue"):
                    vals[a.split(".")[-1]] = attr_value(a, pawn)
                active = []
                try:
                    for sk in pc.GetSkillManager().ActiveSkills:
                        inst = sk.SkillInstigator
                        if inst is not None and (inst is pawn.Controller or (getattr(inst, "Pawn", None) is not None and inst.Pawn.Name == pawn.Name)):
                            active.append(f"{sk.Definition.Name} grade {sk.Grade}")
                except Exception:
                    active = ["<unreadable: " + traceback.format_exc().splitlines()[-1] + ">"]
                out(f"{TAG} clone {pawn.Name} maxhp {pawn.GetMaxHealth():.0f} hp {pawn.GetHealth():.0f}: {vals}; clone-instigated skills {active}")
                break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] == "beamorigin":
            if len(parts) >= 4:
                MERGER_ORIGIN[:] = [float(parts[1]), float(parts[2]), float(parts[3])]
                MERGER_ORIGIN_MODE = "camera"
            elif len(parts) == 2 and parts[1] in ("camera", "socket", "tps"):
                MERGER_ORIGIN_MODE = parts[1]
            out(f"{TAG} Merger beam origin: mode {MERGER_ORIGIN_MODE}, camera offsets forward/side/up {MERGER_ORIGIN}, shift {MERGER_SHIFT}, fp scale {MERGER_FP_SCALE}")
        elif parts[0] == "onmymark":
            pc = _pc()
            out(f"{TAG} {_on_my_mark(pc, force=True) if pc is not None else 'no player'}")
        elif parts[0] == "lasercone":
            if len(parts) >= 4:
                LS_CONE_OFFSET[:] = [float(parts[1]), float(parts[2]), float(parts[3])]
                try:
                    node = unrealsdk.find_object("Behavior_SpawnProjectile", LS_SPAWN_NODE)
                    node.bSpawnFromContextViewLocation = True
                    node.RelativeLocation = unrealsdk.make_struct("Vector", X=LS_CONE_OFFSET[0], Y=LS_CONE_OFFSET[1], Z=LS_CONE_OFFSET[2])
                except Exception as e:
                    out(f"{TAG} cone node: {str(e)[:80]}")
            out(f"{TAG} Laser Surplus cone offset forward/right/up {LS_CONE_OFFSET}")
        elif parts[0] == "barragefx":
            if len(parts) >= 2 and parts[1] in ("psc", "inflight", "both"):
                BADASS_FX_MODE = parts[1]
                _badass_fixed = False
                _badass_fixups()
            out(f"{TAG} barrage comet mode {BADASS_FX_MODE} (psc = the projectiles' own component, inflight = BL2's InFlightEffects slot, both)")
        elif parts[0] == "fxboost":
            if len(parts) >= 3 and parts[1] in FX_RUNTIME_MATERIALS:
                FX_COLOR_BOOST[parts[1]] = float(parts[2])
                _fx_materials.pop(parts[1], None)
                _fx_applied = False
                out(f"{TAG} {parts[1]} boost {FX_COLOR_BOOST[parts[1]]}; {_fx_apply_materials()}")
            else:
                out(f"{TAG} boosts {FX_COLOR_BOOST}; fxboost <{'|'.join(FX_RUNTIME_MATERIALS)}> <x>")
        elif parts[0] == "comet":
            out(f"{TAG} comet emitters: {_comet_enable(parts[1:] or ['all'])} (ribbon flare embers burst bolts all)")
        elif parts[0] == "ported":
            # A/B: the ported material against the stock-master approximation, live. The hue sweep
            # and the p_color boost only ever applied to the approximation, so they come back with
            # it and go quiet again when the real shader is in use.
            global _PORTED_OFF
            if len(parts) >= 2:
                _PORTED_OFF = parts[1] in ("off", "0", "false")
                PORTED_MATERIALS.clear()
                if not _PORTED_OFF:
                    PORTED_MATERIALS.update(PORTED_DEFAULT)
                _fx_materials.clear()
                _fx_applied = False
                out(f"{TAG} ported materials {'off' if _PORTED_OFF else 'on'}; {_fx_apply_materials()}")
            else:
                out(f"{TAG} ported materials {'off' if _PORTED_OFF else 'on'}: {PORTED_DEFAULT}")
        elif parts[0] == "rainbowsweep":
            global RAINBOW_SWEEP_SECONDS
            if len(parts) >= 2:
                if parts[1] == "off":
                    RAINBOW_SWEEP_SECONDS = 0.0
                    _reset_rainbow(_fx_materials.get("Rainbow") or "")
                else:
                    try:
                        RAINBOW_SWEEP_SECONDS = float(parts[1])
                    except ValueError:
                        out(f"{TAG} rainbowsweep <seconds|off>")
                        return
            out(f"{TAG} rainbow sweep {RAINBOW_SWEEP_SECONDS:.2f}s" + (" (off)" if not RAINBOW_SWEEP_SECONDS else ""))
        elif parts[0] == "inspiredelay":
            global INSPIRE_TEXT_DELAY
            if len(parts) >= 2:
                try:
                    INSPIRE_TEXT_DELAY = float(parts[1])
                except ValueError:
                    out(f"{TAG} inspiredelay <seconds>")
                    return
            out(f"{TAG} text delay {INSPIRE_TEXT_DELAY:.2f}s; {_delay_inspire_text() or 'unchanged'}")
        elif parts[0] == "inspirediag":
            _inspirediag(pc, out)
        elif parts[0] == "fxdump":
            out(f"{TAG} {_fx_dump(parts[1] if len(parts) > 1 else 'Part_BadassShot_Fire')}")
        elif parts[0] == "fxmaster":
            if len(parts) >= 3 and parts[1] in FX_RUNTIME_MATERIALS and parts[2] in FX_MASTERS:
                master, params = FX_RUNTIME_MATERIALS[parts[1]]
                FX_RUNTIME_MATERIALS[parts[1]] = (FX_MASTERS[parts[2]], params)
                _fx_materials.pop(parts[1], None)
                _fx_applied = False
                out(f"{TAG} {parts[1]} master -> {parts[2]}; {_fx_apply_materials()}")
            else:
                out(f"{TAG} fxmaster <{'|'.join(FX_RUNTIME_MATERIALS)}> <{'|'.join(FX_MASTERS)}>")
        elif parts[0] == "laserboost":
            if len(parts) >= 2:
                if parts[1] == "off":                # do not touch p_color at all
                    FX_COLOR_BOOST.clear()
                else:
                    FX_COLOR_BOOST["LaserTracer"] = float(parts[1])
                    if len(parts) >= 3:
                        FX_COLOR_BOOST["LaserImpact"] = float(parts[2])
                _fx_materials.clear()
                _fx_applied = False
            out(f"{TAG} laser colour boost {FX_COLOR_BOOST}; {_fx_apply_materials()}")
        elif parts[0] == "fxmat":
            if len(parts) >= 2 and parts[1] in ("runtime", "package"):
                FX_MATERIAL_MODE = parts[1]
            _fx_applied = False
            out(f"{TAG} {_fx_apply_materials()}")
        elif parts[0] == "inspirefx":
            pc = _pc()
            if pc is not None:
                _inspire_screen(pc, parts[1] if len(parts) >= 2 else "inspired")
                out(f"{TAG} screen effect requested")
        elif parts[0] == "inspire":
            # the whole thing on demand, rather than waiting for the skill to roll it: the line
            # through the pawn's own dialog component, then the word and the sting behind it exactly
            # as the skill queues them, so the timing is the real timing. `now` skips the line.
            pc = _pc()
            which = parts[1] if len(parts) >= 2 and parts[1].startswith("dis") else "inspired"
            if pc is None or pc.Pawn is None:
                out(f"{TAG} inspire: no pawn")
            elif "now" in parts:
                _play_ak(pc.Pawn, INSPIRE_AK["rainbow" if which == "inspired" else "disillusioned"])
                _inspire_screen(pc, which)
                if which == "inspired":
                    start_rainbow_sweep()
                out(f"{TAG} inspire ({which}): effect fired, no line")
            else:
                from . import digijack
                tag = INSPIRATION_TAG if which == "inspired" else DISILLUSION_TAG
                spoke = digijack._trigger_dialog(pc.Pawn, tag, which)
                queue_inspire_screen(which, "doppelskill inspire")
                out(f"{TAG} inspire ({which}): line {'triggered' if spoke else 'FAILED'}; "
                    f"the word follows when it ends (inspire {which} now = effect only)")
        elif parts[0] == "beamk":
            if len(parts) >= 2:
                MERGER_FP_SCALE = float(parts[1])
            out(f"{TAG} Merger first-person socket lateral scale {'from the FOVs' if MERGER_FP_SCALE <= 0 else MERGER_FP_SCALE} "
                f"(now {merger_fp_scale(pc):.2f}; 0 = from the FOVs)")
        elif parts[0] == "beamprobe":
            global _beam_probe_frames, _beam_probe_dumped
            _beam_probe_frames = int(parts[1]) if len(parts) > 1 else 40
            _beam_probe_dumped = False
            out(f"{TAG} beamprobe: the next {_beam_probe_frames} Merger frames go to the log (camera space: forward, right, up)")
        elif parts[0] == "beampush":
            global MERGER_PUSH_TAU
            if len(parts) > 1:
                MERGER_PUSH_TAU = float(parts[1])
            out(f"{TAG} Merger beam push-out smoothing {MERGER_PUSH_TAU:.2f} s (0 = re-measured raw every frame)")
        elif parts[0] == "beamattach":
            global MERGER_ATTACH_BEAMS
            if len(parts) > 1:
                MERGER_ATTACH_BEAMS = parts[1].lower() in ("on", "1", "true")
                _release_own_beams()
            out(f"{TAG} Merger beams hang on the first-person arms: {'on' if MERGER_ATTACH_BEAMS else 'off (drawn free from the tick)'}")
        elif parts[0] == "beamown":
            global MERGER_OWN_BEAMS
            if len(parts) > 1:
                MERGER_OWN_BEAMS = parts[1].lower() in ("on", "1", "true")
                if not MERGER_OWN_BEAMS:
                    _release_own_beams()
            out(f"{TAG} Merger beams drawn by the mod: {'on' if MERGER_OWN_BEAMS else 'off (the manager draws them)'}")
        elif parts[0] == "beamsway":
            global MERGER_SWAY_TAU
            if len(parts) >= 2:
                MERGER_SWAY_TAU = float(parts[1])
            out(f"{TAG} Merger beam start sway smoothing {MERGER_SWAY_TAU:.2f} s (0 = the start follows the arms' sway)")
        elif parts[0] == "beamshift":
            if len(parts) >= 4:
                MERGER_SHIFT[:] = [float(parts[1]), float(parts[2]), float(parts[3])]
            out(f"{TAG} Merger beam shift forward/side/up {MERGER_SHIFT}")
        elif parts[0] == "beamscale":
            if len(parts) >= 2:
                MERGER_BEAM_SCALE = float(parts[1])
            out(f"{TAG} Merger beam particle scale {MERGER_BEAM_SCALE}")
        elif parts[0] == "beams":
            # every light-projectile manager's live beams, on any instance: who owns it, where each beam
            # starts / points / ends and what its source and target data say (co-op: does a client mirror
            # the host's Merger beams, and from which socket?)
            from .netdiag import replicated
            replicated("WillowLightProjectileManager")
            n = 0
            for mgr in unrealsdk.find_all("WillowLightProjectileManager", exact=False):
                try:
                    owner = mgr.PawnOwner
                    if mgr.Name.startswith("Default__") or owner is None:
                        continue
                    beams = []
                    for i, b in enumerate(mgr.MyBeams):
                        core = b.CoreBeamState
                        if not core.bEnabled:
                            continue
                        src, tgt = core.SourceData, core.TargetData
                        beams.append(f"[{i}] src {src.Actor.Name if src.Actor is not None else None}:{src.Socket} weapon={bool(src.bUseWeapon)} off {_v(src.Offset)}"
                                     f" tgt {tgt.Actor.Name if tgt.Actor is not None else None}:{tgt.Socket} locked={bool(core.bLocked)}"
                                     f" start {_v(b.CurrentStartPoint)} dir {_v(b.CurrentDirection)} end {_v(b.CurrentEndPoint)} part {b.PartRef.Name if b.PartRef is not None else None}")
                    n += 1
                    out(f"{TAG} {mgr.Name} owner {owner.Name} (role {int(owner.Role)}, remote role {int(owner.RemoteRole)}): {len(beams)} live beam(s)")
                    for line in beams:
                        out(f"{TAG}    {line}")
                except Exception:  # noqa: BLE001
                    out(f"{TAG} beam probe: " + traceback.format_exc().splitlines()[-1])
            out(f"{TAG} {n} manager(s) with an owner")
        elif parts[0] == "beamlead":
            if len(parts) >= 2:
                MERGER_LEAD = float(parts[1])
            out(f"{TAG} Merger beam origin lead {MERGER_LEAD} frames (last frame {_last_dt * 1000:.1f} ms)")
        elif parts[0] == "clonehp":
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is None:
                    continue
                vals = {a.split(".")[-1]: attr_value(a, pawn) for a in ("D_Attributes.HealthResourcePool.HealthMaxValue", "D_Attributes.HealthResourcePool.HealthCurrentValue",
                                                                        "GD_Quince_JackClone.Misc.Att_DigiJack_HealthScale", "GD_Quince_JackClone.Misc.Att_Bolster_TotalHealthBonus")}
                out(f"{TAG} {pawn.Name}: GetMaxHealth {pawn.GetMaxHealth():.0f} GetHealth {pawn.GetHealth():.0f} shield {pawn.GetShieldStrength() if hasattr(pawn, 'GetShieldStrength') else '-'} attrs {vals}")
        elif parts[0] == "seq":
            # sequence enabled states on the first live clone's death + AI graphs
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is not None:
                    out(f"{TAG} {pawn.Name}: {clone_sequence_states(pawn)}")
                    break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] in ("despawn", "cloneevent"):
            # raise a body-death graph event on the first live clone: doppelskill despawn [EventName]
            from . import digijack
            event = parts[1] if len(parts) > 1 else "CE_CloneDespawned"
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is not None:
                    out(f"{TAG} {event} on {pawn.Name}: {fire_on_clone(pawn, event)}")
                    break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] == "flag":
            # doppelskill flag <FlagDefinition path> <0|1> ; set an AI flag on the first live clone
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is not None:
                    out(f"{TAG} {parts[1]} = {parts[2]} on {pawn.Name}: {set_flag(parts[1], pawn, parts[2] not in ('0', 'false', 'False'))}")
                    break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] == "seqon":
            from . import digijack
            for entry in list(digijack._clones):
                pawn = digijack._resolve_pawn(entry["path"])
                if pawn is not None:
                    out(f"{TAG} enable {parts[1]} on {pawn.Name}: {enable_clone_sequence(pawn, parts[1])}; now {clone_sequence_states(pawn)}")
                    break
            else:
                out(f"{TAG} no live clone")
        elif parts[0] == "ttfbridge":
            TTF_BRIDGE = parts[1].lower() in ("on", "1", "true")
            out(f"{TAG} Take Their Freedom clone-kill bridge {'on' if TTF_BRIDGE else 'off'}")
        elif parts[0] == "loadpkg":
            mgr = pc.GetWillowGlobals().GetPlayerPawnDataManager()
            r = mgr.LoadDataPackage(PackageName="GD_Doppelganger_Streaming", PlayerContext=pc)
            t = mgr.LoadSkillTreeDefinition(SkillTreeDataPath=pc.PlayerClass.SkillTreePath, PlayerContext=pc)
            out(f"{TAG} LoadDataPackage -> {r}; LoadSkillTreeDefinition now -> {t}")
        elif parts[0] == "tree":
            out(f"{TAG} " + tree_report(pc))
        elif parts[0] == "commitment":
            # one-shot probe of the stack pipeline: the live Commitment / Feedback instances, the stack
            # attribute before and after one CE_AddCommitmentStack (the graph's SimpleMath runs inside
            # the kernel call), then the removal event; `doppelskill commitment` in any game mode
            stacks_path = "Quince_Doppel_GreaterGood.Misc.Att_Commitment_CurrentStacks"
            states = {0: "Deactivated", 1: "Active", 2: "Paused"}
            for leaf in ("Commitment", "Commitment_Feedback"):
                sk = active_skill(leaf, pc)
                out(f"{TAG} {leaf}: " + ("no live instance" if sk is None else f"state {states.get(int(sk.SkillState), sk.SkillState)}, grade {sk.Grade}, instigator {sk.SkillInstigator.Name if sk.SkillInstigator is not None else '-'}"))
            for ctx, label in ((pc, "pc"), (pc.Pawn, "pawn")):
                out(f"{TAG} stacks before ({label}): {attr_value(stacks_path, ctx)}")
            ok = fire("Commitment", "CE_AddCommitmentStackFromClone", pc=pc)
            out(f"{TAG} CE_AddCommitmentStackFromClone -> {ok}; stacks now pc={attr_value(stacks_path, pc)} pawn={attr_value(stacks_path, pc.Pawn)}")
            if len(parts) > 1 and parts[1] == "clear":
                ok = fire("Commitment", "CE_RemoveCommitmentStack", pc=pc)
                out(f"{TAG} CE_RemoveCommitmentStack -> {ok}; stacks now pc={attr_value(stacks_path, pc)}")
        elif parts[0] == "regen":
            # Supply and Demand: every player's live health regen (HP/s) next to their health. The ally
            # entry evaluates in Jack's context (as Maya's Elated does), so it is 0 while Jack is at full
            # health whatever the ally's own health is. Pawns come from the live controllers only:
            # find_all("WillowPlayerPawn") also lists torn-down pawns, and a native call on one is a crash.
            regen_path = "D_Attributes.HealthResourcePool.HealthPassiveRegenerationRate"
            for pri in pc.WorldInfo.GRI.PRIArray:
                try:
                    ctrl = pri.Owner if pri is not None else None
                    pawn = ctrl.Pawn if ctrl is not None else None
                    if pawn is None:
                        out(f"{TAG} {pri.PlayerName if pri is not None else '?'}: no pawn")
                        continue
                    hp = attr_value("D_Attributes.HealthResourcePool.HealthCurrentValue", pawn)
                    mx = attr_value("D_Attributes.HealthResourcePool.HealthMaxValue", pawn)
                    name_id = pri.CharacterNameIdDef
                    who = str(name_id.CharacterName) if name_id is not None else "?"
                    out(f"{TAG} {pri.PlayerName} ({who}, {pawn.Name}): health {hp}/{mx}, regen {attr_value(regen_path, pawn)} HP/s")
                except Exception:  # noqa: BLE001
                    out(f"{TAG} regen probe: " + traceback.format_exc().splitlines()[-1])
        elif parts[0] == "instdata":
            # Integrity's first-hit memory: the 'DamagedBy' instance data its graph writes on each enemy
            # Jack has hit. Lists every live AI pawn carrying instance data.
            found = 0
            for pawn in unrealsdk.find_all("WillowAIPawn"):
                try:
                    if pawn.Name.startswith("Default__") or pawn.IsDead():
                        continue
                    data = list(pawn.InstanceData)
                except Exception:  # noqa: BLE001
                    continue
                if not data:
                    continue
                found += 1
                out(f"{TAG} {pawn.Name} ({pawn.Class.Name}): " + "; ".join(str(d) for d in data))
            out(f"{TAG} {found} AI pawn(s) carry instance data")
        elif parts[0] == "starttime":
            from . import digijack
            att = attr_value("GD_Doppelganger_Skills.Skills_Misc.Att_DigiJack_StartTime", pc)
            out(f"{TAG} skill StartTime={action_skill_start_time(pc)} hook start={digijack._started_time} now={pc.WorldInfo.TimeSeconds:.2f} "
                f"Att_DigiJack_StartTime(pc)={att} BFF grade={grade('BestFootForward', pc)}")
        else:
            out(f"{TAG} unknown doppelskill subcommand {parts[0]!r}")
    except Exception:
        out(f"{TAG} doppelskill failed: " + traceback.format_exc())


add_command("doppelskill", _command)

# --- doppelcheck ---------------------------------------------------------------------------------
# One dump of the runtime state behind the four release-day reports that survived their first fix
# (cosmetics not saving, the action-skill key, Collaborate on the clones, the buff-tray slot), so the
# next change is made against what the game actually holds instead of against what it ought to hold.
def _check_keys(pc, out) -> None:
    for only_kb in (True, False):
        try:
            n = pc.PlayerInput.GetKeyForAction(ActionName=ACTION_SKILL_INPUT_ACTION, bOnlyKeyboardMouse=only_kb, bNoRemapping=False)
            out(f"{TAG} check keys: GetKeyForAction('ActionSkill', keyboard only={only_kb}) -> {n!r}")
        except Exception as e:  # noqa: BLE001
            out(f"{TAG} check keys: GetKeyForAction(keyboard only={only_kb}) failed: {str(e)[:120]}")
    try:
        binds = list(pc.PlayerInput.Bindings)
    except Exception:
        out(f"{TAG} check keys: PlayerInput.Bindings unreadable: " + traceback.format_exc().splitlines()[-1])
        return
    out(f"{TAG} check keys: {len(binds)} bindings; ones mentioning a skill or action:")
    for b in binds:
        try:
            name, cmd = str(b.Name), str(b.Command)
        except Exception:
            continue
        if "skill" in cmd.lower() or "action" in cmd.lower():
            out(f"{TAG}    {name!r} -> {cmd!r}")
    out(f"{TAG} check keys: scan finds {sorted(_action_skill_key_names(pc))}; cache holds {sorted(_action_skill_keys) if _action_skill_keys else None}")


def _check_cosmetics(pc, out) -> None:
    pri = None
    try:
        pri = pc.PlayerReplicationInfo
    except Exception:
        pass
    for field in ("RemoteCustomizations", "LocalCustomizations"):
        try:
            arr = getattr(pri, field)
            out(f"{TAG} check cosmetics: PRI.{field} = {[c._path_name() if c is not None else None for c in arr]}")
        except Exception as e:  # noqa: BLE001
            out(f"{TAG} check cosmetics: PRI.{field} unreadable: {e}")
    try:
        pawn = pc.Pawn
        out(f"{TAG} check cosmetics: pawn head={pawn.HeadCustomizationData._path_name() if pawn.HeadCustomizationData is not None else None} "
            f"skin={pawn.SkinCustomizationData._path_name() if pawn.SkinCustomizationData is not None else None}")
    except Exception as e:  # noqa: BLE001
        out(f"{TAG} check cosmetics: pawn data unreadable: {e}")
    for path in ("GD_Doppelganger_Package.Profile_Doppelganger",):
        try:
            save = unrealsdk.find_object("PlayerSaveGame", path)
            out(f"{TAG} check cosmetics: {path}.AppliedCustomizations = "
                f"{[c._path_name() if c is not None else None for c in save.AppliedCustomizations]}")
        except Exception as e:  # noqa: BLE001
            out(f"{TAG} check cosmetics: {path}: {e}")
    try:
        sg = pc.GeneratePlayerSaveGame()
        out(f"{TAG} check cosmetics: GeneratePlayerSaveGame() -> {sg._path_name() if sg is not None else None}; "
            f"AppliedCustomizations = {[c._path_name() if c is not None else None for c in sg.AppliedCustomizations]}")
    except Exception as e:  # noqa: BLE001
        out(f"{TAG} check cosmetics: GeneratePlayerSaveGame failed: {str(e)[:120]}")


def _check_collaborate(pc, out) -> None:
    from . import digijack
    out(f"{TAG} check collaborate: grade {grade('Collaborate', pc)}; "
        f"stacks {attr_value('Quince_Doppel_GreaterGood.Misc.Att_Collaborate_CurrentStacks', pc.Pawn)} (pawn) / "
        f"{attr_value('Quince_Doppel_GreaterGood.Misc.Att_Collaborate_CurrentStacks', pc)} (pc)")
    for entry in list(digijack._clones):
        pawn = digijack._resolve_pawn(entry.get("path"))
        if pawn is None:
            continue
        out(f"{TAG} check collaborate: {pawn.Name} damage bonus "
            f"{attr_value('GD_Quince_JackClone.Misc.Att_DigiJack_DamageBonusFromSkills', pawn)}, "
            f"health bonus {attr_value('GD_Quince_JackClone.Misc.Att_DigiJack_HealthBonusFromSkills', pawn)}")
    out(f"{TAG} check collaborate: push now -> {push_collaborate_to_clones('doppelcheck', force=True)}")
    from . import digijack as _dj
    for entry in list(_dj._clones):
        pawn = _dj._resolve_pawn(entry.get("path"))
        if pawn is not None:
            out(f"{TAG} check collaborate: {pawn.Name} damage bonus now "
                f"{attr_value('GD_Quince_JackClone.Misc.Att_DigiJack_DamageBonusFromSkills', pawn)}")


def _check_hud(pc, out) -> None:
    for path in ("Quince_Doppel_Hero.Skills.JacksCache", "GD_Doppelganger_Skills.Hero.JacksCache",
                 "Quince_Doppel_FreeEnterprise.Skills.SponsoredBy", "Quince_Doppel_Hero.Skills.JacksCache_Feedback"):
        try:
            d = unrealsdk.find_object("SkillDefinition", path)
            out(f"{TAG} check hud: {path} slot={int(d.TrackedSkillHUDSlot)}")
        except Exception as e:  # noqa: BLE001
            out(f"{TAG} check hud: {path}: {str(e)[:80]}")
    try:
        manager = pc.GetSkillManager()
        for sk in manager.ActiveSkills:
            try:
                d = sk.Definition
                if d is None or int(d.TrackedSkillHUDSlot) == 0:
                    continue
                out(f"{TAG} check hud: active {d.Name} slot={int(d.TrackedSkillHUDSlot)} grade={sk.Grade}")
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        out(f"{TAG} check hud: active skills unreadable: {e}")


def _check_points(pc, out) -> None:
    from . import progression
    try:
        pri = pc.PlayerReplicationInfo
        tree = pc.PlayerSkillTree
        spent = int(tree.GetSkillPointsSpentInTree()) if tree is not None else -1
        out(f"{TAG} check points: level {int(pri.ExpLevel)}, available {int(pri.GeneralSkillPoints)}, spent {spent}; "
            f"settings first={progression.first_point_level()} cap={progression.total_cap()} tier={progression.tier_points()}")
    except Exception:
        out(f"{TAG} check points: " + traceback.format_exc().splitlines()[-1])


def _check_tree(pc, out) -> None:
    """The live skill tree beside the definitions progression.apply_tiers writes into.

    Changing the single-point row setting only shows up after a save and quit. The first dump found
    why: PlayerSkillTree.Tiers caches BranchPointsToUnlockTier, a running total worked out from the
    definitions when the tree is initialised, and nothing recomputes it in place. This prints the
    definitions, the setting, and every cached tier in full, which is what writing the update needs
    (user, 2026-09-22)."""
    from . import progression
    out(f"{TAG} check tree: setting is {progression.tier_points()} point(s) in the single row; "
        f"apply now -> {progression.apply_tiers(force=True)}")
    for path in progression.BRANCHES:
        try:
            branch = unrealsdk.find_object("SkillTreeBranchDefinition", path)
            out(f"{TAG} check tree: {branch.Name} definition tiers "
                f"{[int(t.PointsToUnlockNextTier) for t in branch.Tiers]}")
        except Exception:  # noqa: BLE001
            out(f"{TAG} check tree: {path}: " + traceback.format_exc().splitlines()[-1][:90])
    tree = pc.PlayerSkillTree
    if tree is None:
        out(f"{TAG} check tree: no live tree on the controller")
        return
    for name in ("Branches", "Tiers"):
        try:
            rows = list(getattr(tree, name))
        except Exception:  # noqa: BLE001
            out(f"{TAG} check tree: {name} unreadable")
            continue
        out(f"{TAG} check tree: {name}[{len(rows)}]")
        for i, row in enumerate(rows):
            out(f"{TAG} check tree:   {name}[{i}] {row}")


def _doppelcheck_command(line: str, cmd_len: int) -> None:
    out = unrealsdk.logging.info
    pc = _pc()
    if pc is None:
        out(f"{TAG} check: no player controller")
        return
    for name, fn in (("keys", _check_keys), ("cosmetics", _check_cosmetics), ("collaborate", _check_collaborate),
                     ("hud", _check_hud), ("points", _check_points), ("tree", _check_tree)):
        arg = line[cmd_len:].strip().lower()
        if arg and arg != name:
            continue
        try:
            fn(pc, out)
        except Exception:
            out(f"{TAG} check {name} failed: " + traceback.format_exc().splitlines()[-1])


add_command("doppelcheck", _doppelcheck_command)

# --- doppelskill inspirediag -----------------------------------------------------------------
# Two questions this answers, after three rounds of reasoning got both wrong. The particles draw as
# flat coloured quads, which is what an additive master looks like when its texture parameter never
# took and the stub white is being tinted by the colour module; reading the parameters back off the
# live instance says whether they took. And the word has to appear when the line ends rather than
# after a fixed wait, so this lists what the dialog component actually offers to tell us that.
def _inspire_materials(out) -> None:
    for name in ("Sparkle", "Rainbow", "StarSpark", "InspiredText"):
        path = _fx_materials.get(name)       # the cache holds object paths, not the objects
        mic = None
        if path:
            try:
                mic = unrealsdk.find_object("MaterialInstanceConstant", path)
            except Exception:  # noqa: BLE001
                mic = None
        if mic is None:
            out(f"{TAG} inspirediag: {name}: no runtime instance ({path or 'never built'})")
            continue
        try:
            parent = mic.Parent
            out(f"{TAG} inspirediag: {name} = {mic.Name}, parent {parent.Name if parent is not None else None}")
        except Exception:  # noqa: BLE001
            out(f"{TAG} inspirediag: {name}: unreadable")
            continue
        try:
            rows = list(mic.TextureParameterValues)
            out(f"{TAG} inspirediag: {name}: {len(rows)} texture parameter(s) on the instance")
            for r in rows:
                tex = r.ParameterValue
                bits = f"{r.ParameterName} -> {tex._path_name() if tex is not None else None}"
                if tex is not None:
                    for field in ("SizeX", "SizeY", "bForceMiplevelsToBeResident", "ResidentMips", "RequestedMips", "NeverStream"):
                        try:
                            bits += f" {field}={getattr(tex, field)}"
                        except Exception:  # noqa: BLE001
                            pass
                out(f"{TAG} inspirediag:    {bits}")
        except Exception:  # noqa: BLE001
            out(f"{TAG} inspirediag: {name}: parameters unreadable: " + traceback.format_exc().splitlines()[-1][:80])


def _inspire_emitters(out) -> None:
    for path in ("Quince_FX.Particles.Part_Inspire_Screen",):
        try:
            ps = unrealsdk.find_object("ParticleSystem", path)
        except Exception:  # noqa: BLE001
            out(f"{TAG} inspirediag: {path} not resident")
            continue
        for em in ps.Emitters:
            if em is None:
                continue
            try:
                lod = em.LODLevels[0]
                req = lod.RequiredModule
                mat = req.Material
                out(f"{TAG} inspirediag: emitter {em.EmitterName} enabled={bool(lod.bEnabled)} "
                    f"material={mat._path_name() if mat is not None else None} "
                    f"subimages={int(req.SubImages_Horizontal)}x{int(req.SubImages_Vertical)} "
                    f"interp={req.InterpolationMethod} screenalign={req.ScreenAlignment}")
            except Exception:  # noqa: BLE001
                out(f"{TAG} inspirediag: emitter dump failed: " + traceback.format_exc().splitlines()[-1][:80])


def _inspire_dialog(pc, out) -> None:
    """What the speaking side offers, so the word can follow the line instead of a fixed wait."""
    pawn = pc.Pawn if pc is not None else None
    comp = None
    for field in ("DialogComponent", "Dialog", "TalkComponent"):
        try:
            comp = getattr(pawn, field)
        except Exception:  # noqa: BLE001
            continue
        if comp is not None:
            out(f"{TAG} inspirediag: pawn.{field} = {comp.Name} ({comp.Class.Name})")
            break
    if comp is None:
        out(f"{TAG} inspirediag: no dialog component on the pawn; searching the world")
        try:
            found = [o for o in unrealsdk.find_all("GearboxDialogComponent") if not o.Name.startswith("Default__")]
            out(f"{TAG} inspirediag: {len(found)} GearboxDialogComponent(s) live; first {found[0].Name if found else None}")
            comp = found[0] if found else None
        except Exception:  # noqa: BLE001
            pass
    if comp is None:
        return
    cls = comp.Class
    while cls is not None:
        names = []
        try:
            names = sorted(str(f.Name) for f in cls._fields()
                           if any(k in str(f.Name).lower() for k in ("talk", "speak", "dialog", "line", "duration", "finish")))
        except Exception:  # noqa: BLE001
            pass
        if names:
            out(f"{TAG} inspirediag: {cls.Name}: {', '.join(names)}")
        cls = cls.SuperField


def _inspirediag(pc, out) -> None:
    _inspire_materials(out)
    _inspire_emitters(out)
    _inspire_dialog(pc, out)


