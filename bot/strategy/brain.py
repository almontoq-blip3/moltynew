"""
Strategy brain — main decision engine with priority-based action selection.

Implements the game-loop.md priority chain for high win rate.

v1.6.0 OPTIMIZED changes (vs v1.5.2):
- More aggressive early guardian farming (biggest sMoltz source!)
- Smarter combat: attack when advantaged, not just when HP > threshold
- Rest only when EP <= 1 (not < 4) — EP regen is automatic, don't waste turns
- Proactive healing at HP < 50 when area is safe (not just < 70)
- Better movement: chase items/guardians, avoid clustering
- Smarter target selection: prioritize low-HP targets to secure kills
- Late game hyper-aggression: when < 15 alive, attack everyone

Uses ALL view fields from api-summary.md:
- self: agent stats, inventory, equipped weapon
- currentRegion: terrain, weather, connections, facilities
- connectedRegions: adjacent regions (full Region object when visible, bare string ID when out-of-vision)
- visibleRegions: all regions in vision range
- visibleAgents: other agents (players + guardians — guardians are HOSTILE)
- visibleMonsters: monsters
- visibleNPCs: NPCs (flavor — safe to ignore per game-systems.md)
- visibleItems: ground items in visible regions
- pendingDeathzones: regions becoming death zones next ({id, name} entries)
- recentLogs: recent gameplay events
- recentMessages: regional/private/broadcast messages
- aliveCount: remaining alive agents
"""

from bot.utils.logger import get_logger
log = get_logger(__name__)

# ── Weapon stats ───────────────────────────────────────────────────────
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
    global _known_agents, _map_knowledge, _kills_this_game
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _kills_this_game = 0
    log.info("Strategy brain reset for new game")

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine. Priority chain (v1.6.0 OPTIMIZED):

    1.  DEATHZONE ESCAPE — instant, overrides all
    1b. Pre-escape pending death zone
    2.  Guardian threat evasion (if HP critically low)
    3.  FREE ACTIONS: pickup, equip, utility items
    4.  Critical heal (HP < 25) — use biggest item
    5.  EP recovery (energy drink if EP = 0)
    6.  Guardian farming — PRIORITIZED, only 5 guardians, 120 sMoltz each!
    7.  Enemy agent combat — aggressive when advantaged OR late game
    8.  Monster farming
    9.  Proactive heal (HP < 55, area safe)
    10. Facility interaction
    11. Strategic movement
    12. Rest ONLY when EP <= 1 (not < 4 — don't waste turns!)
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

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # ── P1: DEATHZONE ESCAPE ──────────────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp}"}
        log.error("🚨 IN DEATH ZONE but NO SAFE REGION!")

    # ── P1b: Pre-escape pending DZ ─────────────────────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming death zone soon"}

    # ── P2: Guardian threat evasion (only if HP very low) ─────────────
    # v1.6.0: flee threshold lowered to 25 (was 40) — fight more, flee less!
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    if guardians_here and hp < 25 and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat + critical HP=%d, fleeing", hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp} critical"}

    # ── P3: FREE ACTIONS (pickup, equip, utility) ──────────────────────
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

    # ── P4: Critical heal (HP < 25) ───────────────────────────────────
    # v1.6.0: threshold lowered to 25 (was 30) — don't panic-heal, save items
    if hp < 25:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp}"}

    # ── P5: EP recovery ────────────────────────────────────────────────
    if ep == 0:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": "EP RECOVERY: EP=0, using energy drink (+5 EP)"}

    # ── P6: Guardian farming ────────────────────────────────────────────
    # v1.6.0: HEAVILY PRIORITIZED — 120 sMoltz per kill, only 5 guardians!
    # Attack guardian if: HP > 30 AND EP >= 2 AND we deal more OR target low HP
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 30:
        target = _select_best_target(guardians, atk, get_weapon_bonus(equipped),
                                     defense, region_weather)
        w_range = get_weapon_range(equipped)
        if target and _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            guardian_dmg = calc_damage(target.get("atk", 10),
                                       _estimate_enemy_weapon_bonus(target),
                                       defense, region_weather)
            # v1.6.0: attack if we deal >= 60% of enemy damage OR target HP is low
            kill_threshold = my_dmg * 5
            if my_dmg >= guardian_dmg * 0.6 or target.get("hp", 100) <= kill_threshold:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: HP={target.get('hp','?')} "
                                  f"(120 sMoltz! dmg={my_dmg} vs {guardian_dmg})"}

    # ── P7: Agent combat ───────────────────────────────────────────────
    # v1.6.0: much more aggressive!
    # - Late game (< 15 alive): attack anyone HP > 20, we need kills for ranking
    # - Mid game: attack if we deal >= enemy OR target HP <= 2x our damage
    # - HP threshold: 30 (was 40) — fight more!
    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != my_id]

    late_game   = alive_count <= 15
    mid_game    = alive_count <= 30
    hp_min_fight = 20 if late_game else 30

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

            # Late game: kill anyone we can
            if late_game and target_hp <= my_dmg * 6:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"LATE AGGRO: alive={alive_count}, target HP={target_hp}"}

            # Favourable fight: we deal more, OR target is close to dead
            if my_dmg >= enemy_dmg or target_hp <= my_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: dmg={my_dmg} vs {enemy_dmg}, target HP={target_hp}"}

    # ── P8: Monster farming ────────────────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2:
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER: {target.get('name','?')} HP={target.get('hp','?')}"}

    # ── P9: Proactive heal (HP < 55 + area safe) ──────────────────────
    # v1.6.0: heal at 55 when safe (was 70 but emergency only) — stay topped up!
    area_safe = not enemies and not guardians_here
    if hp < 55 and area_safe:
        heal = _find_healing_item(inventory, critical=(hp < 25))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"PROACTIVE HEAL: HP={hp}, area safe"}

    # ── P10: Facility interaction ──────────────────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type','unknown')}"}

    # ── P11: Strategic movement ────────────────────────────────────────
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          guardians, visible_agents)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "MOVE: Repositioning strategically"}

    # ── P12: Rest ONLY when EP <= 1 ───────────────────────────────────
    # v1.6.0: was EP < 4 — that wasted too many turns!
    # EP regens +1 automatically every turn anyway.
    # Only rest when truly can't do anything (EP too low to attack or move).
    if ep <= 1 and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, truly idle (+1 bonus EP)"}

    return None  # Wait for next turn


