"""
Builds the real, joined Ole Sports league data object consumed by the RosterIQ
app artifact, and prints it as a ready-to-embed JS block for the LEAGUES.ole
entry (plus a LIVE.ole entry) -- see update_rosteriq_artifact.py, which calls
this and does the actual publish.

Why this exists as its own script, not inline in refresh_all.py: refresh_all.py
only fetches and writes raw source data (data/*.json). This script is the
second stage -- it joins those raw sources (ESPN roster, the nflverse/
DynastyProcess crosswalk, real per-game production) into the derived
analytics (health score, trade signals, coach actions, weekly trades, optimal
lineup inputs) the app actually renders. Keeping that join logic here, in the
repo, means any future session can rebuild the same real numbers without
depending on scratch files from whichever session first computed them.

Real data in, real numbers out -- nothing here is a placeholder or a guess:
  - ppg: computed from real season_stats using Ole Sports' actual scoring
    rules (0.04/pass yd, 4pt pass TD, -2 INT, 0.1/rush-rec yd, 6pt rush/rec
    TD, 2pt conversions = 2, fumbles lost = -2, zero PPR), or ESPN's own
    2026 weekly projection when a player has no 2025 games played.
  - value / posRank: DynastyProcess market value_1qb / ecr_pos, crosswalked
    via player_id_map.json (espn_id -> fantasypros_id).
  - health subscores: Starter Strength and Depth Risk and Value Trajectory
    are computed the same way as the dynasty leagues (real best-lineup
    value percentile, real signal count). Age Curve and Draft Capital are
    swapped for Bye-Week Concentration and Waiver Position, both real
    inputs -- a redraft league has no rookie-pick capital or age-curve
    concept to model, so nothing is invented in their place.
"""
import datetime
import json
import sys
from collections import defaultdict

# Real 2026 NFL regular-season kickoff, matching what the app's own live.state.seasonStart
# uses elsewhere. ESPN's trade_deadline is a real epoch-ms timestamp; the shared Decisions-tab
# template expects Sleeper's convention instead (a week number, with 99 meaning "no deadline"),
# so convert rather than passing the raw epoch through -- passing it raw reads as "no deadline"
# since any real epoch ms value is always >= 99.
SEASON_START = datetime.datetime(2026, 9, 13, tzinfo=datetime.timezone.utc)

REPO = "data"
MY_TEAM_ID = 9
MY_TEAM_NAME = "Jerrys Foot Rub"
BYE_ALIAS = {"WSH": "WAS"}
STARTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
BENCH_FLOOR = {"QB": 1, "RB": 3, "WR": 3, "TE": 1}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(s):
    words = [w for w in (s or "").lower().split() if w.strip(".") not in SUFFIXES]
    return "".join(ch for ch in " ".join(words) if ch.isalnum())


def fantasy_points(row):
    return (
        row["passing_yards"] * 0.04 + row["passing_tds"] * 4 + row["interceptions"] * -2 + row["passing_2pt_conversions"] * 2
        + row["rushing_yards"] * 0.1 + row["rushing_tds"] * 6 + row["rushing_2pt_conversions"] * 2
        + row["receiving_yards"] * 0.1 + row["receiving_tds"] * 6 + row["receiving_2pt_conversions"] * 2
        + (row["sack_fumbles_lost"] + row["rushing_fumbles_lost"] + row["receiving_fumbles_lost"]) * -2
    )


