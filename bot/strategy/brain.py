"""
brain.py v3.0 — AGGRESSIVE + SURVIVE
=====================================
Target: Banyak kill, tidak mati cepat, selalu ada aksi tiap turn.

Perubahan dari v1.6.1:
- TIDAK ADA return None — bot SELALU melakukan sesuatu tiap turn
- Attack threshold diturunkan: mau fight lebih sering
- Fallback WAJIB: kalau tidak ada target, gerak → kalau tidak bisa gerak, rest
- Water escape: langsung keluar kalau di terrain water
- Heal lebih agresif: HP < 75 langsung heal kalau aman (tidak buang nyawa)
- Move EP cost fix: base = 2 (bukan 3), water = 3
- Guardian: attack kalau HP >= 40 saja (bukan 30) untuk survive lebih lama
- Tidak ada bug item ID double-use
"""

from bot.utils.logger import get_logger
log = get_logger(__name__)

# ── Senjata ────────────────────────────────────────────────────────────
WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword":  {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow":    {"bonus": 5,  "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20, "emergency_rations": 20,
}

WEATHER_PENALTY = {
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}

ITEM_PRIORITY = {
    "rewards": 300,
    "katana": 100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger": 80, "bow": 75,
    "medkit": 70, "bandage": 65, "emergency_food": 60, "emergency_rations": 60,
    "energy_drink": 58, "binoculars": 55, "map": 52, "megaphone": 40,
}

# ── State global ───────────────────────────────────────────────────────
_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_last_used_item_id: str = ""

def reset_game_state():
    global _known_agents, _map_knowledge, _last_used_item_id
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _last_used_item_id = ""
    log.info("🔄 Brain reset untuk game baru")

# ── Kalkulasi damage ───────────────────────────────────────────────────
def calc_damage(atk, bonus, target_def, weather="clear"):
    base = atk + bonus - int(target_def * 0.5)
    return max(1, int(base * (1 - WEATHER_PENALTY.get(weather, 0.0))))

def weapon_bonus(equipped):
    if not equipped:
        return 0
    return WEAPONS.get(equipped.get("typeId", "").lower(), {}).get("bonus", 0)

def weapon_range(equipped):
    if not equipped:
        return 0
    return WEAPONS.get(equipped.get("typeId", "").lower(), {}).get("range", 0)

def enemy_weapon_bonus(agent):
    w = agent.get("equippedWeapon")
    if not w or not isinstance(w, dict):
        return 0
    return WEAPONS.get(w.get("typeId", "").lower(), {}).get("bonus", 0)

# ── Helpers region ─────────────────────────────────────────────────────
def _resolve_region(entry, view):
    if isinstance(entry, dict):
        return entry
    for r in view.get("visibleRegions", []):
        if isinstance(r, dict) and r.get("id") == entry:
            return r
    return None

def _region_id(entry):
    if isinstance(entry, str): return entry
    if isinstance(entry, dict): return entry.get("id", "")
    return ""

def _move_ep_cost(terrain, weather):
    # Per game docs: base move = 2 EP. Water = 3. Storm juga +1.
    if terrain == "water": return 3
    if weather == "storm": return 3
    return 2

def _find_safe_region(connections, danger_ids, view=None):
    scored = []
    for conn in connections:
        rid = _region_id(conn)
        if not rid: continue
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if is_dz or rid in danger_ids: continue
        if rid in _map_knowledge.get("death_zones", set()): continue
        terrain = conn.get("terrain", "").lower() if isinstance(conn, dict) else ""
        score = {"hills": 4, "plains": 3, "ruins": 2, "forest": 1, "water": -3}.get(terrain, 0)
        scored.append((rid, score))
    if scored:
        return max(scored, key=lambda x: x[1])[0]
    # Last resort — any non-DZ
    for conn in connections:
        rid = _region_id(conn)
        if rid and (not isinstance(conn, dict) or not conn.get("isDeathZone")):
            return rid
    return None

def _in_range(target, my_region, w_range, connections):
    tr = target.get("regionId", "")
    if not tr or tr == my_region: return True
    if w_range >= 1:
        adj = {_region_id(c) for c in connections}
        if tr in adj: return True
    return False

# ── Item helpers ───────────────────────────────────────────────────────
def _find_heal(inventory, strong=False):
    heals = [i for i in inventory
             if isinstance(i, dict)
             and RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0) > 0]
    if not heals: return None
    heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0),
               reverse=strong)
    return heals[0]

def _find_energy_drink(inventory):
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId","").lower() == "energy_drink":
            return i
    return None

