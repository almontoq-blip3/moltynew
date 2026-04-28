"""
Strategy brain — main decision engine with priority-based action selection.

v2.0 BALANCED changes (vs v1.6.0 OVERAGGRESSIVE):
- Move EP cost corrected to 3 (per game docs) — was incorrectly 2
- Guardian attack threshold raised back to HP>=60 (not 30) — guardian ATK=10,
  but we need margin for counter-hits over multiple turns to kill (HP 150)
- Proactive heal threshold raised to 70 when safe (was 55) — stay healthy
- Critical heal threshold raised to 40 (was 25) — don't panic at 25%
- Guardian flee threshold: flee at HP<45 if no heals (not just 25)
- Agent combat: only engage if we have clear damage advantage OR target is weak
- Action rotation tracking: avoid cooldown waste (1 min per action type)
- EP rest threshold: rest if EP<=2 (not just <=1), to ensure we can attack next turn
- Monster farming: only attack if EP>=3 (preserve EP for fights/moves)
- Late game (<=20 alive, not 15): slightly earlier aggression ramp
- Inventory: keep slot free for sponsor items (limit 9 not 10)

Ranking formula: Kills > HP remaining at end
Strategy: survive first, farm kills second, hoard recovery items for late game

Uses ALL view fields from api-summary.md:
- self, currentRegion, connectedRegions, visibleRegions, visibleAgents,
  visibleMonsters, visibleItems, pendingDeathzones, recentMessages, aliveCount
"""

from bot.utils.logger import get_logger
log = get_logger(__name__)

# ── Weapon stats (from game docs) ──────────────────────────────────────
WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword":  {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow":    {"bonus": 5,  "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

# ── Item pickup priority ───────────────────────────────────────────────
ITEM_PRIORITY = {
    "rewards": 300,
    "katana": 100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger": 80, "bow": 75,
    "medkit": 70, "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars": 55,
    "map": 52,
    "megaphone": 40,
}

# ── Recovery item HP values ────────────────────────────────────────────
RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20,
    "energy_drink": 0,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}

def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))

def get_weapon_bonus(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)

def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)

_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_kills_this_game: int = 0
# Action rotation tracker: avoid cooldown (1 min = 1 turn per action type)
_last_action_type: str = ""
_action_counts: dict = {}

def _resolve_region(entry, view: dict):
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None

