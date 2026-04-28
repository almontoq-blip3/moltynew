"""
Strategy brain — main decision engine with priority-based action selection.

v4.0 CRITICAL BUG FIXES

═══════════════════════════════════════════════════════════════════
BUG #1 FIXED — "City Hall Freeze" (sama seperti Pond bug tapi berbeda)
═══════════════════════════════════════════════════════════════════
Log evidence:
  Turn 18: HP=76, EP=6, Region=City Hall
  Turn 19: HP=68, EP=7  ← EP naik (regen pasif) — TIDAK ADA AKSI!
  Turn 20: HP=59, EP=8  ← TIDAK ADA AKSI!
  Turn 21: HP=51, EP=9  ← TIDAK ADA AKSI!
  Turn 22: HP=42, EP=10 ← EP=10 (full), TIDAK ADA AKSI!
  Turn 23: HP=34, EP=10 ← Sudah di bawah threshold critical heal 35, TETAP DIAM
  Turn 24: HP=26, EP=10 ← TIDAK ADA AKSI! (mati)
  Turn 25: HP=26, EP=10 ← bot mati atau nyaris mati

Root cause: can_act=False (cooldown) memblokir SEMUA aksi di P2+.
  - P2 Critical heal: cek `if heal and can_act:` → skip jika can_act=False
  - P3 util_action (use_item=map): butuh can_act=True (main action)
  - P8 Proactive heal: juga butuh can_act=True
  - Bot menunggu cooldown selesai, tapi setiap turn cooldown belum kelar
    atau ada bug di websocket_engine yang tidak reset can_act dengan benar.

FIX:
  1. use_item (heal) adalah MAIN ACTION → butuh can_act=True, BENAR.
     TAPI: saat HP kritis (< 35), kita HARUS menunggu cooldown selesai
     dan langsung heal di turn pertama can_act=True. Ini sudah benar.
     
  2. BUG SEBENARNYA: saat can_act=False, bot return None sehingga
     engine tidak mengirim apa-apa. Tapi EP terus naik (regen pasif),
     artinya bot "idle" selama beberapa turn berturut-turut.
     
     Kemungkinan 1: cooldown di engine tidak clear — brain terus dipanggil
     dengan can_act=False. Fix: setelah beberapa turn idle, paksa REST
     (walaupun EP penuh) untuk "consume" turn dan reset cooldown state.
     
     Kemungkinan 2: heal threshold 35 tidak cukup tinggi — saat HP=42 (Turn 21)
     belum kritis, tapi sudah berbahaya. Naikkan threshold ke 50.
     
     Kemungkinan 3: _use_utility_item (map) dipanggil SEBELUM can_act check,
     map adalah use_item yang butuh main action. → FIX: pindahkan setelah
     can_act check, atau skip map jika sedang kritis.

  3. NEW SURVIVAL LOGIC: "Under Attack" detection
     Jika HP turun > 8 dari turn sebelumnya = sedang diserang.
     Jika under_attack + HP < 60: PRIORITAS FLEE sebelum apapun!
     Jika under_attack + bisa heal: HEAL SEGERA!

═══════════════════════════════════════════════════════════════════
BUG #2 FIXED — use_item (Map) sebelum can_act check
═══════════════════════════════════════════════════════════════════
Map adalah use_item = MAIN ACTION. Tapi di v3.0, _use_utility_item dipanggil
di P3 (sebelum `if not can_act: return None`). Padahal use_item butuh can_act!
Fix: Pindahkan map usage ke SETELAH can_act check.
Pickup & Equip tetap di P3 (free actions, tidak butuh can_act).

═══════════════════════════════════════════════════════════════════
BUG #3 FIXED — Rest threshold terlalu ketat (EP <= 2)
═══════════════════════════════════════════════════════════════════
Saat can_act=False, bot harusnya masih bisa signal "REST" sebagai idle action.
Rest dinaikkan ke EP <= 3 dan ditambah: jika can_act=False dan EP < max_ep,
coba REST dulu agar engine bisa reset state.

═══════════════════════════════════════════════════════════════════
IMPROVEMENT — Heal threshold lebih konservatif
═══════════════════════════════════════════════════════════════════
v3.0: critical_heal < 35, proactive_heal < 65
v4.0: critical_heal < 50 (kalau bisa heal, heal lebih awal!)
      proactive_heal < 75 (lebih agresif heal di area aman)
Reasoning: Guardian counter-attack bisa 22-24 damage per hit.
HP 35 → satu hit Guardian = mati. HP 50 = masih ada buffer.

═══════════════════════════════════════════════════════════════════
TETAP SAMA dari v3.0:
═══════════════════════════════════════════════════════════════════
- P0: Water Escape
- P1: Deathzone Escape  
- Guardian farming (120 sMoltz!)
- Movement scoring: Hills > Ruins > Plains > Forest, hindari Water
- Late game aggression
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
    "emergency_rations": 20,   # alias dari log game
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
_last_hp: int = 100          # [BUG FIX #1] Track HP perubahan untuk deteksi "under attack"
_idle_turns: int = 0         # [BUG FIX #1] Track berapa turn bot diam (can_act=False)

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
    global _known_agents, _map_knowledge, _kills_this_game, _last_hp, _idle_turns
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _kills_this_game = 0
    _last_hp = 100
    _idle_turns = 0
    log.info("Strategy brain reset for new game")

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine. Priority chain (v4.0 CRITICAL BUG FIX):

    P0.  WATER ESCAPE — [v3.0 BUG FIX] jika di water/pond, langsung keluar!
    P1.  DEATHZONE ESCAPE — instant, overrides all
    P1b. Pre-escape pending death zone
    P2.  Critical heal (HP < 50) — [v4.0 RAISED] HANYA jika can_act=True
    P2b. Under Attack + HP rendah → FLEE jika ada EP, heal jika ada item + can_act
    P2c. Guardian threat evasion (HP rendah, tidak ada heal)
    P3.  FREE ACTIONS: pickup, equip — BUKAN use_item (free actions saja)
    P3b. [v4.0 FIX] can_act check di sini — jika False, cek idle rest
    P4.  EP recovery (energy drink jika EP=0) — butuh can_act
    P4b. [v4.0 NEW] Map usage — SETELAH can_act check (main action!)
    P5.  Guardian farming — HP >= 50, EP >= 2, hanya jika advantaged
    P6.  Enemy agent combat — damage advantage atau target lemah
    P7.  Monster farming — EP >= 2
    P8.  Proactive heal (HP < 75, area aman) — [v4.0 RAISED threshold]
    P9.  Facility interaction
    P10. Strategic movement
    P11. Rest — EP <= 3 atau EP < max_ep
    """
    global _last_hp, _idle_turns

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
        _last_hp = 100
        _idle_turns = 0
        return None

    # ── [BUG FIX #1] Deteksi "under attack" ──────────────────────────
    hp_delta = _last_hp - hp   # positif = HP turun
    under_attack = hp_delta >= 6  # kena setidaknya 6 damage dari turn lalu
    if under_attack:
        log.warning("⚠️ UNDER ATTACK: HP delta=-%d (HP=%d→%d)", hp_delta, _last_hp, hp)
    _last_hp = hp

    # Track idle turns (saat can_act=False)
    if not can_act:
        _idle_turns += 1
        log.debug("⏳ Cooldown turn %d: can_act=False, HP=%d EP=%d", _idle_turns, hp, ep)
    else:
        _idle_turns = 0

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

    # Move EP cost
    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # Game phase
    early_game = alive_count > 70
    mid_game   = 20 < alive_count <= 70
    late_game  = alive_count <= 20

    # ── P0: WATER ESCAPE [v3.0 BUG FIX] ──────────────────────────────
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
    # [v4.0 BUG FIX] Threshold dinaikkan ke 50 (dari 35)
    # Reasoning: Guardian counter 22-24 dmg/hit. HP 35 = satu hit mati!
    # Harus heal saat HP < 50 untuk punya buffer yang cukup.
    # use_item BUTUH can_act=True (main action, bukan free action!)
    if hp < 50 and can_act:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            log.warning("💊 CRITICAL HEAL: HP=%d < 50, pakai %s", hp, heal.get("typeId"))
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp} berbahaya! (threshold=50)"}

    # ── P2b: [v4.0 NEW] Under Attack response ─────────────────────────
    # Jika kena serangan dan HP < 70: flee SEKARANG atau heal jika bisa
    if under_attack and hp < 70:
        # Cek apakah ada ancaman di sini
        threats_here = [a for a in visible_agents
                        if a.get("isAlive", True) and a.get("id") != my_id
                        and a.get("regionId") == region_id]
        monsters_here = [m for m in visible_monsters
                         if m.get("hp", 0) > 0 and m.get("regionId", region_id) == region_id]

        if (threats_here or monsters_here) and ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("🏃 UNDER ATTACK FLEE: HP=%d, menghindar ke %s", hp, safe)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"UNDER ATTACK FLEE: HP={hp} turun {hp_delta} dari serangan"}

        # Tidak bisa flee atau tidak ada ancaman visible — heal jika bisa
        if can_act:
            heal = _find_healing_item(inventory, critical=False)
            if heal:
                log.warning("💊 UNDER ATTACK HEAL: HP=%d, pakai %s", hp, heal.get("typeId"))
                return {"action": "use_item", "data": {"itemId": heal["id"]},
                        "reason": f"UNDER ATTACK HEAL: HP={hp} drop {hp_delta}"}

    # ── P2c: Guardian threat evasion ──────────────────────────────────
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    has_heals = bool(_find_healing_item(inventory, critical=False))
    flee_threshold = 40 if not has_heals else 25  # [v4.0] dinaikkan dari 35/20
    if guardians_here and hp < flee_threshold and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat + HP=%d (<%d), flee", hp, flee_threshold)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp} kritis"}

    # ── P3: FREE ACTIONS (pickup, equip) ──────────────────────────────
    # HANYA free actions di sini! use_item (map) BUKAN free action → ke P4b
    pickup_action = _check_pickup(visible_items, inventory, region_id)
    if pickup_action:
        return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    # ── [v4.0 BUG FIX #2] can_act check — dengan idle rest fallback ──
    if not can_act:
        # [v4.0 FIX] Jika sudah idle >= 2 turn DAN EP tidak penuh: REST
        # Ini membantu engine mereset cooldown state dan menghindari infinite idle
        if _idle_turns >= 2 and ep < max_ep and not region.get("isDeathZone"):
            log.info("⏳ Idle %d turns, can_act=False. REST untuk isi EP dan reset state.",
                     _idle_turns)
            return {"action": "rest", "data": {},
                    "reason": f"COOLDOWN REST: idle {_idle_turns} turns, EP={ep}/{max_ep}"}
        # Jika EP penuh dan idle lama: coba REST tetap (engine mungkin butuh signal)
        if _idle_turns >= 3 and not region.get("isDeathZone"):
            log.warning("🚨 STUCK Idle %d turns, can_act=False, EP=%d/%d HP=%d — force REST",
                        _idle_turns, ep, max_ep, hp)
            return {"action": "rest", "data": {},
                    "reason": f"FORCE REST: idle {_idle_turns} turns stuck, HP={hp}"}
        return None

    # ══ Dari sini: can_act = True ══════════════════════════════════════

    # ── P4: EP recovery ────────────────────────────────────────────────
    if ep == 0:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": "EP RECOVERY: EP=0, pakai energy drink (+5 EP)"}

    # ── P4b: [v4.0 FIX] Map usage — SETELAH can_act check ─────────────
    # Map adalah use_item = main action, butuh can_act=True
    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        return util_action

    # ── P5: Guardian farming ────────────────────────────────────────────
    # Guardian: HP=150, ATK=10, DEF=5. Reward: 120 sMoltz. SANGAT berharga!
    # Syarat: HP >= 55 (margin dari counter-hits), EP >= 2
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 55:  # [v4.0] dinaikkan dari 50
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
            turns_to_kill = target_hp / max(1, my_dmg)
            hp_cost_estimate = guardian_dmg * min(turns_to_kill, 8)
            if (my_dmg >= guardian_dmg * 0.7 or target_hp <= my_dmg * 3) \
               and hp - hp_cost_estimate > 25:  # [v4.0] safety margin 25 (dari 20)
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: HP={target_hp} "
                                  f"(120 sMoltz! dmg={my_dmg} vs {guardian_dmg})"}

    # ── P6: Agent combat ───────────────────────────────────────────────
    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != my_id]

    hp_min_fight = 55 if early_game else (45 if mid_game else 30)  # [v4.0] dinaikkan

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

            if (my_dmg > enemy_dmg or target_hp <= my_dmg * 2) and hp > enemy_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: dmg={my_dmg} vs {enemy_dmg}, target HP={target_hp}"}

    # ── P7: Monster farming ────────────────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 40:  # [v4.0] tambah HP check minimum
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER: {target.get('name','?')} HP={target.get('hp','?')}"}

    # ── P8: Proactive heal ────────────────────────────────────────────
    # [v4.0] Threshold DINAIKKAN ke 75 (dari 65) — lebih agresif heal
    # Reasoning: lebih baik heal saat aman daripada terlambat saat diserang
    area_safe = not enemies and not guardians_here
    if hp < 75 and area_safe:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"PROACTIVE HEAL: HP={hp} < 75, area aman"}

    # ── P8b: [v4.0 NEW] Heal bahkan saat ada musuh jika HP sangat rendah ─
    # Jika HP < 55 dan ada musuh tapi kita tidak bisa fight (EP rendah dll)
    if hp < 55:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"SAFETY HEAL: HP={hp} < 55, heal sebelum terlambat"}

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

    # ── P11: Rest ─────────────────────────────────────────────────────
    # [v4.0] Threshold dinaikkan ke EP <= 3 (dari <= 2)
    # Pastikan selalu ada cukup EP untuk gerak + attack di turn berikutnya
    if ep <= 3 and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, isi energi"}

    # [v4.0 NEW] Jika tidak ada yang dilakukan dan EP tidak penuh, rest saja
    if ep < max_ep and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: Tidak ada aksi optimal, EP={ep}/{max_ep}"}

    return None  # EP sudah penuh, tunggu turn berikutnya


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
        if t_hp <= my_dmg * 2:
            score += 15
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
                score = {"hills": 4, "plains": 3, "ruins": 3,
                         "forest": 2, "water": -5}.get(terrain, 1)
                if conn.get("interactables"):
                    score += 1
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
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
    else:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
    return heals[0]

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None

def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
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
    """
    [v4.0 BUG FIX] Ini dipanggil SETELAH can_act check.
    Map adalah use_item = main action, butuh can_act=True.
    Jangan pakai map saat HP kritis (prioritas heal lebih penting).
    """
    if hp < 50:
        return None  # Saat kritis, jangan waste turn untuk map
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
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
    v4.0 Movement scoring (sama dengan v3.0, sudah baik):
    - Water terrain mendapat penalti BESAR (-6)
    - Hills > Ruins > Plains > Forest > Water
    - Chase guardian/item regions
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

            score += {"hills": 6, "ruins": 4, "plains": 3,
                      "forest": 2, "water": -6}.get(terrain, 1)

            if rid in item_regions:
                score += 6
            if rid in guardian_regions:
                score += 8

            facs   = conn.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 3

            weather = conn.get("weather", "").lower()
            score += {"storm": -3, "fog": -2, "rain": 0, "clear": 1}.get(weather, 0)

            if late_game:
                if rid in enemy_regions:
                    score += 4
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 7

            if not early_game:
                if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                    score += 3

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]