def _pickup_score(item, inventory, heal_count):
    tid = item.get("typeId","").lower()
    cat = item.get("category","").lower()
    if tid in ("rewards",) or cat == "currency": return 300
    if cat == "weapon":
        bonus = WEAPONS.get(tid, {}).get("bonus", 0)
        best_now = max(
            (WEAPONS.get(i.get("typeId","").lower(), {}).get("bonus", 0)
             for i in inventory if isinstance(i, dict) and i.get("category") == "weapon"),
            default=0)
        return (100 + bonus) if bonus > best_now else 0
    if tid in RECOVERY_ITEMS and RECOVERY_ITEMS[tid] > 0:
        return ITEM_PRIORITY.get(tid, 0) + (10 if heal_count < 4 else 0)
    return ITEM_PRIORITY.get(tid, 0)

def _check_pickup(items, inventory, region_id):
    if len(inventory) >= 9: return None
    local = [i for i in items if isinstance(i, dict)
             and (i.get("regionId","") == region_id or not i.get("regionId",""))]
    if not local: local = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local: return None
    heal_count = sum(1 for i in inventory
                     if isinstance(i, dict) and RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0) > 0)
    local.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best = local[0]
    if _pickup_score(best, inventory, heal_count) > 0:
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {best.get('typeId','item')}"}
    return None

def _check_equip(inventory, equipped):
    cur = weapon_bonus(equipped)
    best = None
    best_b = cur
    for i in inventory:
        if not isinstance(i, dict) or i.get("category") != "weapon": continue
        b = WEAPONS.get(i.get("typeId","").lower(), {}).get("bonus", 0)
        if b > best_b:
            best = i
            best_b = b
    if best:
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best.get('typeId')} (+{best_b})"}
    return None

def _select_best_target(targets, my_atk, my_bonus, my_def, weather):
    if not targets: return None
    scored = []
    for t in targets:
        my_dmg    = max(1, calc_damage(my_atk, my_bonus, t.get("def", 5), weather))
        their_dmg = max(1, calc_damage(t.get("atk", 10), enemy_weapon_bonus(t), my_def, weather))
        t_hp = t.get("hp", 100)
        score = (100 / their_dmg) / (t_hp / my_dmg)
        if t_hp <= my_dmg * 2: score += 20   # bonus hampir mati
        if t_hp <= my_dmg:     score += 40   # bisa one-shot
        scored.append((t, score))
    return max(scored, key=lambda x: x[1])[0]

def _select_weakest(targets):
    return min(targets, key=lambda t: t.get("hp", 999))

def _choose_move(connections, danger_ids, region, visible_items, alive_count,
                 guardians, agents):
    item_regions = {i.get("regionId","") for i in visible_items if isinstance(i, dict)}
    guardian_regions = {g.get("regionId","") for g in (guardians or []) if g.get("isAlive")}
    enemy_regions = {a.get("regionId","") for a in (agents or [])
                     if not a.get("isGuardian") and a.get("isAlive")}
    late = alive_count <= 20

    candidates = []
    for conn in connections:
        rid = _region_id(conn)
        if not rid: continue
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if is_dz or rid in danger_ids: continue
        if rid in _map_knowledge.get("death_zones", set()): continue

        score = 0
        if isinstance(conn, dict):
            t = conn.get("terrain","").lower()
            w = conn.get("weather","").lower()
            score += {"hills": 5, "ruins": 3, "plains": 2, "forest": 1, "water": -5}.get(t, 0)
            score += {"clear": 1, "rain": 0, "fog": -1, "storm": -2}.get(w, 0)
            unused = [f for f in conn.get("interactables",[]) if isinstance(f,dict) and not f.get("isUsed")]
            score += len(unused) * 2
            if rid in _map_knowledge.get("safe_center", []): score += 3
        else:
            score = 1

        if rid in item_regions: score += 6
        if rid in guardian_regions: score += 8
        if late and rid in enemy_regions: score += 5

        candidates.append((rid, score))

    if not candidates: return None
    return max(candidates, key=lambda x: x[1])[0]

def _track_agents(agents, my_id, region_id):
    global _known_agents
    for a in agents:
        if not isinstance(a, dict): continue
        aid = a.get("id","")
        if not aid or aid == my_id: continue
        _known_agents[aid] = {
            "hp": a.get("hp", 100), "atk": a.get("atk", 10),
            "isGuardian": a.get("isGuardian", False),
            "equippedWeapon": a.get("equippedWeapon"),
            "isAlive": a.get("isAlive", True),
        }
    if len(_known_agents) > 60:
        dead = [k for k,v in _known_agents.items() if not v.get("isAlive",True)]
        for d in dead[:20]: del _known_agents[d]

