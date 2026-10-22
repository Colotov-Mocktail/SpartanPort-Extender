"""
progression.py; how fast Jack's trees fill. Three settings, because the two games disagree.

TPS opens the next row of a tree after one point in its single-skill row, gives the action skill
and the first point at level 3, and stops at level 70 (68 points). BL2 wants five points in every
row, starts at 5, and runs to 80 (76 points). The tier numbers live in the tree data
(SkillTreeBranchDefinition.Tiers[n].PointsToUnlockNextTier); the points per level come from
GD_Globals.Skills.INI_SkillPointsPerLevelUp, which every class in BL2 shares, so the level rules
are applied to Jack on the level-up itself rather than by editing that.
"""
import time
import traceback

import unrealsdk
from mods_base import get_pc, hook
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import diag

TAG = "[Doppelganger] progression:"
BRANCHES = ("Quince_Doppel_Hero.SkillTree.Branch_TheHeroofthisStory",
            "Quince_Doppel_GreaterGood.SkillTree.Branch_GreaterGood",
            "Quince_Doppel_FreeEnterprise.SkillTree.Branch_FreeEnterprise")
SINGLE_POINT_TIER = 2          # the third tier of each tree: one point in TPS, five in BL2
BL2_FIRST_POINT_LEVEL = 5      # GD_Globals.Skills.INI_SkillPointsPerLevelUp's condition
TPS_TOTAL = 68                 # levels 3..70
TIER_CHOICES = ("1 point (TPS)", "5 points (BL2)")
LEVEL_CHOICES = ("3 (TPS)", "5 (BL2)")

_tier_option = None
_level_option = None
# mods_base loads the saved settings by assigning option.value, which runs on_change_anytime BEFORE the value
# updates: a saved "5 points (BL2)" arrived as a change from the default "1 point (TPS)", owed a refund, and the first
# Jack load of every launch reset the whole tree (a player, 2026-10-05: "skill points reset on every save quit").
# Until __init__ calls settings_ready() after build_mod, a callback only applies the value, never refunds.
_settings_ready = False


def settings_ready() -> None:
    global _settings_ready
    _settings_ready = True
_total_option = None
_tiers_applied: str | None = None       # the choice the resident branches carry, None after a level change
_points_before: dict[str, int] = {}     # controller path -> GeneralSkillPoints when the level-up began


def make_options() -> list:
    global _tier_option, _level_option, _total_option
    from mods_base import BoolOption, SpinnerOption
    _tier_option = SpinnerOption(
        "Single-point row", TIER_CHOICES[0], list(TIER_CHOICES),
        description="Points the single-skill rows need to open the next row. TPS 1, BL2 5. Raising it refunds.",
        **diag.on_change(lambda option, value: _tiers_changed(value)))       # called before the value updates
    _level_option = SpinnerOption(
        "Action skill level", LEVEL_CHOICES[1], list(LEVEL_CHOICES),
        description="Level of the first skill point and Expendable Assets. TPS 3, BL2 5. Raising it refunds.",
        **diag.on_change(lambda option, value: _first_point_changed(value)))
    _total_option = BoolOption(
        "Pre-Sequel skill point total", False,
        description="Stop at 68 skill points, as in the Pre-Sequel. Off: Borderlands 2's 76.",
        **diag.on_change(lambda option, value: _recount_soon()))
    return [_tier_option, _level_option, _total_option]


def tier_points(choice: str | None = None) -> int:
    """The requirement a choice means; None asks the option, which is also what an unapplied
    setting counts as when working out whether a change tightened the rule."""
    if choice is None:
        choice = _tier_option.value if _tier_option is not None else TIER_CHOICES[0]
    return 5 if choice == TIER_CHOICES[1] else 1


_level_forced: str | None = None


def first_point_level() -> int:
    choice = _level_forced if _level_forced is not None else (
        _level_option.value if _level_option is not None else LEVEL_CHOICES[1])
    return 3 if choice == LEVEL_CHOICES[0] else 5


def total_cap() -> int | None:
    return TPS_TOTAL if _total_option is not None and _total_option.value else None


# --- Borderlands 2's own level gate -------------------------------------------------------------
# "Action skill level" used to move only how many points the level-up handed out, so Jack got his
# point at level 3 and the skill screen still would not let him spend it until 5 (user, 2026-09-23).
# The gate is data rather than native code; read out of the cooked Startup, 2026-09-23:
#
#   INI_SkillPointsPerLevelUp            PlayerExperienceLevel >= 5 -> 1 point
#   INI_TotalSkillPointsForCurrentLevel  PlayerExperienceLevel >= 5 -> the formula below
#   INI_TotalSkillPointsForCurrentLevelFormula   level * 1 + (-4)
#
# The total is the one the screen asks for before it will spend anything, which is the half that was
# missing. The Pre-Sequel's numbers are the same shape two levels earlier: gate 3, offset -2, so
# level 70 comes to 68 - the total the "Pre-Sequel skill point total" setting already used.
#
# All three live in Startup and are shared with every other character, so the gate is written only
# while Jack is the one being played and put back to 5 otherwise.
GATE_INITS = ("GD_Globals.Skills.INI_SkillPointsPerLevelUp",
              "GD_Globals.Skills.INI_TotalSkillPointsForCurrentLevel")
GATE_FORMULA = "GD_Globals.Skills.INI_TotalSkillPointsForCurrentLevelFormula"
_gate_applied: int | None = None
# What the three objects held before we touched them, or what another mod last put there (compat review,
# 2026-10-06): Limitless rewrites them for 1/2/5/10 points a level from level 1, and putting BL2's hard-coded
# gate 5 / offset -4 back for the other characters took that away from every one of them; UCP and Reborn leave
# them alone. Other characters get exactly the baseline back; Jack gets his first-point level on it, keeping its
# points-per-level multiplier: total = multiplier x (level - first + 1).
_baseline: tuple | None = None          # (condition level, formula multiplier, formula offset)
_last_written: tuple | None = None      # what _write_gate left there, to notice another mod's write
_baseline_checked = 0.0


def _read_gate() -> tuple | None:
    """(condition level of INI_SkillPointsPerLevelUp, formula multiplier, formula offset), or None."""
    try:
        init = _find("AttributeInitializationDefinition", GATE_INITS[0])
        formula = _find("AttributeInitializationDefinition", GATE_FORMULA)
        if init is None or formula is None:
            return None
        cond = int(init.ConditionalInitialization.ConditionalExpressionList[0].Expressions[0].ConstantOperand2)
        vf = formula.ValueFormula
        return cond, float(vf.Multiplier.BaseValueConstant), float(vf.Offset.BaseValueConstant)
    except Exception:  # noqa: BLE001
        return None


