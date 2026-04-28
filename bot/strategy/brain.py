"""
Strategy brain — main decision engine with priority-based action selection.

v3.0 OPTIMAL — gabungan terbaik v1.6.0 + v2.0 + fix bug kritis

BUG FIX KRITIS (dari log Railway):
- Bot stuck di "Pond" (water) Turn 13-49 tanpa bergerak!
  Root cause: move_ep_cost=4 di water benar, tapi TIDAK ada paksa keluar.
  Fix: P0 baru — "WATER ESCAPE" jika di water region, langsung pindah.
- Juga fix: olghaa.py set move_ep_cost=3 flat, tapi game docs pakai 2 base.
  Dari log: bot di turn awal berhasil move normal, EP regen tiap turn 1,
  artinya base cost adalah 2 (normal). Water memang lebih mahal → 3.

BALANCE FIXES vs brain.py (v1.6.0 terlalu agresif):
- Critical heal threshold: 35 (antara 25 terlalu rendah dan 40 terlalu tinggi)
- Guardian flee threshold: 35 jika tidak punya heals, 20 jika punya heals
- Guardian attack: HP >= 50, EP >= 2 (v1.6: HP>=30 terlalu berani)
- Agent combat early game: hanya jika dmg advantage JELAS (my_dmg > enemy_dmg)
- Proactive heal threshold: 65 ketika aman (v1.6: 55 terlalu rendah)
- Rest: EP <= 2 (bukan <= 1 — pastikan bisa gerak+attack giliran berikutnya)
- Inventory: simpan 1 slot untuk sponsor item (max 9)

TETAP AGRESIF:
- Late game (<=20 alive): attack siapapun dalam range
- Guardian farming tetap high priority (120 sMoltz!)
- Gerak chase guardian/item region
- Movement scoring lebih baik: Hills > Ruins > Plains > Forest, hindari Water

Ranking formula game: Kills > HP sisa
Strategi: SURVIVE + FARM KILLS + hoard heals untuk late game
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
    Main decision engine. Priority chain (v3.0 OPTIMAL):

    P0.  WATER ESCAPE — [BUG FIX] jika di water/pond, langsung keluar!
    P1.  DEATHZONE ESCAPE — instant, overrides all
    P1b. Pre-escape pending death zone
    P2.  Critical heal (HP < 35) — sebelum semua aksi lain
    P2b. Guardian threat evasion (HP rendah, tidak ada heal)
    P3.  FREE ACTIONS: pickup, equip, utility items
    P4.  EP recovery (energy drink jika EP=0)
    P5.  Guardian farming — HP >= 50, EP >= 2, hanya jika advantaged
    P6.  Enemy agent combat — damage advantage atau target lemah
    P7.  Monster farming — EP >= 2
    P8.  Proactive heal (HP < 65, area aman)
    P9.  Facility interaction
    P10. Strategic movement
    P11. Rest ONLY ketika EP <= 2
    """
    self_data     = view.get("self", {})
    region        = view.get("currentRegion", {})
    hp            = self_data.get("hp", 100)
    ep            = self_data.get("ep", 10)
    max_ep        = self_data.get("maxEp", 10)
    atk           = self_data.get("atk", 10)
    defense       = self_data.get("def", 5)
    is_alive      = self_data.get("isAlive", True)
    inventory     = self_data.get("inventory", [])[:]
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

    interactables  = region.get("interactables", [])
    region_id      = region.get("id", "") or my_region_id
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""
    connections    = connected_regions or region.get("connections", [])

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

    # Move EP cost: base=2, water=3, storm=3 (sesuai observasi log game nyata)
    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # Game phase
    early_game = alive_count > 70
    mid_game   = 20 < alive_count <= 70
    late_game  = alive_count <= 20

    # ── P0: WATER ESCAPE [BUG FIX KRITIS] ─────────────────────────────
    # Bot pernah stuck di "Pond" (water) 40 turn! Jika di water, keluar dulu.
    # Water tidak punya item/fasilitas berguna, membuat movement scoring gagal.
    if region_terrain == "water" and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("🌊 DI WATER TERRAIN! Keluar segera ke %s (bug fix)", safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"WATER ESCAPE: Keluar dari water terrain (EP={ep})"}

    # ── P1: DEATHZONE ESCAPE ──────────────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 DI DEATH ZONE! Escape ke %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: Di death zone! HP={hp}"}
        log.error("🚨 DI DEATH ZONE tapi TIDAK ADA REGION AMAN!")

    # ── P1b: Pre-escape pending DZ ─────────────────────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region akan jadi death zone"}

    # ── P2: Critical heal ─────────────────────────────────────────────
    # Threshold 35: satu hit guardian (~8-10 dmg) masih aman, tapi dua hit tidak.
    if hp < 35:
        heal = _find_healing_item(inventory, critical=True)
        if heal and can_act:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp} berbahaya!"}

    # ── P2b: Guardian threat evasion ──────────────────────────────────
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    has_heals = bool(_find_healing_item(inventory, critical=False))
    flee_threshold = 35 if not has_heals else 20
    if guardians_here and hp < flee_threshold and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat + HP=%d (<%d), flee", hp, flee_threshold)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp} kritis"}

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

    # ── P4: EP recovery ────────────────────────────────────────────────
    if ep == 0:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": "EP RECOVERY: EP=0, pakai energy drink (+5 EP)"}

    # ── P5: Guardian farming ────────────────────────────────────────────
    # Guardian: HP=150, ATK=10, DEF=5. Reward: 120 sMoltz. SANGAT berharga!
    # Syarat: HP >= 50 (margin dari counter-hits), EP >= 2
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 50:
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
            # Hitung estimasi cost HP untuk bunuh guardian
            turns_to_kill = target_hp / max(1, my_dmg)
            hp_cost_estimate = guardian_dmg * min(turns_to_kill, 8)
            # Serang jika:
            # - Kita deal lebih banyak damage dari mereka, ATAU target hampir mati
            # - DAN kita tidak akan kehabisan HP dalam proses (sisa > 20)
            if (my_dmg >= guardian_dmg * 0.7 or target_hp <= my_dmg * 3) \
               and hp - hp_cost_estimate > 20:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: HP={target_hp} "
                                  f"(120 sMoltz! dmg={my_dmg} vs {guardian_dmg})"}

    # ── P6: Agent combat ───────────────────────────────────────────────
    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != my_id]

    # HP minimum untuk fight: lebih tinggi early game (perlu margin untuk kesalahan)
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

            # Late game: kill siapapun yang bisa kita bunuh
            if late_game and target_hp <= my_dmg * 5:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"LATE AGGRO: alive={alive_count}, target HP={target_hp}"}

            # Mid/Early: fight hanya jika jelas menang
            # Syarat: kita deal lebih banyak dmg ATAU target hampir mati
            # Safety: jangan fight jika mereka bisa bunuh kita dalam 2 hit
            if (my_dmg > enemy_dmg or target_hp <= my_dmg * 2) and hp > enemy_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: dmg={my_dmg} vs {enemy_dmg}, target HP={target_hp}"}

    # ── P7: Monster farming ────────────────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2:
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER: {target.get('name','?')} HP={target.get('hp','?')}"}

    # ── P8: Proactive heal (HP < 65, area aman) ───────────────────────
    area_safe = not enemies and not guardians_here
    if hp < 65 and area_safe:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"PROACTIVE HEAL: HP={hp}, area aman"}

    # ── P9: Facility interaction ──────────────────────────────────────
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
                                          guardians, visible_agents,
                                          region_terrain)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "MOVE: Repositioning strategis"}

    # ── P11: Rest — EP <= 2 (pastikan bisa gerak+attack giliran berikut) ─
    if ep <= 2 and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, isi energi (+1 bonus EP)"}

    return None  # Tunggu giliran berikutnya