def learn_from_map(view):
    global _map_knowledge
    regions = view.get("visibleRegions", [])
    if not regions: return
    _map_knowledge["revealed"] = True
    safe = []
    for r in regions:
        if not isinstance(r, dict): continue
        rid = r.get("id","")
        if not rid: continue
        if r.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            t = r.get("terrain","").lower()
            tv = {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -2}.get(t, 0)
            safe.append((rid, len(r.get("connections",[])) + tv))
    safe.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe[:5]]


# ══════════════════════════════════════════════════════════════════════
#  FUNGSI UTAMA — decide_action
#  JAMINAN: selalu return aksi, TIDAK PERNAH return None saat can_act=True
# ══════════════════════════════════════════════════════════════════════

def decide_action(view: dict, can_act: bool, memory_temp: dict = None, turn: int = 0) -> dict | None:
    global _last_used_item_id

    self_data  = view.get("self", {})
    region     = view.get("currentRegion", {})
    hp         = self_data.get("hp", 100)
    ep         = self_data.get("ep", 10)
    max_ep     = self_data.get("maxEp", 10)
    atk        = self_data.get("atk", 10)
    defense    = self_data.get("def", 5)
    is_alive   = self_data.get("isAlive", True)
    inventory  = self_data.get("inventory", [])
    equipped   = self_data.get("equippedWeapon")
    my_id      = self_data.get("id", "")

    vis_agents   = view.get("visibleAgents", [])
    vis_monsters = view.get("visibleMonsters", [])
    vis_items_raw= view.get("visibleItems", [])
    conn_regions = view.get("connectedRegions", [])
    pending_dz   = view.get("pendingDeathzones", [])
    alive_count  = view.get("aliveCount", 100)

    if not is_alive:
        return None

    # Unwrap visibleItems
    vis_items = []
    for entry in vis_items_raw:
        if not isinstance(entry, dict): continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId","")
            vis_items.append(inner)
        elif entry.get("id"):
            vis_items.append(entry)

    region_id     = region.get("id","") or self_data.get("regionId","")
    terrain       = region.get("terrain","").lower() if isinstance(region, dict) else ""
    weather       = region.get("weather","").lower() if isinstance(region, dict) else ""
    interactables = region.get("interactables",[])
    connections   = conn_regions or region.get("connections",[])
    move_cost     = _move_ep_cost(terrain, weather)

    # Inventory ID set (cegah re-use item yang sudah dikonsumsi)
    inv_ids = {i["id"] for i in inventory if isinstance(i, dict) and i.get("id")}
    if _last_used_item_id and _last_used_item_id not in inv_ids:
        _last_used_item_id = ""

    # Danger zone set
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict): danger_ids.add(dz.get("id",""))
        elif isinstance(dz, str): danger_ids.add(dz)
    for conn in connections:
        r = _resolve_region(conn, view)
        if r and r.get("isDeathZone"): danger_ids.add(r.get("id",""))

    _track_agents(vis_agents, my_id, region_id)

    guardians      = [a for a in vis_agents if a.get("isGuardian") and a.get("isAlive",True)]
    enemies        = [a for a in vis_agents if not a.get("isGuardian") and a.get("isAlive",True) and a.get("id") != my_id]
    guardians_here = [g for g in guardians if g.get("regionId","") == region_id]
    monsters       = [m for m in vis_monsters if m.get("hp",0) > 0]
    my_bonus       = weapon_bonus(equipped)
    my_range       = weapon_range(equipped)

    # ── P0: WATER ESCAPE ──────────────────────────────────────────────
    if terrain == "water" and ep >= move_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("🌊 WATER — keluar ke %s", safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"WATER ESCAPE EP={ep}"}

    # ── P1: DEATHZONE ESCAPE ──────────────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_cost:
            log.warning("🚨 DEATH ZONE! Kabur ke %s HP=%d", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE DEATHZONE HP={hp}"}

    # ── P1b: Pre-escape ───────────────────────────────────────────────
    if region_id in danger_ids and ep >= move_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE DZ datang!"}

    # ── P2: FREE ACTIONS ──────────────────────────────────────────────
    pickup = _check_pickup(vis_items, inventory, region_id)
    if pickup: return pickup

    equip = _check_equip(inventory, equipped)
    if equip: return equip

    for item in inventory:
        if isinstance(item, dict) and item.get("typeId","").lower() == "map":
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "MAP reveal"}

    if not can_act:
        return None

    # ── P3: Critical heal (HP < 40) ───────────────────────────────────
    if hp < 40:
        heal = _find_heal(inventory, strong=True)
        if heal and heal.get("id","") != _last_used_item_id:
            _last_used_item_id = heal["id"]
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL HP={hp}"}

    # ── P4: Guardian flee (HP < 30, no heals) ─────────────────────────
    has_heals = bool(_find_heal(inventory))
    if guardians_here and hp < 30 and not has_heals and ep >= move_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"FLEE guardian HP={hp}"}

    # ── P5: Energy drink (EP=0) ───────────────────────────────────────
    if ep == 0:
        ed = _find_energy_drink(inventory)
        if ed:
            return {"action": "use_item", "data": {"itemId": ed["id"]},
                    "reason": "ENERGY DRINK EP=0"}

    # ── P6: ATTACK GUARDIAN ───────────────────────────────────────────
    if guardians and ep >= 2 and hp >= 40:
        tgt = _select_best_target(guardians, atk, my_bonus, defense, weather)
        if tgt and _in_range(tgt, region_id, my_range, connections):
            my_dmg = calc_damage(atk, my_bonus, tgt.get("def",5), weather)
            g_dmg  = calc_damage(tgt.get("atk",10), enemy_weapon_bonus(tgt), defense, weather)
            tgt_hp = tgt.get("hp",150)
            if my_dmg >= g_dmg * 0.5 or tgt_hp <= my_dmg * 4:
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN dmg={my_dmg} targetHP={tgt_hp}"}

    # ── P7: ATTACK ENEMY AGENT ────────────────────────────────────────
    late   = alive_count <= 20
    hp_min = 20 if late else 25

    if enemies and ep >= 2 and hp >= hp_min:
        tgt = _select_best_target(enemies, atk, my_bonus, defense, weather)
        if tgt and _in_range(tgt, region_id, my_range, connections):
            my_dmg = calc_damage(atk, my_bonus, tgt.get("def",5), weather)
            e_dmg  = calc_damage(tgt.get("atk",10), enemy_weapon_bonus(tgt), defense, weather)
            tgt_hp = tgt.get("hp",100)

            if tgt_hp <= my_dmg * 2:  # bisa mati dalam 2 hit
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"KILL SHOT HP={tgt_hp}"}
            if late and tgt_hp <= my_dmg * 6:
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"LATE AGGRO alive={alive_count}"}
            if my_dmg >= e_dmg and hp > e_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"COMBAT dmg={my_dmg} vs {e_dmg}"}

    # ── P8: ATTACK MONSTER ────────────────────────────────────────────
    if monsters and ep >= 2:
        tgt = _select_weakest(monsters)
        if _in_range(tgt, region_id, my_range, connections):
            return {"action": "attack",
                    "data": {"targetId": tgt["id"], "targetType": "monster"},
                    "reason": f"MONSTER HP={tgt.get('hp','?')}"}

    # ── P9: Proactive heal (HP < 75, aman) ───────────────────────────
    area_safe = not enemies and not guardians_here
    if hp < 75 and area_safe:
        heal = _find_heal(inventory, strong=False)
        if heal and heal.get("id","") != _last_used_item_id:
            _last_used_item_id = heal["id"]
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL HP={hp}"}

    # ── P10: Facility ─────────────────────────────────────────────────
    if interactables and ep >= 2:
        for fac in interactables:
            if not isinstance(fac, dict) or fac.get("isUsed"): continue
            ftype = fac.get("type","").lower()
            if ftype == "medical_facility" and hp < 90:
                return {"action": "interact",
                        "data": {"interactableId": fac["id"]},
                        "reason": f"FACILITY medical HP={hp}"}
            if ftype in ("supply_cache","watchtower","broadcast_station"):
                return {"action": "interact",
                        "data": {"interactableId": fac["id"]},
                        "reason": f"FACILITY {ftype}"}

    # ── P11: MOVE ─────────────────────────────────────────────────────
    if ep >= move_cost and connections:
        dest = _choose_move(connections, danger_ids, region, vis_items,
                            alive_count, guardians, vis_agents)
        if dest:
            return {"action": "move", "data": {"regionId": dest},
                    "reason": "MOVE reposition"}

    # ── P12: REST — FALLBACK WAJIB, TIDAK PERNAH DIAM ────────────────
    # Ini adalah safety net. Bot TIDAK BOLEH return None saat can_act=True.
    # Rest selalu lebih baik dari diam karena dapat +1 EP bonus.
    log.info("💤 REST fallback EP=%d/%d HP=%d", ep, max_ep, hp)
    return {"action": "rest", "data": {},
            "reason": f"REST EP={ep}/{max_ep} HP={hp}"}