def _get_region_id(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""

def reset_game_state():
    global _known_agents, _map_knowledge, _kills_this_game, _last_action_type, _action_counts
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _kills_this_game = 0
    _last_action_type = ""
    _action_counts = {}
    log.info("Strategy brain reset for new game")

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine. Priority chain (v2.0 BALANCED):

    1.  DEATHZONE ESCAPE — instant, overrides all
    1b. Pre-escape pending death zone
    2.  Critical heal (HP < 40) — high threshold to avoid dying
    2b. Guardian threat evasion if HP < 45 and no heals
    3.  FREE ACTIONS: pickup, equip, utility items (these are free/no-turn)
    4.  EP recovery (energy drink or rest if EP <= 2)
    5.  Guardian farming — HP >= 60, EP >= 3, only when advantaged
    6.  Enemy agent combat — only when HP and damage advantage clear
    7.  Monster farming — EP >= 3, good source of kills
    8.  Proactive heal (HP < 70, area safe)
    9.  Facility interaction
    10. Strategic movement (Hills > Ruins > Plains > Forest; avoid water/DZ)
    11. Rest when EP <= 2 (ensures we can attack AND move next turn)
    """
    self_data     = view.get("self", {})
    region        = view.get("currentRegion", {})
    hp            = self_data.get("hp", 100)
    ep            = self_data.get("ep", 10)
    max_ep        = self_data.get("maxEp", 10)
    atk           = self_data.get("atk", 10)
    defense       = self_data.get("def", 5)
    is_alive      = self_data.get("isAlive", True)
    inventory     = self_data.get("inventory", [])
    equipped      = self_data.get("equippedWeapon")
    my_id         = self_data.get("id", "")
    my_region_id  = self_data.get("regionId", region.get("id", ""))

    visible_agents    = view.get("visibleAgents", [])
    visible_monsters  = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])
    visible_regions   = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz        = view.get("pendingDeathzones", [])
    messages          = view.get("recentMessages", [])
    alive_count       = view.get("aliveCount", 100)

    # Unwrap visibleItems
    visible_items = []
    for entry in visible_items_raw:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId", "")
            visible_items.append(inner)
        elif entry.get("id"):
            visible_items.append(entry)

    interactables = region.get("interactables", [])
    region_id     = region.get("id", "") or my_region_id
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""
    connections   = connected_regions or region.get("connections", [])

    if not is_alive:
        return None

    # ── Build danger map ───────────────────────────────────────────────
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str):
            danger_ids.add(dz)
    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved and resolved.get("isDeathZone"):
            danger_ids.add(resolved.get("id", ""))

    _track_agents(visible_agents, my_id, region_id)

    # Move costs 3 EP per game docs (not 2!)
    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # Game phase detection
    early_game = alive_count > 70
    mid_game   = 20 < alive_count <= 70
    late_game  = alive_count <= 20

    # ── P1: DEATHZONE ESCAPE ──────────────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp}"}
        log.error("🚨 IN DEATH ZONE but NO SAFE REGION or low EP!")

    # ── P1b: Pre-escape pending DZ ─────────────────────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming death zone soon"}

    # ── P2: Critical heal FIRST — before free actions to save our life ─
    # Threshold 40: at 40 HP, one guardian hit (10 ATK = ~8 dmg) still safe
    # but two hits might kill. Heal before it's too late.
    if hp < 40:
        heal = _find_healing_item(inventory, critical=True)
        if heal and can_act:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp} (dangerous!)"}

    # ── P2b: Guardian threat evasion ──────────────────────────────────
    # Flee if: critically low HP + guardians nearby + no heals available
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    has_heals = bool(_find_healing_item(inventory, critical=False))
    # Flee threshold: HP < 45 if no heals, HP < 30 if we do have heals
    flee_threshold = 45 if not has_heals else 30
    if guardians_here and hp < flee_threshold and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat + HP=%d (<%d), fleeing", hp, flee_threshold)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp} critical, no safe margin"}

    # ── P3: FREE ACTIONS (pickup, equip, utility) ──────────────────────
    # These consume no turn — always do them
    pickup_action = _check_pickup(visible_items, inventory, region_id)
    if pickup_action:
        return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        return util_action

    if not can_act:
        return None

    # ── P4: EP recovery ────────────────────────────────────────────────
    # Use energy drink at EP=0; rest at EP<=2 (ensures we can attack+move next turn)
    if ep == 0:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": "EP RECOVERY: EP=0, using energy drink (+5 EP)"}

    # ── P5: Guardian farming ────────────────────────────────────────────
    # Guardian: HP=150, ATK=10, DEF=5. Reward: 120+ sMoltz. Worth farming!
    # Conditions: HP >= 60 (survive multiple counter-hits), EP >= 3
    # Only attack if our damage > their counter-damage significantly
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 3 and hp >= 60:
        target = _select_best_target(guardians, atk, get_weapon_bonus(equipped),
                                     defense, region_weather)
        w_range = get_weapon_range(equipped)
        if target and _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            guardian_dmg = calc_damage(target.get("atk", 10),
                                       _estimate_enemy_weapon_bonus(target),
                                       defense, region_weather)
            target_hp = target.get("hp", 150)
            turns_to_kill_guardian = target_hp / max(1, my_dmg)
            # Only attack guardian if:
            # - We deal more damage than they deal us (favorable exchange)
            # - OR target is close to dead (secure the kill)
            # - AND we won't run out of HP doing so (rough check)
            hp_cost_estimate = guardian_dmg * min(turns_to_kill_guardian, 5)
            if (my_dmg >= guardian_dmg or target_hp <= my_dmg * 3) and hp - hp_cost_estimate > 20:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: HP={target_hp} "
                                  f"(sMoltz! dmg={my_dmg} vs {guardian_dmg})"}

    # ── P6: Agent combat ───────────────────────────────────────────────
    # Ranking = kills first — but don't throw our HP away carelessly.
    # Early game: avoid fights unless we're clearly stronger
    # Mid game: engage if damage advantage or target is weak
    # Late game (<=20 alive): more aggressive, we need kills for ranking
    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != my_id]

    # HP threshold to fight: higher early (need margin), lower late game
    hp_min_fight = 50 if early_game else (40 if mid_game else 25)

    if enemies and ep >= 2 and hp >= hp_min_fight:
        target = _select_best_target(enemies, atk, get_weapon_bonus(equipped),
                                     defense, region_weather)
        w_range = get_weapon_range(equipped)
        if target and _is_in_range(target, region_id, w_range, connections):
            my_dmg    = calc_damage(atk, get_weapon_bonus(equipped),
                                    target.get("def", 5), region_weather)
            enemy_dmg = calc_damage(target.get("atk", 10),
                                    _estimate_enemy_weapon_bonus(target),
                                    defense, region_weather)
            target_hp = target.get("hp", 100)

            # Late game: engage more liberally — kill securable targets
            if late_game and target_hp <= my_dmg * 5:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"LATE AGGRO: alive={alive_count}, target HP={target_hp}"}

            # Mid/Early: only fight if we clearly win the exchange
            # Conditions: we deal more damage OR target near death
            if my_dmg > enemy_dmg or target_hp <= my_dmg * 2:
                # Extra safety: don't fight if they can kill us in 2 hits
                if hp > enemy_dmg * 2:
                    return {"action": "attack",
                            "data": {"targetId": target["id"], "targetType": "agent"},
                            "reason": f"COMBAT: dmg={my_dmg} vs {enemy_dmg}, target HP={target_hp}"}

    # ── P7: Monster farming ────────────────────────────────────────────
    # Good kill source. EP>=3 to preserve energy for moves/attacks after.
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 3:
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER: {target.get('name','?')} HP={target.get('hp','?')}"}

    # ── P8: Proactive heal (HP < 70, area safe) ───────────────────────
    # Stay topped up when no enemies around. 70 gives room for fights.
    area_safe = not enemies and not guardians_here
    if hp < 70 and area_safe:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"PROACTIVE HEAL: HP={hp}, area safe"}

    # ── P9: Facility interaction ───────────────────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type','unknown')}"}

    # ── P10: Strategic movement ────────────────────────────────────────
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          guardians, visible_agents)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "MOVE: Repositioning strategically"}

    # ── P11: Rest — when EP <= 2 (can't move + attack reliably) ────────
    # EP<=2 means we can attack (EP 2) but can't move (EP 3) next turn.
    # Rest gives +1 bonus EP on top of auto-regen.
    if ep <= 2 and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, building energy (+1 bonus EP)"}

    return None  # Wait for next turn


# ── Helpers ───────────────────────────────────────────────────────────

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    """Per game docs: move = 3 EP base. Water terrain or fog adds more."""
    if terrain == "water":
        return 4  # water is extra costly
    if weather == "fog":
        return 4  # fog: +2 region requirements per docs
    if weather == "storm":
        return 3  # storm: move normal EP
    return 3  # base move cost is 3 (per game action table)

def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)

def _select_best_target(targets: list, my_atk: int, my_bonus: int,
                         my_def: int, weather: str) -> dict | None:
    """
    Smart target selection.
    Score = efficiency of killing them vs how dangerous they are.
    Prefer: low HP (easy to secure kill), low incoming damage (safe fight).
    """
    if not targets:
        return None

    scored = []
    for t in targets:
        t_hp  = t.get("hp", 100)
        t_def = t.get("def", 5)
        t_atk = t.get("atk", 10)
        t_bonus = _estimate_enemy_weapon_bonus(t)

        my_dmg     = max(1, calc_damage(my_atk, my_bonus, t_def, weather))
        their_dmg  = max(1, calc_damage(t_atk, t_bonus, my_def, weather))

        turns_to_kill = t_hp / my_dmg          # lower = easier to kill
        turns_to_die  = 100 / their_dmg        # higher = safer for us

        score = turns_to_die / turns_to_kill   # higher = better target
        # Big bonus for almost-dead targets (secure the kill!)
        if t_hp <= my_dmg * 2:
            score += 15
        # Bonus for targets that barely hurt us
        if their_dmg < my_dmg * 0.5:
            score += 5
        scored.append((t, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0]

def _select_weakest(targets: list) -> dict:
    return min(targets, key=lambda t: t.get("hp", 999))

def _is_in_range(target: dict, my_region: str, weapon_range: int,
                  connections=None) -> bool:
    target_region = target.get("regionId", "")
    if not target_region or target_region == my_region:
        return True
    if weapon_range >= 1 and connections:
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str):
                adj_ids.add(conn)
            elif isinstance(conn, dict):
                adj_ids.add(conn.get("id", ""))
        if target_region in adj_ids:
            return True
    return False

def _find_safe_region(connections, danger_ids: set, view: dict = None) -> str | None:
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                # Prefer terrains with more connections (safer to exit from)
                score = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -2}.get(terrain, 0)
                # Bonus if region has items or facilities
                if conn.get("interactables"):
                    score += 1
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    # Last resort: any non-DZ connected region
    for conn in connections:
        rid   = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            return rid
    return None

def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = [i for i in inventory
             if isinstance(i, dict)
             and i.get("typeId", "").lower() in RECOVERY_ITEMS
             and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0]
    if not heals:
        return None
    if critical:
        # Use strongest item when critical
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0), reverse=True)
    else:
        # Use weakest item first to conserve strong heals
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0))
    return heals[0]

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None

def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    # Keep 1 slot free for sponsor items (max 9 items, not 10)
    if len(inventory) >= 9:
        return None
    local_items = [i for i in items if isinstance(i, dict) and i.get("regionId") == region_id]
    if not local_items:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local_items:
        return None
    heal_count = sum(1 for i in inventory
                     if isinstance(i, dict)
                     and RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0) > 0)
    local_items.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best = local_items[0]
    if _pickup_score(best, inventory, heal_count) > 0:
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {best.get('typeId','item')}"}
    return None

def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    type_id  = item.get("typeId", "").lower()
    category = item.get("category", "").lower()
    if type_id == "rewards" or category == "currency":
        return 300
    if category == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        current_best = max(
            (WEAPONS.get(i.get("typeId","").lower(), {}).get("bonus", 0)
             for i in inventory if isinstance(i, dict) and i.get("category") == "weapon"),
            default=0)
        return (100 + bonus) if bonus > current_best else 0
    if type_id == "binoculars":
        has_binos = any(i.get("typeId","").lower() == "binoculars" for i in inventory if isinstance(i, dict))
        return 55 if not has_binos else 0
    if type_id == "map":
        return 52
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        # Always pick heals up to 4 stacks; bonus if we're low
        return ITEM_PRIORITY.get(type_id, 0) + (15 if heal_count < 4 else 0)
    if type_id == "energy_drink":
        return 58
    return ITEM_PRIORITY.get(type_id, 0)

def _check_equip(inventory: list, equipped) -> dict | None:
    current_bonus = get_weapon_bonus(equipped) if equipped else 0
    best = None
    best_bonus = current_bonus
    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        bonus = WEAPONS.get(item.get("typeId","").lower(), {}).get("bonus", 0)
        if bonus > best_bonus:
            best = item
            best_bonus = bonus
    if best:
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best.get('typeId','weapon')} (+{best_bonus} ATK)"}
    return None

def _select_facility(interactables: list, hp: int, ep: int) -> dict | None:
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
        # Use medical facility if HP not full (threshold 90 — top up whenever possible)
        if ftype == "medical_facility" and hp < 90:
            return fac
        if ftype == "supply_cache":
            return fac
        if ftype == "watchtower":
            return fac
        if ftype == "broadcast_station":
            return fac
    return None

def _track_agents(visible_agents: list, my_id: str, my_region: str):
    global _known_agents
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id", "")
        if not aid or aid == my_id:
            continue
        _known_agents[aid] = {
            "hp": agent.get("hp", 100),
            "atk": agent.get("atk", 10),
            "isGuardian": agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen": my_region,
            "isAlive": agent.get("isAlive", True),
        }
    if len(_known_agents) > 50:
        dead = [k for k, v in _known_agents.items() if not v.get("isAlive", True)]
        for d in dead[:20]:
            del _known_agents[d]

def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        # Use map immediately to reveal all regions (helps movement scoring)
        if type_id == "map":
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "UTILITY: Using Map — reveals entire map"}
    return None

def learn_from_map(view: dict):
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return
    _map_knowledge["revealed"] = True
    safe_regions = []
    for region in visible_regions:
        if not isinstance(region, dict):
            continue
        rid = region.get("id", "")
        if not rid:
            continue
        if region.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            conns = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            # Hills give best vision (+2), prioritize for scouting
            terrain_value = {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -1}.get(terrain, 0)
            safe_regions.append((rid, len(conns) + terrain_value))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("🗺️ MAP LEARNED: %d DZ, top center: %s",
             len(_map_knowledge["death_zones"]), _map_knowledge["safe_center"][:3])

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         guardians: list = None, visible_agents: list = None) -> str | None:
    """
    v2.0 BALANCED movement:
    - Hills (vision +2) >> Ruins (items) >> Plains >> Forest (avoid water)
    - Chase guardian regions for sMoltz farming
    - Chase item-rich regions early game
    - Center movement late game (away from shrinking DZ)
    - NEVER move into DZ or pending DZ
    """
    candidates = []
    item_regions = {i.get("regionId", "") for i in visible_items if isinstance(i, dict)}

    # Guardian region attraction
    guardian_regions = set()
    if guardians:
        for g in guardians:
            if g.get("isAlive", True):
                guardian_regions.add(g.get("regionId", ""))

    # Enemy regions (mild avoidance early, attraction late)
    enemy_regions = set()
    if visible_agents:
        for a in visible_agents:
            if not a.get("isGuardian", False) and a.get("isAlive", True):
                enemy_regions.add(a.get("regionId", ""))

    early_game = alive_count > 70
    late_game  = alive_count <= 20

    for conn in connections:
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            score = 1
            if conn in item_regions:
                score += 6
            if conn in guardian_regions:
                score += 7
            # Late game: seek enemies for kills
            if late_game and conn in enemy_regions:
                score += 4
            candidates.append((conn, score))

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            if rid in _map_knowledge.get("death_zones", set()):
                continue

            score = 0
            terrain = conn.get("terrain", "").lower()
            # Hills: best vision. Ruins: items. Plains: ok. Forest: ok. Water: avoid.
            score += {"hills": 5, "ruins": 3, "plains": 2, "forest": 1, "water": -4}.get(terrain, 0)

            if rid in item_regions:
                score += 6
            if rid in guardian_regions:
                score += 7  # Chase guardians for sMoltz

            # Unused facilities are valuable
            facs = conn.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 3

            weather = conn.get("weather", "").lower()
            score += {"storm": -2, "fog": -3, "rain": 0, "clear": 1}.get(weather, 0)

            # Late game: seek enemies, go central
            if late_game:
                if rid in enemy_regions:
                    score += 4
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 6

            # Mid game: go central, avoid edges (become DZ soon)
            if not early_game:
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 3

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]