def points_per_level() -> float:
    """The baseline's multiplier: 1 in BL2, 10 under Limitless' default."""
    return _baseline[1] if _baseline is not None and _baseline[1] > 0 else 1.0


def _write_gate(level: int, offset: float | None = None) -> str:
    """Both conditions and the formula's offset. Read-modify-write at every step: a struct read out
    of an array comes back as a copy, as the tier code below already has to allow for."""
    global _last_written
    if offset is None:
        offset = -points_per_level() * (level - 1)
    done = []
    for path in GATE_INITS:
        init = _find("AttributeInitializationDefinition", path)
        if init is None:
            continue
        try:
            ci = init.ConditionalInitialization
            lst = ci.ConditionalExpressionList
            entry = lst[0]
            exprs = entry.Expressions
            ex = exprs[0]
            if int(ex.ConstantOperand2) != level:
                ex.ConstantOperand2 = level
                exprs[0] = ex
                entry.Expressions = exprs
                lst[0] = entry
                ci.ConditionalExpressionList = lst
                init.ConditionalInitialization = ci
            done.append(path.split(".")[-1])
        except Exception:  # noqa: BLE001
            unrealsdk.logging.error(f"{TAG} {path}: " + traceback.format_exc().splitlines()[-1])
    formula = _find("AttributeInitializationDefinition", GATE_FORMULA)
    if formula is not None:
        try:
            vf = formula.ValueFormula
            off = vf.Offset
            if float(off.BaseValueConstant) != float(offset):
                off.BaseValueConstant = float(offset)
                vf.Offset = off
                formula.ValueFormula = vf
            done.append("Formula")
        except Exception:  # noqa: BLE001
            unrealsdk.logging.error(f"{TAG} {GATE_FORMULA}: " + traceback.format_exc().splitlines()[-1])
    _last_written = _read_gate()
    return f"first point at {level} (formula offset {offset:g}) on {len(done)} of 3 objects"


def apply_gate(pc: UObject) -> None:
    """Jack's rule while Jack is the one playing, Borderlands 2's the rest of the time. From every
    character's tick; skills.player_tick calls it for the others before its Jack-only return."""
    set_gate_for(_ours(pc), "tick")


def set_gate_for(is_jack: bool, why: str) -> None:
    """The gate for the character being played or loaded. Also from FinishSaveGameLoad, before the
    engine counts the loaded character's points: the per-tick call alone left a window at the first
    frames of another character's load where Jack's gate still stood."""
    global _gate_applied, _baseline, _baseline_checked
    now = time.monotonic()
    if _baseline is None or now >= _baseline_checked:
        _baseline_checked = now + 1.0
        live = _read_gate()
        if live is not None and (_baseline is None or (_last_written is not None and live != _last_written)):
            # first sight, or another mod wrote them since we did (a text mod run at the main menu): that is the
            # baseline now, for the other characters and as Jack's multiplier
            if _baseline is not None:
                unrealsdk.logging.info(f"{TAG} skill point formulas changed by another mod: {live} (was {_baseline}); "
                                       "following it")
            _baseline = live
            _gate_applied = None
    if _baseline is None:
        return                               # the objects are not resident yet
    want = ("jack", first_point_level(), _baseline) if is_jack else ("base", _baseline)
    if _gate_applied == want:
        return
    if is_jack:
        r = _write_gate(first_point_level())
    else:
        r = _write_gate(int(_baseline[0]), offset=_baseline[2])
    _gate_applied = want
    unrealsdk.logging.info(f"{TAG} {r} ({why}, {'Jack' if is_jack else 'another character'}; "
                           f"{points_per_level():g} point(s) a level)")


def _first_point_changed(value: str) -> None:
    """Raising the level can strand a build the same way tightening the single-point row does: a
    character who specced at 3 and is then told the first point arrives at 5 is holding skills the
    new rule would not have paid for, and at low levels the action skill itself is one of them. So
    it refunds on the way up, exactly as the row setting does, and only recounts on the way down,
    where the change can only ever hand points out (user, 2026-09-23)."""
    global _level_forced, _gate_applied, _refund_owed, _synced
    before = first_point_level()
    _level_forced = str(value)
    _gate_applied = None          # the next tick rewrites the condition
    if _settings_ready and first_point_level() > before:
        _refund_owed = True
        # ...and wake tick() up. It drains the refund, but it returns at its first line once the
        # single-point row is synced, and this setting does not touch that state - so the flag was
        # being set and never read (user raised the level back to 5 with two points invested and
        # kept them, 2026-09-23). Clearing _synced is what _tiers_changed does for the same reason.
        _synced = None
    _recount_soon()


# The three trees are children of the action skill's own branch, whose Tiers[0] asks for one point
# before any child tier opens, and that one point is the action skill itself. Borderlands 2 puts it
# in at level 5 for free - the player still has their first point to place afterwards, so it is a
# grade, not a spend. Nothing put it in at level 3, so "Action skill level: 3 (TPS)" handed Jack a
# point and left all three trees dark (user's screenshot, 2026-09-23). The live-tree sync could not
# see it either: it works each tier out from its own branch's spend, and tier 0 of a child asks 0
# within its own branch, so it kept reporting that everything already matched.
#
#   Branch_Actionskill_Doppelganger
#     Children: [Branch_TheHeroofthisStory, Branch_GreaterGood, Branch_FreeEnterprise]
#     Tiers[0]: Skills = [Skill_DeathTrap], PointsToUnlockNextTier = 1
ACTION_SKILL_BRANCH = "Quince_Doppel_Skills.SkillTree.Branch_Actionskill_Doppelganger"


def _first(value):
    """GetSkillState / GetBranchState return (ReturnValue, OutState) through the SDK."""
    return value[-1] if isinstance(value, tuple) else value


# Through this SDK a function's out parameter is an ARGUMENT, not just a return value: leaving it
# off raises "GetSkillState() missing 1 required positional argument: 'OutSkillState'". Every read
# of the skill, tier and branch state threw that for a whole session and was swallowed as
# "unreadable", which is why the value that decides whether a cell is greyed - bIsUnlocked - went
# unexamined for so long (2026-09-23).
_OUT = {"SkillTreeSkillStateData": "OutSkillState",
        "SkillTreeTierStateData": "OutTierState",
        "SkillTreeBranchStateData": "OutBranchState"}


def _state(fn, struct_name: str, **kwargs):
    """Call one of the *State getters and hand back its filled-in struct, or None."""
    try:
        blank = unrealsdk.make_struct(struct_name)
    except Exception:  # noqa: BLE001
        blank = None
    for attempt in ({**kwargs, _OUT[struct_name]: blank}, kwargs):
        try:
            r = fn(**attempt)
        except Exception:  # noqa: BLE001
            continue
        return r[-1] if isinstance(r, tuple) else r
    return None