# ── Helpers ───────────────────────────────────────────────────────────

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water":
        return 3
    if weather == "storm":
        return 3
    return 2

def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)

def _select_best_target(targets: list, my_atk: int, my_bonus: int,
                         my_def: int, weather: str) -> dict | None:
    """
    v1.6.0: Smart target selection.
    Score = how many turns to kill / how many turns they kill us.
    Lower = better target (easier to kill, less dangerous).
    Strongly prefer targets that are almost dead.
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

        turns_to_kill = t_hp / my_dmg          # lower = easier
        turns_to_die  = 100 / their_dmg        # higher = safer

        # Score: prefer targets we kill fast AND that don't hurt us much
        score = turns_to_die / turns_to_kill   # higher = better target
        # Big bonus for almost-dead targets (secure the kill!)
        if t_hp <= my_dmg * 2:
            score += 10
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
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    # Last resort
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
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0), reverse=True)
    else:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0))
    return heals[0]

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None

def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    if len(inventory) >= 10:
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
        return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0)
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
        if ftype == "medical_facility" and hp < 85:   # v1.6.0: heal at 85, not 80
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
        for d in dead:
            del _known_agents[d]

def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
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
            terrain_value = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
            safe_regions.append((rid, len(conns) + terrain_value))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("🗺️ MAP LEARNED: %d DZ, top center: %s",
             len(_map_knowledge["death_zones"]), _map_knowledge["safe_center"][:3])

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         guardians: list = None, visible_agents: list = None) -> str | None:
    """
    v1.6.0: Smarter movement.
    - Chase guardian regions (120 sMoltz!)
    - Chase item-rich regions
    - Prefer hills > plains > ruins > forest (avoid water)
    - NEVER move into DZ or pending DZ
    """
    candidates = []
    item_regions = {i.get("regionId", "") for i in visible_items if isinstance(i, dict)}

    # Build guardian region set for attraction
    guardian_regions = set()
    if guardians:
        for g in guardians:
            if g.get("isAlive", True):
                guardian_regions.add(g.get("regionId", ""))

    for conn in connections:
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            score = 1
            if conn in item_regions:
                score += 5
            if conn in guardian_regions:
                score += 8  # Chase guardians!
            candidates.append((conn, score))

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            if rid in _map_knowledge.get("death_zones", set()):
                continue

            score = 0
            terrain = conn.get("terrain", "").lower()
            score += {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -3}.get(terrain, 0)

            if rid in item_regions:
                score += 5
            if rid in guardian_regions:
                score += 8  # Chase guardians for sMoltz!

            facs = conn.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 2

            weather = conn.get("weather", "").lower()
            score += {"storm": -2, "fog": -1, "rain": 0, "clear": 1}.get(weather, 0)

            if alive_count < 30:
                score += 2  # Move more aggressively in late game

            if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                score += 5

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]