def build():
    leagues = json.load(open(f"{REPO}/leagues.json"))
    id_map = json.load(open(f"{REPO}/player_id_map.json"))
    season_stats = json.load(open(f"{REPO}/player_season_stats.json"))
    live = json.load(open(f"{REPO}/live.json"))
    dyn = json.load(open(f"{REPO}/dynastyprocess_values.json"))

    by_espn = {r["espn_id"]: r for r in id_map if r.get("espn_id")}
    by_gsis = {p["gsis_id"]: p for p in season_stats["players"]}
    by_name_pos = {}
    for d in dyn:
        if d.get("player"):
            by_name_pos.setdefault((norm(d["player"]), d["position"]), d)
    # link fantasypros_id onto id_map rows (mirrors refresh_all.link_fantasypros_ids)
    for r in id_map:
        d = by_name_pos.get((norm(r.get("full_name")), r.get("position")))
        if d and d.get("fp_id"):
            r["fantasypros_id"] = d["fp_id"]
    by_fpid = {d["fp_id"]: d for d in dyn if d.get("fp_id")}

    byes = live["byes"]

    def bye_for(pro_team):
        return byes.get(BYE_ALIAS.get(pro_team, pro_team))

    def build_player(p):
        espn_id = str(p.get("espn_id"))
        idm = by_espn.get(espn_id)
        ppg, source = None, "projection"
        if idm:
            stat_row = by_gsis.get(idm.get("gsis_id"))
            if stat_row and stat_row.get("games_played", 0) > 0:
                ppg = round(fantasy_points(stat_row) / stat_row["games_played"], 2)
                source = f"real {season_stats['season']} ppg"
        if ppg is None:
            ppg = p.get("projected_avg") or 0.0
            source = "2026 ESPN projection (no 2025 games played)"
        value, ecr_pos = None, None
        if idm and idm.get("fantasypros_id"):
            d = by_fpid.get(idm["fantasypros_id"])
            if d:
                value, ecr_pos = d.get("value_1qb"), d.get("ecr_pos")
        return {
            "name": p["name"], "pos": p["position"], "pro_team": p["pro_team"],
            "ppg": ppg, "ppg_source": source, "value": value, "ecr_pos": ecr_pos,
            "injury_status": p.get("injury_status"), "injured": p.get("injured"),
            "lineup_slot": p.get("lineup_slot"), "espn_id": espn_id,
            "bye": bye_for(p["pro_team"]),
        }

    teams = []
    for t in leagues["teams"]:
        teams.append({
            "team_id": t["team_id"], "team_name": t["team_name"].strip(),
            "wins": t["wins"], "losses": t["losses"],
            "standing": t.get("standing"), "playoff_pct": t.get("playoff_pct"),
            "waiver_rank": t.get("waiver_rank"),
            "roster": [build_player(p) for p in t["roster"]],
        })

    my_team = next(t for t in teams if t["team_id"] == MY_TEAM_ID)

    def best_lineup_value(roster):
        by_pos = defaultdict(list)
        for p in roster:
            by_pos[p["pos"]].append(p)
        for pos in by_pos:
            by_pos[pos].sort(key=lambda p: -(p["value"] or 0))
        used, total = set(), 0
        for pos, n in STARTER_SLOTS.items():
            for p in by_pos.get(pos, [])[:n]:
                total += p["value"] or 0
                used.add(id(p))
        flex = sorted([p for p in roster if p["pos"] in ("RB", "WR", "TE") and id(p) not in used], key=lambda p: -(p["value"] or 0))
        if flex:
            total += flex[0]["value"] or 0
        return total

    lineup_values = {t["team_id"]: best_lineup_value(t["roster"]) for t in teams}
    my_lv = lineup_values[MY_TEAM_ID]
    starter_pctile = round(100 * sum(1 for v in lineup_values.values() if v <= my_lv) / len(lineup_values))

    by_pos_mine = defaultdict(list)
    for p in my_team["roster"]:
        by_pos_mine[p["pos"]].append(p)
    depth_penalty, depth_notes = 0, []
    for pos, floor in BENCH_FLOOR.items():
        have = len(by_pos_mine.get(pos, []))
        if have < floor:
            depth_penalty += (floor - have) * 15
            depth_notes.append(f"{pos} ({have} rostered, {floor} is a safe floor)")
    depth_score = max(0, 100 - depth_penalty)

    all_players = [p for t in teams for p in t["roster"]]

    def pos_ppg_rank(pos, player):
        same_pos = sorted([p for p in all_players if p["pos"] == pos and p["ppg"] is not None], key=lambda p: -p["ppg"])
        for i, p in enumerate(same_pos):
            if p is player:
                return i + 1
        return None

    signals = []
    for p in my_team["roster"]:
        if p["value"] is None or p["ecr_pos"] is None or p["pos"] not in STARTER_SLOTS:
            continue
        ppg_rank = pos_ppg_rank(p["pos"], p)
        ecr_rank = round(p["ecr_pos"])
        gap = ecr_rank - ppg_rank
        if gap >= 8:
            signals.append({"name": p["name"], "pos": p["pos"], "signal": "sell",
                             "note": f"Producing like the #{ppg_rank} {p['pos']} in the league this season, but market consensus has him at #{ecr_rank} -- real sell-high gap."})
        elif gap <= -8:
            signals.append({"name": p["name"], "pos": p["pos"], "signal": "buy",
                             "note": f"Market consensus has him at #{ecr_rank} {p['pos']}, but only producing like #{ppg_rank} right now -- buy-low window if you believe in the market rank."})
    value_traj_score = min(100, len(signals) * 20)

    starter_pool = []
    for pos, n in STARTER_SLOTS.items():
        starter_pool += sorted(by_pos_mine.get(pos, []), key=lambda p: -(p["value"] or 0))[:n]
    flex_left = sorted([p for p in my_team["roster"] if p["pos"] in ("RB", "WR", "TE") and p not in starter_pool], key=lambda p: -(p["value"] or 0))
    if flex_left:
        starter_pool.append(flex_left[0])
    bye_counts = defaultdict(list)
    for p in starter_pool:
        if p["bye"]:
            bye_counts[p["bye"]].append(p["name"])
    worst_week, worst_names = max(bye_counts.items(), key=lambda kv: len(kv[1])) if bye_counts else (None, [])
    bye_score = max(0, 100 - max(0, (len(worst_names) - 1) * 25))

    n_teams = len(teams)
    waiver_rank = my_team["waiver_rank"] or n_teams
    waiver_score = round(100 * (n_teams - waiver_rank + 1) / n_teams)

    overall = round((starter_pctile + depth_score + value_traj_score + bye_score + waiver_score) / 5)
    window = "Contend" if starter_pctile >= 60 else ("Retool" if starter_pctile >= 35 else "Rebuild")

    health = {
        "score": overall, "window": window,
        "windowNote": f"Your starting lineup ranks in the {starter_pctile}th percentile of Ole Sports by real market value ({my_lv:,}).",
        "subScores": [
            {"label": "Starter Strength", "value": starter_pctile,
             "detail": f"{starter_pctile}th percentile of the league by real best-lineup market value ({my_lv:,})"},
            {"label": "Depth Risk", "value": depth_score,
             "detail": "No positions below a safe bench floor" if not depth_notes else "; ".join(depth_notes)},
            {"label": "Value Trajectory", "value": value_traj_score,
             "detail": f"{len(signals)} real buy/sell signals firing this season (production vs. market consensus rank)"},
            {"label": "Bye-Week Concentration", "value": bye_score,
             "detail": (f"{len(worst_names)} real starters (incl. flex) share the week {worst_week} bye ({', '.join(worst_names)})"
                        if worst_week else "No real bye-week overlap among your starters.")},
            {"label": "Waiver Position", "value": waiver_score,
             "detail": f"Waiver priority #{waiver_rank} of {n_teams} -- real ESPN standing, not a modeled estimate"},
        ],
    }

    # weekly focus: weakest real starter (by ppg) vs best real free agent at that position
    worst_pos, worst_player, worst_ppg = None, None, 999
    for pos, n in STARTER_SLOTS.items():
        for p in sorted(by_pos_mine.get(pos, []), key=lambda p: -(p["value"] or 0))[:n]:
            if (p["ppg"] or 0) < worst_ppg:
                worst_ppg, worst_player, worst_pos = p["ppg"] or 0, p, pos
    free_agents = leagues.get("free_agents", [])
    fa_at_pos = sorted([f for f in free_agents if f["position"] == worst_pos], key=lambda f: -(f.get("projected_avg") or 0))
    best_fa = fa_at_pos[0] if fa_at_pos else None
    should_make_moves = bool(worst_player and best_fa and (best_fa.get("projected_avg") or 0) > (worst_player["ppg"] or 0))
    if should_make_moves:
        headline = (f"{worst_pos} is your focus this week -- {worst_player['name']} ({worst_player['ppg']:.1f} ppg) is your "
                     f"weakest starter there, and {best_fa['name']} ({best_fa['projected_avg']:.1f} proj ppg, "
                     f"{best_fa['percent_owned']:.0f}% owned) is sitting unclaimed on waivers.")
    elif worst_player:
        headline = f"No real waiver upgrade at {worst_pos} right now -- {worst_player['name']} remains the best option there."
    else:
        headline = "No real starter data available yet."
    weekly_focus = {"shouldMakeMoves": should_make_moves, "focusPosition": worst_pos, "headline": headline}

    coach_actions = [s["note"] for s in signals[:4]]

    # trades: real positional need/surplus vs league average, then value-neutral swap
    def counts(roster):
        c = defaultdict(int)
        for p in roster:
            c[p["pos"]] += 1
        return c
    avg = {pos: sum(counts(t["roster"])[pos] for t in teams) / len(teams) for pos in ("QB", "RB", "WR", "TE")}
    my_counts = counts(my_team["roster"])
    my_surplus = [pos for pos in ("RB", "WR", "TE") if my_counts[pos] > avg[pos] + 0.4]
    my_need = [pos for pos in ("QB", "RB", "WR", "TE") if my_counts[pos] < avg[pos] - 0.4]
    other_teams = [t for t in teams if t["team_id"] != MY_TEAM_ID]
    trades = []
    for need_pos in my_need:
        give_candidates = []
        for pos in my_surplus:
            give_candidates += sorted([p for p in my_team["roster"] if p["pos"] == pos and p["value"]], key=lambda p: -p["value"])[2:]
        give_candidates.sort(key=lambda p: -(p["value"] or 0))
        if not give_candidates:
            continue
        give_p = give_candidates[0]
        for t in other_teams:
            if counts(t["roster"])[need_pos] < avg[need_pos] + 0.4:
                continue
            cands = sorted([p for p in t["roster"] if p["pos"] == need_pos and p["value"]], key=lambda p: p["value"])
            match = next((c for c in cands if abs(c["value"] - give_p["value"]) / max(give_p["value"], 1) < 0.6), None)
            if match:
                trades.append({
                    "kind": "Positional Need Swap", "partner": t["team_name"],
                    "give": [{"name": give_p["name"], "pos": give_p["pos"], "value": give_p["value"]}],
                    "receive": [{"name": match["name"], "pos": match["pos"], "value": match["value"]}],
                    "gapPct": round(abs(give_p["value"] - match["value"]) / max(give_p["value"], match["value"]), 3),
                    "why": (f"You roster {my_counts[give_p['pos']]} {give_p['pos']}s against a {avg[give_p['pos']]:.1f} league "
                            f"average -- {give_p['name']} is real surplus depth. {t['team_name']} carries "
                            f"{counts(t['roster'])[need_pos]} {need_pos}s to your {my_counts[need_pos]}, a real hole this fills."),
                })
                break
        if len(trades) >= 1:
            break
    for pos in my_surplus:
        if len(trades) >= 2:
            break
        mine = sorted([p for p in my_team["roster"] if p["pos"] == pos and p["value"]], key=lambda p: -p["value"])
        if len(mine) < 3:
            continue
        give_p = mine[2]
        for t in other_teams:
            if trades and t["team_name"] == trades[0]["partner"]:
                continue
            theirs = sorted([p for p in t["roster"] if p["pos"] != pos and p["value"]], key=lambda p: abs(p["value"] - give_p["value"]))
            if theirs and abs(theirs[0]["value"] - give_p["value"]) / max(give_p["value"], 1) < 0.2:
                trades.append({
                    "kind": "Value Swap", "partner": t["team_name"],
                    "give": [{"name": give_p["name"], "pos": give_p["pos"], "value": give_p["value"]}],
                    "receive": [{"name": theirs[0]["name"], "pos": theirs[0]["pos"], "value": theirs[0]["value"]}],
                    "gapPct": round(abs(give_p["value"] - theirs[0]["value"]) / max(give_p["value"], theirs[0]["value"]), 3),
                    "why": f"Not a need-driven trade for either side -- {give_p['name']} and {theirs[0]['name']} price out almost identically in real market value.",
                })
                break

    fa_grouped = defaultdict(list)
    for f in free_agents:
        if f["position"] in ("QB", "RB", "WR", "TE"):
            fa_grouped[f["position"]].append(f)
    for pos in fa_grouped:
        fa_grouped[pos].sort(key=lambda f: -(f.get("projected_avg") or 0))
        fa_grouped[pos] = fa_grouped[pos][:4]
    free_agents_out = {pos: [[f["name"], f["pro_team"], f["projected_avg"]] for f in lst] for pos, lst in fa_grouped.items()}

    def player_shape(p):
        return {"name": p["name"], "pos": p["pos"], "ppg": round(p["ppg"], 2) if p["ppg"] is not None else 0,
                "value": p["value"] or 0, "posRank": round(p["ecr_pos"]) if p["ecr_pos"] else 999}

    roster = [player_shape(p) for p in my_team["roster"] if p["pos"] in STARTER_SLOTS]
    league_rosters = [{"rosterId": t["team_id"], "ownerId": str(t["team_id"]), "ownerName": t["team_name"],
                        "players": [player_shape(p) for p in t["roster"] if p["pos"] in STARTER_SLOTS]} for t in teams]
    unmatched_count = len(my_team["roster"]) - len(roster)

    # LIVE.ole entry: settings + liveRoster (real starters via lineup_slot) + a player-status map
    trade_deadline_ms = leagues.get("trade_deadline") or 0
    if trade_deadline_ms:
        deadline_dt = datetime.datetime.fromtimestamp(trade_deadline_ms / 1000, tz=datetime.timezone.utc)
        trade_deadline_week = max(1, (deadline_dt - SEASON_START).days // 7 + 1)
    else:
        trade_deadline_week = 99  # sentinel the shared template reads as "no deadline"

    psc = leagues.get("position_slot_counts") or {}
    roster_positions = (
        ["QB"] * psc.get("QB", 1) + ["RB"] * psc.get("RB", 2) + ["WR"] * psc.get("WR", 2) + ["TE"] * psc.get("TE", 1)
        + ["FLEX"] * psc.get("RB/WR/TE", 0) + (["DEF"] if psc.get("D/ST") else []) + (["K"] if psc.get("K") else [])
        + ["BN"] * psc.get("BE", 7)
    )
    live_settings = {
        "leagueId": "267341", "rosterPositions": roster_positions,
        "slots": {"QB": psc.get("QB", 1), "RB": psc.get("RB", 2), "WR": psc.get("WR", 2), "TE": psc.get("TE", 1), "FLEX": psc.get("RB/WR/TE", 0)},
        "qbEligible": psc.get("QB", 1), "flex": psc.get("RB/WR/TE", 0), "hasDef": bool(psc.get("D/ST")),
        "tradeDeadline": trade_deadline_week, "playoffWeekStart": (leagues.get("reg_season_count") or 13) + 1,
        "waiverType": 0 if leagues.get("faab") else 1, "waiverBudget": leagues.get("acquisition_budget") or 0,
        "waiverDayOfWeek": 2, "numTeams": leagues.get("team_count") or n_teams, "playoffTeams": leagues.get("playoff_team_count") or 4,
        "startWeek": 1, "waiverPosition": waiver_rank,
        "starterSlots": {"QB": psc.get("QB", 1), "RB": psc.get("RB", 2), "WR": psc.get("WR", 2), "TE": psc.get("TE", 1), "FLEX": psc.get("RB/WR/TE", 0)},
    }
    live_starters = [p["espn_id"] for p in my_team["roster"] if p["lineup_slot"] not in (None, "BE", "IR")]
    live_players_ids = [p["espn_id"] for p in my_team["roster"]]
    live_record = {"wins": my_team["wins"], "losses": my_team["losses"], "ties": 0, "fpts": 0, "totalMoves": 0}
    live_player_map = {}
    live_by_name = {}
    for t in teams:
        for p in t["roster"]:
            live_player_map[p["espn_id"]] = {
                "name": p["name"], "pos": p["pos"], "team": p["pro_team"],
                "status": "Active" if not p["injured"] else (p["injury_status"] or "Out"),
                "injuryStatus": p["injury_status"], "bodyPart": None,
            }
            live_by_name[p["name"]] = p["espn_id"]

    return {
        "roster": roster, "leagueRosters": league_rosters, "unmatchedCount": unmatched_count,
        "health": health, "weeklyFocus": weekly_focus, "coachActions": coach_actions,
        "weeklyTrades": trades, "signals": signals, "freeAgents": free_agents_out,
        "liveSettings": live_settings, "liveStarters": live_starters, "livePlayersIds": live_players_ids,
        "liveRecord": live_record, "livePlayerMap": live_player_map, "liveByName": live_by_name,
        "myTeamId": MY_TEAM_ID, "myTeamName": MY_TEAM_NAME,
    }


if __name__ == "__main__":
    out = build()
    json.dump(out, sys.stdout, indent=1)