def _action_skill_object(pc: UObject):
    """(tree, action skill definition). GetActionSkill answers; the root branch is the fallback."""
    tree = pc.PlayerSkillTree if pc is not None else None
    if tree is None:
        return None, None
    try:
        skill = tree.GetActionSkill()
    except Exception:  # noqa: BLE001
        skill = None
    if skill is not None:
        return tree, skill
    root = _find("SkillTreeBranchDefinition", ACTION_SKILL_BRANCH)
    try:
        return tree, (root.Tiers[0].Skills[0] if root is not None else None)
    except Exception:  # noqa: BLE001
        return tree, None


def action_skill_state(pc: UObject):
    """(grade, points spent in the root branch), or None if the tree is not ready."""
    tree = pc.PlayerSkillTree if pc is not None else None
    if tree is None:
        return None
    try:
        skill = tree.GetActionSkill()
        if skill is None:
            # GetActionSkill has come back None all session, which is why nothing was ever granted
            # and why the tree report never printed. The branch names the skill anyway.
            root = _find("SkillTreeBranchDefinition", ACTION_SKILL_BRANCH)
            try:
                skill = root.Tiers[0].Skills[0] if root is not None else None
            except Exception:  # noqa: BLE001
                skill = None
            if skill is None:
                return None
        st = _state(tree.GetSkillState, "SkillTreeSkillStateData", SkillDef=skill)
        grade = int(st.SkillGrade) if st is not None else -1
        root = _find("SkillTreeBranchDefinition", ACTION_SKILL_BRANCH)
        bs = _state(tree.GetBranchState, "SkillTreeBranchStateData", BranchDef=root) if root is not None else None
        spend = int(bs.PointsSpentInBranch) if bs is not None else -1
        return skill, grade, spend
    except Exception:  # noqa: BLE001
        return None


def ensure_action_skill(pc: UObject) -> str:
    """Nothing. Borderlands 2 does not grant the action skill - the player specs it - and an earlier
    version of this filled it in for free, which was wrong. Kept as a no-op so the tick's call site
    stays honest about there being nothing to do here."""
    return ""


# --- the level the skill screen is shown -----------------------------------------------------------
# What actually stops a point going in before level 5, measured at level 3 on 2026-09-23:
#
#   skill state       bIsUnlocked True, SkillGrade 0, TierNumber 0
#   root branch       unlocked True, spent 0/1        root tier 0   unlocked True, spent 0
#   GetSkillUpgradeCost 1, HasPlayerEarnedAnySkillPoints True, 1 point in hand
#
# Every gate the game models is open, and PlayerSkillTree.UpgradeSkill accepts the spend. The block
# is in the skill SCREEN, which refuses on the character's level in UI code that cannot be read from
# here. So the screen is shown level 5 for exactly as long as it is open and then the level goes
# straight back. The tree does all the real work - genuine point, genuine cost, genuine grade - and
# nothing else is forced: the trees stay shut until the action skill has the root branch's point,
# the way they should.
_real_level = None


def _menu_open(pc: UObject) -> bool:
    try:
        return bool(pc.GetPlayerViewportClient().ViewportUI.bShowingMenu)
    except Exception:  # noqa: BLE001
        return True                      # cannot tell: leave it alone rather than yank it back


def _show_level_to_screen(pc: UObject) -> None:
    global _real_level
    if _real_level is not None:
        return
    try:
        pri = pc.PlayerReplicationInfo
        level = int(pri.ExpLevel)
        if level >= BL2_FIRST_POINT_LEVEL or level < first_point_level():
            return
        _real_level = level
        pri.ExpLevel = BL2_FIRST_POINT_LEVEL
        unrealsdk.logging.info(f"{TAG} skill tree: showing the screen level {BL2_FIRST_POINT_LEVEL} "
                               f"instead of {level} while it is open")
    except Exception:  # noqa: BLE001
        _real_level = None


def restore_level(why: str) -> None:
    """Put the real level back: on close, and from the tick if the screen ever goes without closing."""
    global _real_level
    if _real_level is None:
        return
    level, _real_level = _real_level, None
    try:
        pc = get_pc()
        if pc is not None and pc.PlayerReplicationInfo is not None:
            pc.PlayerReplicationInfo.ExpLevel = level
            unrealsdk.logging.info(f"{TAG} skill tree: level back to {level} ({why})")
    except Exception:  # noqa: BLE001
        pass