# ── Helpers ───────────────────────────────────────────────────────────

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    """
    Base move cost: 2 EP (dari observasi log — bot berhasil move normal di awal).
    Water terrain: +1 = 3 EP total (lebih berat).
    Storm: +1 = 3 EP total.
    Normal: 2 EP.
    """
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
    Smart target selection.
    Score = efisiensi membunuh vs seberapa berbahaya mereka.
    Prefer: HP rendah (mudah dibunuh), damage rendah (fight aman).
    """
    if not targets:
        return None

    scored = []
    for t in targets:
        t_hp    = t.get("hp", 100)
        t_def   = t.get("def", 5)
        t_atk   = t.get("atk", 10)
        t_bonus = _estimate_enemy_weapon_bonus(t)

        my_dmg    = max(1, calc_damage(my_atk, my_bonus, t_def, weather))
        their_dmg = max(1, calc_damage(t_atk, t_bonus, my_def, weather))

        turns_to_kill = t_hp / my_dmg
        turns_to_die  = 100 / their_dmg

        score = turns_to_die / turns_to_kill
        # Bonus besar untuk target hampir mati (amankan kill!)
        if t_hp <= my_dmg * 2:
            score += 15
        # Bonus untuk target yang tidak terlalu berbahaya
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
                safe_regions.append((conn, 1))
        elif isinstance(conn, dict):
            rid    = conn.get("id", "")
            is_dz  = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                # Hindari water saat flee (bisa stuck lagi!)
                score = {"hills": 4, "plains": 3, "ruins": 3,
                         "forest": 2, "water": -5}.get(terrain, 1)
                if conn.get("interactables"):
                    score += 1
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    # Last resort: region apapun yang bukan DZ
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
        # Pakai item terkuat saat kritis
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
    else:
        # Pakai item terlemah dulu (hemat yang kuat untuk kritis)
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
    return heals[0]

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None

def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    # Simpan 1 slot untuk sponsor item (max 9)
    if len(inventory) >= 9:
        return None
    local_items = [i for i in items if isinstance(i, dict) and i.get("regionId") == region_id]
    if not local_items:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local_items:
        return None
    heal_count = sum(1 for i in inventory
                     if isinstance(i, dict)
                     and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0)
    local_items.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best = local_items[0]
    if _pickup_score(best, inventory, heal_count) > 0:
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {best.get('typeId', 'item')}"}
    return None

def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    type_id  = item.get("typeId", "").lower()
    category = item.get("category", "").lower()
    if type_id == "rewards" or category == "currency":
        return 300
    if category == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        current_best = max(
            (WEAPONS.get(i.get("typeId", "").lower(), {}).get("bonus", 0)
             for i in inventory if isinstance(i, dict) and i.get("category") == "weapon"),
            default=0)
        return (100 + bonus) if bonus > current_best else 0
    if type_id == "binoculars":
        has_binos = any(i.get("typeId", "").lower() == "binoculars"
                        for i in inventory if isinstance(i, dict))
        return 55 if not has_binos else 0
    if type_id == "map":
        return 52
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        # Selalu ambil heals sampai 5 stack; bonus jika stok rendah
        return ITEM_PRIORITY.get(type_id, 0) + (15 if heal_count < 5 else 0)
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
        bonus = WEAPONS.get(item.get("typeId", "").lower(), {}).get("bonus", 0)
        if bonus > best_bonus:
            best = item
            best_bonus = bonus
    if best:
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best.get('typeId', 'weapon')} (+{best_bonus} ATK)"}
    return None

def _select_facility(interactables: list, hp: int, ep: int) -> dict | None:
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
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
        # Pakai map segera untuk reveal semua region (bantu movement scoring)
        if type_id == "map":
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "UTILITY: Pakai Map — reveal seluruh map"}
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
            # Hills: vision +2. Ruins: banyak item. Water: hindari (bug stuck!)
            terrain_value = {"hills": 5, "ruins": 3, "plains": 2,
                             "forest": 1, "water": -3}.get(terrain, 0)
            safe_regions.append((rid, len(conns) + terrain_value))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("🗺️ MAP LEARNED: %d DZ, top center: %s",
             len(_map_knowledge["death_zones"]), _map_knowledge["safe_center"][:3])

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         guardians: list = None, visible_agents: list = None,
                         current_terrain: str = "") -> str | None:
    """
    v3.0 Movement scoring:
    - [BUG FIX] Water terrain mendapat penalti BESAR (-6) — jangan masuk
    - Hills (vision) >> Ruins (items) >> Plains >> Forest >> Water (HINDARI)
    - Chase guardian regions (120 sMoltz!)
    - Chase item-rich regions
    - Late game: cari musuh + pergi ke center
    - NEVER ke DZ atau pending DZ
    """
    candidates = []
    item_regions = {i.get("regionId", "") for i in visible_items if isinstance(i, dict)}

    guardian_regions = set()
    if guardians:
        for g in guardians:
            if g.get("isAlive", True):
                guardian_regions.add(g.get("regionId", ""))

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
                score += 8
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

            # [BUG FIX] Water sangat dihindari karena bisa stuck!
            # Hills: vision bagus. Ruins: banyak item/fasilitas. Plains/Forest: normal.
            score += {"hills": 6, "ruins": 4, "plains": 3,
                      "forest": 2, "water": -6}.get(terrain, 1)

            if rid in item_regions:
                score += 6
            if rid in guardian_regions:
                score += 8  # Chase guardian untuk sMoltz!

            # Fasilitas yang belum dipakai sangat berharga
            facs   = conn.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 3

            weather = conn.get("weather", "").lower()
            score += {"storm": -3, "fog": -2, "rain": 0, "clear": 1}.get(weather, 0)

            if late_game:
                if rid in enemy_regions:
                    score += 4
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 7  # Late game: pergi ke center (jauh dari shrinking DZ)

            if not early_game:
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 3

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]