@hook("WillowGame.SkillTreeGFxObject:HandleOpen", Type.POST)
def _on_skill_screen_open(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    try:
        pc = get_pc()
    except Exception:  # noqa: BLE001
        return
    if pc is None or not _ours(pc) or first_point_level() >= BL2_FIRST_POINT_LEVEL:
        return
    _show_level_to_screen(pc)


@hook("WillowGame.SkillTreeGFxObject:HandleClose", Type.PRE)
def _on_skill_screen_close(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    restore_level("screen closed")


# --- the single-point row ---------------------------------------------------------------------
def _find(cls: str, path: str):
    try:
        return unrealsdk.find_object(cls, path)
    except ValueError:
        return None


def apply_tiers(choice: str | None = None, force: bool = False) -> str:
    """Write the chosen requirement into the third tier of each resident branch."""
    global _tiers_applied
    if choice is None:
        choice = _tier_option.value if _tier_option is not None else TIER_CHOICES[0]
    if _tiers_applied == choice and not force:
        return "unchanged"
    points = tier_points(choice)
    done = []
    for path in BRANCHES:
        branch = _find("SkillTreeBranchDefinition", path)
        if branch is None:
            continue
        try:
            tiers = branch.Tiers
            if len(tiers) <= SINGLE_POINT_TIER:
                continue
            tier = tiers[SINGLE_POINT_TIER]
            if int(tier.PointsToUnlockNextTier) != points:
                tier.PointsToUnlockNextTier = points
                tiers[SINGLE_POINT_TIER] = tier
            done.append(path.split(".")[-1])
        except Exception:  # noqa: BLE001
            unrealsdk.logging.error(f"{TAG} {path}: " + traceback.format_exc().splitlines()[-1])
    if len(done) == len(BRANCHES):
        _tiers_applied = choice
    return f"single-point row -> {points} point(s) on {len(done)} of {len(BRANCHES)} trees"


def _tiers_changed(value: str) -> None:
    """The option's own change: the branches are often not resident yet (a flip at the main menu
    logged "0 of 3 trees"), so this is a first attempt and the tick is what makes it stick."""
    global _tiers_applied, _synced, _refund_owed
    if _settings_ready and tier_points(value) > tier_points(_tiers_applied):
        _refund_owed = True          # the build may now sit in rows the stricter rule keeps shut
    _tiers_applied = None
    _synced = None
    try:
        unrealsdk.logging.info(f"{TAG} " + apply_tiers(value))
    except Exception:  # noqa: BLE001
        pass


def reset_session() -> None:
    """A level change reloads the packages' cooked values; the tick re-applies."""
    global _tiers_applied, _synced, _settle, _tree_logged, _tree_tries, _grant_reported
    global _granted_at
    _granted_at = None
    global _spend_probe
    _grant_reported = False
    _spend_probe = 0                     # a fresh budget for the character being loaded now
    _tree_logged = None
    _tree_tries = 0
    _tiers_applied = None
    _synced = None          # the tree is built again per level, so its thresholds are stale again
    _settle = None
    _points_before.clear()
    # _refund_owed deliberately survives: the setting is often changed at the main menu, where there
    # is no tree to refund, and the character loaded next is the one the change strands. It is
    # one-shot, so it cannot fire twice.


_next_try = 0.0


def tick() -> None:
    """From the per-tick path once the packages are resident: the tier rule, once per session
    (a look-up per second until the branches are there, then the live tree once)."""
    global _next_try, _synced, _refund_owed
    if _tiers_applied is not None and _synced == _tiers_applied:
        return
    now = time.monotonic()
    if now < _next_try:
        return
    _next_try = now + 1.0
    if _tiers_applied is None:
        r = apply_tiers()
        if _tiers_applied is None:
            return               # the branches are not resident yet; the option change lands here
        diag.info(f"{TAG} {r}")
    # the definitions are right, so the tree the player is looking at can be brought up to them
    from mods_base import get_pc
    pc = get_pc()
    if pc is None or pc.PlayerSkillTree is None:
        return                   # before the character exists: try again next second
    if not _ours(pc):
        return                   # refund_tree and sync_live_tree both write to pc's own tree
    # the refund comes first: it empties the tree, and which tiers are open is worked out from what
    # is spent in each branch, so doing it the other way round leaves every tier reading unlocked
    if _refund_owed:
        _refund_owed = False
        unrealsdk.logging.info(f"{TAG} " + refund_tree(pc))
    _synced = _tiers_applied
    r = sync_live_tree(pc, "single-point row")
    (diag.detail if r == "live tree already matches" else unrealsdk.logging.info)(f"{TAG} {r}")


# The recount has to wait for the tree to be real. On a load the controller and its PlayerSkillTree
# both exist before the saved grades are put back, and read in that window
# GetSkillPointsSpentInTree() answers 0: the recount then hands over every point the character has
# earned as unspent and the grades arrive on top of them, which is coming back from a save and quit
# with more points than you left with (user, 2026-09-23). Nothing announces that the tree is
# finished, so the numbers have to hold still instead - the same level and spend read a second
# apart, with a pawn in the world.
_settle: tuple | None = None
_settle_at = 0.0
SETTLE_SECONDS = 1.0


_points_tick_at = 0.0
POINTS_TICK_SECONDS = 0.25     # the settle test below needs the same reading a second apart; 4 Hz is plenty


def points_tick(pc: UObject) -> None:
    """From the per-tick path: keep the unspent count at earned - spent, and Borderlands 2's gate on
    whichever level the setting asks for. Four times a second: GetSkillPointsSpentInTree walks the
    tree natively and this was 285 us of every frame (doppelperf, 2026-09-25).

    This used to run once a session and then only when a setting changed, so the level-up hook's
    arithmetic was never checked against the tree and three separate level-7 grants in half a minute
    stayed on the character (streamer's log, 2026-09-23). It runs every tick now and writes only
    when the two disagree, which also repairs a character levelled under the other rule."""
    global _settle, _settle_at, _recount_at, _points_tick_at
    now = time.monotonic()
    if now < _points_tick_at:
        return
    _points_tick_at = now + POINTS_TICK_SECONDS
    try:
        if _real_level is not None and not _menu_open(pc):
            restore_level("no menu on screen")
        # apply_gate runs for everyone on purpose: the three INI objects live in Startup and are
        # shared, so they have to be put back to Borderlands 2's 5 whenever the player is not Jack.
        apply_gate(pc)
        if not _ours(pc):
            # Nothing past here may touch another character. recount_points writes
            # GeneralSkillPoints from Jack's rules - his first-point level, his earned-points
            # formula, his cap - and with no check here it was doing that to Gaige every tick, so
            # her points were being rewritten under a formula that is not hers and saved that way
            # (user, 2026-09-23: Gaige -> invest -> save quit -> load Jack, crash). The level-up
            # hooks were guarded; this path never was.
            _settle = None
            return
        _report_tree_once(pc)
        # No tier forcing here. Both of those were written while the tier gate was wrongly believed
        # to be the blocker, and with the real one fixed they only break the natural order: the
        # trees are supposed to stay shut until the action skill has the root branch's point in it
        # (user, 2026-09-23 - points were going into the trees with the action skill still at 0/1).
        granted = ensure_action_skill(pc)
        if granted:
            unrealsdk.logging.info(f"{TAG} {granted}")
        if pc.Pawn is None:
            _settle = None
            return
        pri, tree = pc.PlayerReplicationInfo, pc.PlayerSkillTree
        if pri is None or tree is None:
            _settle = None
            return
        key = (int(pri.ExpLevel), int(tree.GetSkillPointsSpentInTree()))
    except Exception:  # noqa: BLE001
        _settle = None
        return
    now = time.monotonic()
    if key != _settle:
        _settle, _settle_at = key, now
        return
    if now - _settle_at < SETTLE_SECONDS:
        return
    _recount_at = 0.0
    recount_points(pc, "skill points")


# --- the live tree ------------------------------------------------------------------------------
# Writing the definitions is only half of it. The controller's PlayerSkillTree holds its own copy of
# the thresholds: each entry of its Tiers array carries BranchPointsToUnlockTier, the running total
# of the branch definition's PointsToUnlockNextTier up to that tier, worked out once when the tree
# is initialised. Nothing recomputes it in place, so a changed setting did nothing until the next
# load re-initialised the tree, which is the save and quit players kept having to do
# (`doppelcheck tree`, 2026-09-22). Reading the same dump:
#
#   Tiers[3]  {SkillIndices: [6, 7], BranchPointsToUnlockTier: 10, Index: 3, TierNumber: 2,
#              ParentBranchIndex: 1, bUnlocked: True, ...}
#
# so a tier knows its branch and its number within it, and 0/5/10/15/20/25 against a definition of
# [5, 5, 5, 5, 5, 0] gives the rule: the threshold for tier n is the sum of the first n.
_synced: str | None = None
_refund_owed = False


def _thresholds(branch_def) -> list:
    """The cumulative points a branch's definition now asks for, one per tier."""
    out, running = [], 0
    for tier in branch_def.Tiers:
        out.append(running)
        running += int(tier.PointsToUnlockNextTier)
    return out


def _open_branches(tree, spent: dict) -> dict:
    """Branch index -> whether the branch itself is open: the root always is, and a child is open
    once its parent is and the parent's spend covers every tier the parent has.

    The three trees hang off the action skill's own branch, whose one tier asks for one point (the
    action skill). The row rule below only ever compared a tier with the spend in ITS OWN branch, and
    a branch's first row asks 0 of its own, so the sync opened the first row of all three trees with
    the action skill still unbought and points went straight into the trees (user, 2026-09-25; the
    session's own line read "3 tier(s) opened ... branch spend [(0, 0), (1, 0), (2, 0), (3, 0)]").
    The engine's own bUnlocked walks the parent chain; so does this now. The live Branches carry
    ParentBranchIndex (-1 on the root), the same field doppelcheck's tree report reads."""
    parent_of, needs = {}, {}
    for b in tree.Branches:
        try:
            idx = int(b.Index)
            parent = int(b.ParentBranchIndex)
            parent_of[idx] = parent if parent >= 0 else None
            needs[idx] = sum(int(t.PointsToUnlockNextTier) for t in b.Definition.Tiers) if b.Definition is not None else 0
        except Exception:  # noqa: BLE001
            continue

    def is_open(idx, depth=0):
        parent = parent_of.get(idx)
        if parent is None or depth > 8:
            return True                                  # the root, or an orphan: never gate those
        return is_open(parent, depth + 1) and spent.get(parent, 0) >= needs.get(parent, 0)

    return {idx: is_open(idx) for idx in parent_of}


def _branch_spend(tree) -> dict:
    """Points spent in each branch, by branch index. GetBranchState is the same call the respec
    check uses; the live Branches entries carry no spend of their own."""
    out = {}
    for branch in tree.Branches:
        d = branch.Definition
        if d is None:
            continue
        try:
            state = _state(tree.GetBranchState, "SkillTreeBranchStateData", BranchDef=d)
            if state is None:
                continue
            out[int(branch.Index)] = int(state.PointsSpentInBranch)
        except Exception:  # noqa: BLE001
            continue
    return out


def sync_live_tree(pc: UObject, why: str) -> str:
    """Put the definitions' thresholds onto the tree the player is looking at, and say which tiers
    that opens or shuts.

    `UpdateBranchProgression` would be the engine's own recompute, but it wants an argument the mod
    has no business inventing (`TypeError: missing 1 required positional argument: 'GF...'`, so a
    GFx movie, 2026-09-22). The rule is not complicated: the thresholds run non-decreasing up a
    branch, so a tier is open exactly when the branch's spend reaches its own. Loosening the rule is
    where this matters, since a tier that stays shut is one the player cannot spend in."""
    tree = pc.PlayerSkillTree if pc is not None else None
    if tree is None:
        return "no live tree yet"
    try:
        wanted = {int(b.Index): _thresholds(b.Definition) for b in tree.Branches if b.Definition is not None}
        spent = _branch_spend(tree)
        open_branch = _open_branches(tree, spent)
        tiers = tree.Tiers
        moved, opened, shut = 0, 0, 0
        for i in range(len(tiers)):
            tier = tiers[i]
            branch = int(tier.ParentBranchIndex)
            per_tier = wanted.get(branch)
            n = int(tier.TierNumber)
            if not per_tier or n >= len(per_tier):
                continue
            threshold = per_tier[n]
            if branch in spent:
                unlocked = open_branch.get(branch, True) and spent[branch] >= threshold
            else:
                unlocked = bool(tier.bUnlocked)
            if int(tier.BranchPointsToUnlockTier) == threshold and bool(tier.bUnlocked) == unlocked:
                continue
            moved += int(tier.BranchPointsToUnlockTier) != threshold
            opened += unlocked and not bool(tier.bUnlocked)
            shut += bool(tier.bUnlocked) and not unlocked
            tier.BranchPointsToUnlockTier = threshold
            tier.bUnlocked = unlocked
            tiers[i] = tier
    except Exception:  # noqa: BLE001
        return "live tree: " + traceback.format_exc().splitlines()[-1][:110]
    if not (moved or opened or shut):
        return "live tree already matches"
    return (f"live tree ({why}): {moved} threshold(s) moved, {opened} tier(s) opened, "
            f"{shut} shut; branch spend {sorted(spent.items())}")


def refund_tree(pc: UObject) -> str:
    """Give the points back when the single-point row is tightened.

    A build put together under the Pre-Sequel's rule can sit in tiers the Borderlands 2 rule will
    not open: with one point in the middle row a player is several tiers further down than five
    points would have taken them, so after the change the spent points are in rows the tree no
    longer unlocks and there is no way to move them (user, 2026-09-22). Loosening the rule never
    strands anything, so only a tightening refunds. ResetSkillTree is the quick-change station's own
    path, which it reaches after charging; called here it is free."""
    if pc is None:
        return "no controller to refund yet"
    try:
        spent = int(pc.PlayerSkillTree.GetSkillPointsSpentInTree())
    except Exception:  # noqa: BLE001
        return "refund: " + traceback.format_exc().splitlines()[-1][:110]
    if spent <= 0:
        return "nothing spent, no refund needed"
    try:
        pc.ResetSkillTree(bIgnoreProficiencies=True, bIsCharacterLoad=False)
    except Exception:  # noqa: BLE001
        return "refund failed: " + traceback.format_exc().splitlines()[-1][:110]
    if _on_client(pc):
        # the tree that counts is the host's copy: reset it there too, with the points this copy works out under
        # its own settings (the host kept his old grades and refused the action skill's point, user 2026-10-06)
        try:
            level = int(pc.PlayerReplicationInfo.ExpLevel)
        except Exception:  # noqa: BLE001
            level = 0
        sent = _coop_points(True, earned_points(level))
        unrealsdk.logging.info(f"{TAG} joined Jack: tree reset on the host too ("
                               + ("sent" if sent else "NOT sent: no networking") + ")")
    # ResetSkillTree clears the grades but does not hand the points back: after refunding 12 of a
    # level 80 character's 76, GeneralSkillPoints still read 64 (user, 2026-09-22). The count that
    # is now correct is the one recount_points works out, since nothing is spent any more.
    return f"single-point row tightened, {spent} point(s) cleared; " + recount_points(pc, "after the refund", force=True)


# --- the total ------------------------------------------------------------------------------------
# Points are handed out one per level from the first-point level on, so a character at level L has
# earned L - first + 1 of them (capped where the Pre-Sequel total is chosen). Correcting only the
# level-up left an existing character on whatever the old rule had given: changing the setting looked
# like it did nothing (user, 2026-09-22). The recount sets the unspent count to earned - spent, which
# both applies a changed setting at once and repairs a character that was levelled under the other
# one. Spending is never touched, so a respec is not needed.
_recount_at = 0.0
_last_recount: tuple | None = None


def _on_client(pc) -> bool:
    try:
        return int(pc.WorldInfo.NetMode) == 3
    except Exception:  # noqa: BLE001
        return False


def _coop_points(reset: bool, points: int) -> bool:
    try:
        from . import coopnet
        return coopnet.send_points(reset, points)
    except Exception:  # noqa: BLE001
        return False


def remote_points(pc, reset: bool, points: int) -> None:
    """On the host, from a joined Jack's copy (coopnet.doppel_coop_points): his tree reset here (ResetSkillTree is
    native and server-side; BL2's own ServerPurchaseSkillTreeReset charges the respec price) and his unspent points
    set to what his copy worked out under his own settings. Replicates back through his PRI."""
    if pc is None or not _ours(pc) or points < 0:
        return
    try:
        if reset:
            pc.ResetSkillTree(bIgnoreProficiencies=True, bIsCharacterLoad=False)
        pri = pc.PlayerReplicationInfo
        before = int(pri.GeneralSkillPoints)
        pri.GeneralSkillPoints = int(points)
        pri.bForceNetUpdate = True
        unrealsdk.logging.info(f"{TAG} joined Jack {pc.Name}: " + ("tree reset, " if reset else "")
                               + f"unspent {before} -> {points}")
    except Exception:  # noqa: BLE001
        unrealsdk.logging.error(f"{TAG} joined Jack's points: " + traceback.format_exc().splitlines()[-1])


# --- skill caps raised by another mod (compat, 2026-10-06) -----------------------------------------------------------
# Limitless raises every skill's MaxGrade to 100 (or 10) with hotfixes keyed to each vanilla class's own package, so
# Jack's copies stayed at 5/5 while his points went up tenfold (user, 2026-10-06). The running hotfix list says what
# the other classes get: the most common MaxGrade it sets on skills that are not ours. Jack's tree skills (the action
# skill's branch excluded) follow it, and go back to their own grades when no such hotfix is running.
MAX_GRADE_SCAN_SECONDS = 10.0
MAX_GRADE_MIN_ENTRIES = 10                # a handful of one-off fixes is not a cap mod
_max_grade_next = 0.0
_max_grade_target: int | None = None
_max_grade_orig: dict = {}                # skill path -> its own MaxGrade


def _hotfix_max_grade() -> int | None:
    counts: dict = {}
    try:
        services = unrealsdk.find_all("SparkServiceConfiguration", exact=False)
    except Exception:  # noqa: BLE001
        return None
    for svc in services:
        try:
            values = list(svc.Values)
        except Exception:  # noqa: BLE001
            continue
        for v in values:
            v = str(v)
            if ",MaxGrade," not in v:
                continue
            parts = v.split(",")
            try:
                i = parts.index("MaxGrade")
                obj = parts[i - 1]
                grade = int(float(parts[-1]))
            except (ValueError, IndexError):
                continue
            if obj.startswith(("Quince_Doppel", "GD_Doppelganger")) or grade <= 0:
                continue
            counts[grade] = counts.get(grade, 0) + 1
    if not counts:
        return None
    grade, n = max(counts.items(), key=lambda kv: kv[1])
    return grade if n >= MAX_GRADE_MIN_ENTRIES and grade != 5 else None


def max_grade_tick() -> None:
    """From every player tick (any character: a host's skill definitions also judge a joined Jack's spends)."""
    global _max_grade_next, _max_grade_target
    now = time.monotonic()
    if now < _max_grade_next:
        return
    _max_grade_next = now + MAX_GRADE_SCAN_SECONDS
    target = _hotfix_max_grade()
    changed = 0
    found = 0
    for path in BRANCHES:
        branch = _find("SkillTreeBranchDefinition", path)
        if branch is None:
            continue
        try:
            for tier in branch.Tiers:
                for sk in tier.Skills:
                    if sk is None:
                        continue
                    found += 1
                    key = sk._path_name()
                    orig = _max_grade_orig.setdefault(key, int(sk.MaxGrade))
                    # one-point skills (capstones, Hero Pose's kind) stay one point, as Limitless plays them (user)
                    want = target if target is not None and orig > 1 else orig
                    if int(sk.MaxGrade) != want:
                        sk.MaxGrade = want
                        changed += 1
        except Exception:  # noqa: BLE001
            continue
    if found and (changed or target != _max_grade_target):
        unrealsdk.logging.info(f"{TAG} skill caps: " + (f"another mod sets skills to {target} points; Jack's follow"
                               if target is not None else "back to Jack's own") + f" ({changed} of {found} changed)")
    if found:
        _max_grade_target = target


def earned_points(level: int) -> int:
    first = first_point_level()
    n = max(0, int(round(points_per_level() * (level - first + 1))))
    cap = total_cap()
    return min(n, cap) if cap is not None else n


def _recount_soon() -> None:
    """From an option change: the next tick does it, when a controller is certainly there."""
    global _recount_at, _last_recount
    _recount_at = time.monotonic()
    _last_recount = None


def recount_points(pc: UObject, why: str, force: bool = False) -> str:
    global _last_recount
    try:
        pri = pc.PlayerReplicationInfo
        tree = pc.PlayerSkillTree
        if pri is None or tree is None:
            return "no tree yet"
        level = int(pri.ExpLevel)
        spent = int(tree.GetSkillPointsSpentInTree())
        have = int(pri.GeneralSkillPoints)
    except Exception:  # noqa: BLE001
        return "unreadable: " + traceback.format_exc().splitlines()[-1]
    want = max(0, earned_points(level) - spent)
    # The key carries the balance the engine currently shows. Without it, one write per (level,
    # spend) was all this ever did: the engine recomputes unspent from its own uncapped formula on
    # a reload or a skill-screen open (78 at level 80 with the first point at 3, against the cap's
    # 68) and handed the difference back, and "already corrected" let it stand - a capped character
    # came back from a save and quit with 10 spare points (user, 2026-09-25). A changed balance is a
    # new case; the settle test above still keeps this to at most one write a second.
    key = (level, spent, want, have)
    if have == want:
        _last_recount = key
        return f"level {level}: {spent} spent, {have} unspent, correct"
    if _last_recount == key and not force:
        return "already corrected"
    try:
        pri.GeneralSkillPoints = want
    except Exception:  # noqa: BLE001
        return "could not write GeneralSkillPoints: " + traceback.format_exc().splitlines()[-1]
    if _on_client(pc):
        _coop_points(False, want)              # the count that is spent from is the host's (see remote_points)
    _last_recount = key
    cap = total_cap()
    msg = (f"{why}: level {level}, {spent} spent, {earned_points(level)} earned under Jack's rules "
           f"(first point at {first_point_level()}, "
           f"{'capped at ' + str(cap) if cap else 'uncapped'}); unspent {have} -> {want}")
    unrealsdk.logging.info(f"{TAG} {msg}")
    return msg


# --- driving the level, to test the three settings ------------------------------------------------
# Signatures read out of the cooked WillowGame, 2026-09-23 (kwargs only, the parameter order the SDK
# reports is reversed):
#   WillowPlayerController.ExpLevelUp(bCheated: bool)
#   WillowPlayerController.ExpEarn(ExpType: byte, Source: byte, Exp: int)
#   WillowPlayerController.GetExpPointsRequiredForLevel(Level: int) -> int
# ExpLevelUp is the game's own level-up, so OnExpLevelChange fires the way it does in play and the
# hook under test actually runs; writing ExpLevel by hand does not, which is why "set" says so.
def read_gate() -> str:
    """What the three gate objects say right now, so a changed setting can be seen to have landed."""
    parts = []
    for path in GATE_INITS:
        leaf = path.split(".")[-1]
        init = _find("AttributeInitializationDefinition", path)
        if init is None:
            parts.append(f"{leaf}=not resident")
            continue
        try:
            ex = init.ConditionalInitialization.ConditionalExpressionList[0].Expressions[0]
            parts.append(f"{leaf}>={int(ex.ConstantOperand2)}")
        except Exception:  # noqa: BLE001
            parts.append(f"{leaf}=unreadable")
    formula = _find("AttributeInitializationDefinition", GATE_FORMULA)
    try:
        parts.append(f"formula offset {int(formula.ValueFormula.Offset.BaseValueConstant)}")
    except Exception:  # noqa: BLE001
        parts.append("formula offset unreadable")
    return ", ".join(parts)


# The trees' state, written to the log once per character load while the first point comes before
# level 5. Chasing "I am level 3 and still cannot spend it" by asking for a console command has cost
# several rounds; this puts the same answer in the log every time without anyone remembering to.
_tree_logged = None                  # the level it last reported for, so level 3 gets its own line
_tree_tries = 0


def _report_tree_once(pc: UObject) -> None:
    """A full snapshot, once, on any load below level 5 - no command, no click, no timing.

    Built only on things that have proved readable through the SDK: the runtime arrays on
    PlayerSkillTree. GetSkillState's struct is not one of them (its grade comes back None), and
    depending on it is why this printed nothing for four sessions running (2026-09-23)."""
    global _tree_logged, _tree_tries
    if first_point_level() >= BL2_FIRST_POINT_LEVEL:
        return
    try:
        pri, tree = pc.PlayerReplicationInfo, pc.PlayerSkillTree
        if pri is None or tree is None or pc.Pawn is None:
            return
        level = int(pri.ExpLevel)
        if level >= BL2_FIRST_POINT_LEVEL or _tree_logged == level:
            return                       # only below Borderlands 2's gate, and once per level
        _tree_tries += 1
        if _tree_tries < 30:             # let the tree finish building
            return
        _tree_logged = level
        out = [f"level {level}, {int(pri.GeneralSkillPoints)} unspent, "
               f"{int(tree.GetSkillPointsSpentInTree())} spent"]
        tiers = [t for t in tree.Tiers]
        out.append(f"{len(tiers)} tier(s); first tiers: " + ", ".join(
            f"#{i} unlocked={bool(t.bUnlocked)} needs={int(t.BranchPointsToUnlockTier)}"
            for i, t in enumerate(tiers) if int(t.TierNumber) == 0))
        try:
            skills_ = [k for k in tree.Skills]
            out.append(f"{len(skills_)} skill slot(s), grades: " +
                       ", ".join(str(int(getattr(k, "Grade", getattr(k, "SkillGrade", -1)))) for k in skills_[:12]))
        except Exception:  # noqa: BLE001
            out.append("skill grades unreadable: " + traceback.format_exc().splitlines()[-1])
        skill = _action_skill_object(pc)[1]
        out.append(f"action skill object: {skill}")
        # The things the tree screen is most likely to be asking before it lets a point be placed.
        # None of them costs a point to ask; UpgradeSkill is deliberately NOT called here.
        # What greys the cell. PlayerSkillTreeSkillData holds only a Grade, so the locked look comes
        # from SkillTreeSkillStateData.bIsUnlocked, which GetSkillState computes. Print the raw
        # return rather than assuming its shape: reading it as a tuple gave None before and that was
        # taken as "unreadable" instead of being chased (2026-09-23).
        if skill is not None:
            st = _state(tree.GetSkillState, "SkillTreeSkillStateData", SkillDef=skill)
            out.append(f"skill state: {st!r}")
            if st is not None:
                out.append(f"  bIsUnlocked={getattr(st, 'bIsUnlocked', '?')} "
                           f"SkillGrade={getattr(st, 'SkillGrade', '?')} "
                           f"TierNumber={getattr(st, 'TierNumber', '?')}")
            root = _find("SkillTreeBranchDefinition", ACTION_SKILL_BRANCH)
            if root is not None:
                bs = _state(tree.GetBranchState, "SkillTreeBranchStateData", BranchDef=root)
                ts = _state(tree.GetTierState, "SkillTreeTierStateData", BranchDef=root, TierNumber=0)
                out.append(f"root branch state: unlocked={getattr(bs, 'bIsUnlocked', '?')} "
                           f"spent={getattr(bs, 'PointsSpentInBranch', '?')}/"
                           f"{getattr(bs, 'MaxPointsForBranch', '?')}")
                out.append(f"root tier 0 state: unlocked={getattr(ts, 'bIsUnlocked', '?')} "
                           f"spent={getattr(ts, 'PointsSpentInTier', '?')}")
        try:
            out.append(f"HasPlayerEarnedAnySkillPoints: {pc.HasPlayerEarnedAnySkillPoints()}")
        except Exception:  # noqa: BLE001
            out.append("HasPlayerEarnedAnySkillPoints: " + traceback.format_exc().splitlines()[-1])
        if skill is not None:
            try:
                out.append(f"GetSkillUpgradeCost(action skill): {pc.GetSkillUpgradeCost(Skill=skill)}")
            except Exception:  # noqa: BLE001
                out.append("GetSkillUpgradeCost: " + traceback.format_exc().splitlines()[-1])
        try:
            out.append(f"GetSkillPointsSpentInTree: {int(tree.GetSkillPointsSpentInTree())}, "
                       f"ExpLevel {level}, gate {first_point_level()}")
        except Exception:  # noqa: BLE001
            pass
        for line in out:
            unrealsdk.logging.info(f"{TAG} tree@{level}: {line}")
    except Exception:  # noqa: BLE001
        _tree_logged = -1
        unrealsdk.logging.info(f"{TAG} tree@?: " + traceback.format_exc().splitlines()[-1])


def tree_report(pc: UObject) -> list:
    """Every branch's unlock state, so a dark tree can be read off rather than guessed at. The
    children open on the root's spend; the root's own spend is the action skill's grade."""
    out = []
    tree = pc.PlayerSkillTree if pc is not None else None
    if tree is None:
        return ["tree: none"]
    root = _find("SkillTreeBranchDefinition", ACTION_SKILL_BRANCH)
    branches = [("root", root)]
    try:
        branches += [(str(c).split(".")[-1], c) for c in root.Children] if root is not None else []
    except Exception:  # noqa: BLE001
        pass
    for label, branch in branches:
        if branch is None:
            out.append(f"  {label}: not resident")
            continue
        try:
            bs = _state(tree.GetBranchState, "SkillTreeBranchStateData", BranchDef=branch)
            tier0 = _state(tree.GetTierState, "SkillTreeTierStateData", BranchDef=branch, TierNumber=0)
            if bs is None or tier0 is None:
                out.append(f"  {label}: state unreadable")
                continue
            out.append(f"  {label}: branch unlocked={bool(bs.bIsUnlocked)} "
                       f"spent={int(bs.PointsSpentInBranch)}/{int(bs.MaxPointsForBranch)}, "
                       f"tier 0 unlocked={bool(tier0.bIsUnlocked)} spent={int(tier0.PointsSpentInTier)}, "
                       f"needs {int(branch.Tiers[0].PointsToUnlockNextTier)} to open the next")
        except Exception:  # noqa: BLE001
            out.append(f"  {label}: unreadable ({traceback.format_exc().splitlines()[-1]})")
    return ["tree:"] + out


def level_report(pc: UObject) -> list:
    lines = []
    try:
        pri, tree = pc.PlayerReplicationInfo, pc.PlayerSkillTree
        level = int(pri.ExpLevel)
        have = int(pri.GeneralSkillPoints)
        spent = int(tree.GetSkillPointsSpentInTree()) if tree is not None else -1
        cap = total_cap()
        lines.append(f"level {level}: {have} unspent, {spent} spent, {earned_points(level)} earned "
                     f"(first point at {first_point_level()}, "
                     f"{'capped at ' + str(cap) if cap else 'uncapped'})")
    except Exception:  # noqa: BLE001
        lines.append("level: " + traceback.format_exc().splitlines()[-1])
    lines.append("gate: " + read_gate())
    lines += tree_report(pc)
    st = action_skill_state(pc)
    lines.append("action skill: unreadable" if st is None else
                 f"action skill: grade {st[1]}, root branch spend {st[2]} "
                 f"(the trees need 1 here before their first tier opens)")
    lines.append(f"single-point row: {tier_points()} point(s) (applied: {_tiers_applied})")
    return lines


def level_command(pc: UObject, parts: list) -> list:
    """`doppelskill level [up|xp|set] ...` - move the level so the point settings can be tested."""
    if pc is None:
        return ["no controller yet"]
    if not parts:
        return level_report(pc)
    what, n = parts[0], 1
    if len(parts) > 1:
        try:
            n = int(parts[1])
        except ValueError:
            return ["level [up <n> | xp <points> | set <level>]"]
    did = []
    try:
        if what == "up":
            for _ in range(max(1, n)):
                pc.ExpLevelUp(bCheated=False)
            did.append(f"ExpLevelUp x{max(1, n)}")
        elif what == "xp":
            pc.ExpEarn(ExpType=0, Source=0, Exp=max(1, n))
            did.append(f"ExpEarn {max(1, n)}")
        elif what == "set":
            pc.PlayerReplicationInfo.ExpLevel = n
            if pc.Pawn is not None:
                pc.Pawn.SetExpLevel(NewExpLevel=n)
            did.append(f"ExpLevel written straight to {n}; no level-up fires, so this is the way "
                       f"back down between runs, not a test of the hook")
        else:
            return ["level [up <n> | xp <points> | set <level>]"]
    except Exception:  # noqa: BLE001
        return ["level: " + traceback.format_exc().splitlines()[-1]]
    return did + level_report(pc)


# --- the level-up ----------------------------------------------------------------------------
def _ours(pc: UObject) -> bool:
    try:
        from . import characterslot
        return characterslot.is_doppelganger_fast(pc)
    except Exception:  # noqa: BLE001
        return False


def _points(pc: UObject) -> int | None:
    try:
        return int(pc.PlayerReplicationInfo.GeneralSkillPoints)
    except Exception:  # noqa: BLE001
        return None


@hook("WillowGame.WillowPlayerController:OnExpLevelChange", Type.PRE, hook_identifier="doppel_progression_pre")
def _on_level_pre(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("progression._on_level_pre")
    try:
        if not args.bNaturalLevelup or not _ours(obj):
            return
        n = _points(obj)
        if n is not None:
            _points_before[obj._path_name()] = n
    except Exception:  # noqa: BLE001
        pass


@hook("WillowGame.WillowPlayerController:OnExpLevelChange", Type.POST, hook_identifier="doppel_progression_post")
def _on_level_post(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    diag.hcrumb("progression._on_level_post")
    """After the engine's own award for this level: what Jack's rules say he should have got."""
    try:
        if not args.bNaturalLevelup or not _ours(obj):
            return
        key = obj._path_name()
        before = _points_before.pop(key, None)
        now = _points(obj)
        if before is None or now is None:
            return
        pri = obj.PlayerReplicationInfo
        level = int(pri.ExpLevel)
        awarded = now - before                              # the engine's: 1 from its own gate on
        # This used to add (wanted - awarded) to whatever was there. OnExpLevelChange comes round
        # again when the character does, with bNaturalLevelup still set, so the same level paid out
        # more than once: level 7 handed over three points in half a minute on a streamer's save
        # (2026-09-23). earned - spent gives the same answer however many times it is asked.
        r = recount_points(obj, f"level {level}", force=True)
        if r == "no tree yet":                              # mid-load: fall back to the arithmetic
            wanted = earned_points(level) - earned_points(level - 1)
            delta = wanted - awarded
            if delta:
                pri.GeneralSkillPoints = now + delta
            r = f"no tree to count against, added {delta} by hand"
        unrealsdk.logging.info(f"{TAG} level {level}: engine gave {awarded}; {r}")
    except Exception:  # noqa: BLE001
        unrealsdk.logging.error(f"{TAG} level-up hook failed: " + traceback.format_exc().splitlines()[-1])


ALL_HOOKS = (_on_level_pre, _on_level_post, _on_skill_screen_open, _on_skill_screen_close)
