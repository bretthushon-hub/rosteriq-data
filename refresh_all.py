"""
RosterIQ data refresh — runs in GitHub Actions, which has real network access.

Why this exists: the Claude sandbox that builds the RosterIQ app cannot reach
api.sleeper.app or ESPN at all (proxy 403), and its fetch tool truncates large
responses. It CAN reach raw.githubusercontent.com over plain HTTPS. So this job
does all the heavy fetching here, writes compact JSON into data/, and the app
build step just curls those files.

Outputs (all under data/):
  meta.json                  tiny status file — read this first
  live.json                  Sleeper: state, byes, league settings, rosters,
                             users, transactions, traded picks, per-player
                             injury status, trending adds/drops
  projections.json           Sleeper projections for the upcoming week
  player_season_stats.json   season-to-date raw stats keyed by gsis_id, in the
                             exact schema portable_league_analysis.py expects
  player_id_map.json         sleeper_id <-> gsis_id <-> espn_id crosswalk
  dynastyprocess_values.json current dynasty market values
  leagues.json               ESPN "Ole Sports" league state

Every source is wrapped: one failing source degrades that file and is recorded
in meta.json, rather than taking the whole run down and leaving stale data with
no explanation.
"""

import json
import os
import sys
import time
import datetime as dt
import urllib.request
import urllib.error

OUT = "data"
os.makedirs(OUT, exist_ok=True)

SLEEPER = "https://api.sleeper.app"
LEAGUES = {
    "feeg": "1312297143557459968",
    "hobby": "1312099936690515968",
}
MY_ROSTER_ID = {"feeg": 3, "hobby": 9}
ESPN_LEAGUE_ID = 267341
ESPN_SEASON = 2026

FANTASY_POS = {"QB", "RB", "WR", "TE", "K"}
STATUS = {"generatedAt": None, "sources": {}, "warnings": []}


def note(source, ok, detail=""):
    STATUS["sources"][source] = {"ok": bool(ok), "detail": str(detail)[:400]}
    print(("  ok  " if ok else " FAIL ") + source + (" :: " + str(detail)[:200] if detail else ""))


def get_json(url, tries=3, timeout=60):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rosteriq-refresh/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - we want any failure to retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last}")


def get_text(url, tries=3, timeout=60):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rosteriq-refresh/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last}")


def write(name, obj):
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)
    print(f"  wrote {path} ({os.path.getsize(path):,} bytes)")


# ---------------------------------------------------------------- Sleeper core
def fetch_state():
    st = get_json(f"{SLEEPER}/v1/state/nfl")
    return {
        "week": st.get("week"),
        "displayWeek": st.get("display_week"),
        "season": st.get("season"),
        "seasonType": st.get("season_type"),
        "leg": st.get("leg"),
        "seasonStartDate": st.get("season_start_date"),
    }


def fetch_byes(season):
    """Bye week per team = the regular-season week a team does not appear in."""
    sched = get_json(f"{SLEEPER}/schedule/nfl/regular/{season}")
    weeks = {}
    teams = set()
    for g in sched:
        wk = g.get("week")
        for side in ("home", "away"):
            t = g.get(side)
            if t:
                teams.add(t)
                weeks.setdefault(t, set()).add(wk)
    max_week = max((g.get("week") or 0) for g in sched) if sched else 18
    byes = {}
    for t in sorted(teams):
        missing = [w for w in range(1, max_week + 1) if w not in weeks.get(t, set())]
        if len(missing) == 1:
            byes[t] = missing[0]
        elif missing:
            byes[t] = missing[0]
            STATUS["warnings"].append(f"{t} missing {len(missing)} weeks; took first as bye")
    return byes, max_week


def fetch_league(lid):
    league = get_json(f"{SLEEPER}/v1/league/{lid}")
    rosters = get_json(f"{SLEEPER}/v1/league/{lid}/rosters")
    users = get_json(f"{SLEEPER}/v1/league/{lid}/users")
    try:
        picks = get_json(f"{SLEEPER}/v1/league/{lid}/traded_picks")
    except Exception as e:  # noqa: BLE001
        picks = []
        STATUS["warnings"].append(f"traded_picks {lid}: {e}")

    by_user = {u["user_id"]: u for u in users}

    def owner_name(owner_id):
        u = by_user.get(owner_id) or {}
        meta = u.get("metadata") or {}
        return (meta.get("team_name") or u.get("display_name") or f"Roster {owner_id}").strip()

    settings = league.get("settings") or {}
    out = {
        "leagueId": lid,
        "name": league.get("name"),
        "status": league.get("status"),
        "season": league.get("season"),
        "seasonType": league.get("season_type"),
        "rosterPositions": league.get("roster_positions") or [],
        "scoringSettings": league.get("scoring_settings") or {},
        "settings": {
            "numTeams": settings.get("num_teams"),
            "playoffTeams": settings.get("playoff_teams"),
            "playoffWeekStart": settings.get("playoff_week_start"),
            "tradeDeadline": settings.get("trade_deadline"),
            "waiverType": settings.get("waiver_type"),
            "waiverBudget": settings.get("waiver_budget"),
            "waiverDayOfWeek": settings.get("waiver_day_of_week"),
            "startWeek": settings.get("start_week"),
            "leg": settings.get("leg"),
        },
        "rosters": [],
        "tradedPicks": picks,
    }
    for r in rosters:
        rs = r.get("settings") or {}
        out["rosters"].append({
            "rosterId": r.get("roster_id"),
            "ownerId": r.get("owner_id"),
            "ownerName": owner_name(r.get("owner_id")),
            "players": r.get("players") or [],
            "starters": r.get("starters") or [],
            "reserve": r.get("reserve") or [],
            "taxi": r.get("taxi") or [],
            "record": {
                "wins": rs.get("wins", 0),
                "losses": rs.get("losses", 0),
                "ties": rs.get("ties", 0),
                "fpts": float(rs.get("fpts", 0)) + float(rs.get("fpts_decimal", 0) or 0) / 100.0,
                "fptsAgainst": float(rs.get("fpts_against", 0) or 0)
                + float(rs.get("fpts_against_decimal", 0) or 0) / 100.0,
                "waiverPosition": rs.get("waiver_position"),
                "waiverBudgetUsed": rs.get("waiver_budget_used", 0),
                "totalMoves": rs.get("total_moves", 0),
            },
        })
    return out


def fetch_transactions(lid, upto_week, back=4):
    """Recent completed transactions. Sleeper keys these by week; walk back a few."""
    rows = []
    start = max(1, (upto_week or 1))
    for wk in range(start, max(0, start - back), -1):
        try:
            for t in get_json(f"{SLEEPER}/v1/league/{lid}/transactions/{wk}"):
                if t.get("status") != "complete":
                    continue
                rows.append({
                    "type": t.get("type"),
                    "created": t.get("created"),
                    "week": wk,
                    "rosterIds": t.get("roster_ids") or [],
                    "adds": t.get("adds") or {},
                    "drops": t.get("drops") or {},
                    "draftPicks": t.get("draft_picks") or [],
                    "waiverBid": (t.get("settings") or {}).get("waiver_bid"),
                })
        except Exception as e:  # noqa: BLE001
            STATUS["warnings"].append(f"transactions {lid} wk{wk}: {e}")
    rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return rows[:60]


def fetch_matchups(lid, week):
    try:
        rows = get_json(f"{SLEEPER}/v1/league/{lid}/matchups/{week}")
    except Exception as e:  # noqa: BLE001
        STATUS["warnings"].append(f"matchups {lid} wk{week}: {e}")
        return []
    return [{
        "rosterId": m.get("roster_id"),
        "matchupId": m.get("matchup_id"),
        "points": m.get("points"),
        "starters": m.get("starters") or [],
        "startersPoints": m.get("starters_points") or [],
        "playersPoints": m.get("players_points") or {},
    } for m in rows]


def fetch_players_filtered(keep_ids):
    """
    The full player map is ~5MB. Here in Actions that's fine to download; we filter
    down to what the app actually needs so the committed file stays small.
    """
    allp = get_json(f"{SLEEPER}/v1/players/nfl", timeout=180)
    out = {}
    for pid, p in allp.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position")
        keep = pid in keep_ids or (
            pos in FANTASY_POS
            and p.get("active")
            and (p.get("search_rank") is not None and p.get("search_rank") < 400)
        )
        if not keep:
            continue
        out[pid] = {
            "name": p.get("full_name") or (f"{p.get('first_name','')} {p.get('last_name','')}".strip() or None),
            "pos": pos,
            "team": p.get("team"),
            "status": p.get("status"),
            "injuryStatus": p.get("injury_status"),
            "bodyPart": p.get("injury_body_part"),
            "notes": p.get("injury_notes"),
            "injuryStart": p.get("injury_start_date"),
            "practice": p.get("practice_participation"),
            "practiceNote": p.get("practice_description"),
            "depthChartOrder": p.get("depth_chart_order"),
            "depthChartPosition": p.get("depth_chart_position"),
            "age": p.get("age"),
            "yearsExp": p.get("years_exp"),
            "searchRank": p.get("search_rank"),
            "number": p.get("number"),
            "gsisId": p.get("gsis_id"),
            "espnId": p.get("espn_id"),
            "yahooId": p.get("yahoo_id"),
            "newsUpdated": p.get("news_updated"),
        }
    # team defenses come through as the team abbreviation
    for pid in keep_ids:
        if pid not in out and isinstance(pid, str) and len(pid) <= 3 and pid.isalpha():
            out[pid] = {"name": f"{pid} Defense", "pos": "DEF", "team": pid, "status": None,
                        "injuryStatus": None, "bodyPart": None, "notes": None, "practice": None,
                        "depthChartOrder": None, "depthChartPosition": None, "age": None,
                        "yearsExp": None, "searchRank": None, "gsisId": None}
    return out


def fetch_trending(kind, limit=25):
    rows = get_json(f"{SLEEPER}/v1/players/nfl/trending/{kind}?lookback_hours=168&limit={limit}")
    return [{"playerId": r.get("player_id"), "count": r.get("count")} for r in rows]


def fetch_projections(season, week):
    rows = get_json(f"{SLEEPER}/v1/projections/nfl/regular/{season}/{week}", timeout=120)
    out = {}
    # Sleeper returns either a list of rows or a dict keyed by player id.
    items = rows.items() if isinstance(rows, dict) else [(r.get("player_id"), r) for r in rows]
    for pid, r in items:
        if not pid or not isinstance(r, dict):
            continue
        stats = r.get("stats") if isinstance(r.get("stats"), dict) else r
        keep = {k: stats.get(k) for k in ("pts_ppr", "pts_half_ppr", "pts_std", "gp") if stats.get(k) is not None}
        if keep:
            out[str(pid)] = keep
    return out


# ------------------------------------------------------- production (nflverse)
NFLVERSE_COLS = {
    "passing_yards": "passing_yards",
    "passing_tds": "passing_tds",
    "interceptions": "passing_interceptions",
    "passing_2pt_conversions": "passing_2pt_conversions",
    "rushing_yards": "rushing_yards",
    "rushing_tds": "rushing_tds",
    "rushing_2pt_conversions": "rushing_2pt_conversions",
    "receptions": "receptions",
    "receiving_yards": "receiving_yards",
    "receiving_tds": "receiving_tds",
    "receiving_2pt_conversions": "receiving_2pt_conversions",
    "sack_fumbles_lost": "sack_fumbles_lost",
    "rushing_fumbles_lost": "rushing_fumbles_lost",
    "receiving_fumbles_lost": "receiving_fumbles_lost",
}


def build_stats_and_crosswalk(preferred_season):
    """
    Season-to-date raw stats keyed by gsis_id, plus the sleeper<->gsis crosswalk.
    Falls back to the previous season when the preferred one has no games yet, and
    records which season was actually used so the app can say so honestly.
    """
    import pandas as pd  # noqa: F401  (import kept local so a missing dep is a clean failure)
    import nfl_data_py as nfl

    # nfl_data_py's import_weekly_data() is hardcoded to the "player_stats" release,
    # which nflverse deprecated 2025-08-01 in favor of "stats_player" and stopped
    # updating -- it 404s for any season after the split. Fetch the parquet directly
    # from the current release instead of going through that helper.
    used_season = None
    weekly = None
    for season in (preferred_season, preferred_season - 1):
        try:
            url = (
                "https://github.com/nflverse/nflverse-data/releases/download/"
                f"stats_player/stats_player_week_{season}.parquet"
            )
            df = pd.read_parquet(url, engine="auto")
        except Exception as e:  # noqa: BLE001
            STATUS["warnings"].append(f"weekly {season}: {e}")
            continue
        if df is not None and len(df) > 0:
            weekly = df
            used_season = season
            break
    if weekly is None:
        raise RuntimeError("no weekly data for either season")

    weekly = weekly[weekly["season_type"] == "REG"] if "season_type" in weekly.columns else weekly
    for col in NFLVERSE_COLS.values():
        if col not in weekly.columns:
            weekly[col] = 0.0
    weekly = weekly.fillna({c: 0.0 for c in NFLVERSE_COLS.values()})

    agg = {c: "sum" for c in NFLVERSE_COLS.values()}
    agg["week"] = "count"
    grouped = weekly.groupby(["player_id", "player_display_name", "position", "team"], dropna=False).agg(agg).reset_index()

    players = []
    for _, r in grouped.iterrows():
        if r["position"] not in FANTASY_POS:
            continue
        row = {
            "gsis_id": r["player_id"],
            "player_name": r["player_display_name"],
            "position": r["position"],
            "team": r["team"],
            "games_played": int(r["week"]),
        }
        for key, col in NFLVERSE_COLS.items():
            row[key] = float(r[col])
        players.append(row)

    # crosswalk from seasonal rosters (carries sleeper_id / espn_id)
    id_map = []
    for season in (used_season, preferred_season):
        try:
            ros = nfl.import_seasonal_rosters([season])
        except Exception as e:  # noqa: BLE001
            STATUS["warnings"].append(f"rosters {season}: {e}")
            continue
        for _, r in ros.iterrows():
            gsis = r.get("player_id") or r.get("gsis_id")
            if not gsis:
                continue
            id_map.append({
                "rosteriq_id": gsis,
                "full_name": r.get("player_name") or r.get("player_display_name"),
                "position": r.get("position"),
                "team": r.get("team"),
                "gsis_id": gsis,
                "espn_id": str(r["espn_id"]) if r.get("espn_id") == r.get("espn_id") and r.get("espn_id") else None,
                "sleeper_id": str(r["sleeper_id"]) if r.get("sleeper_id") == r.get("sleeper_id") and r.get("sleeper_id") else None,
                "yahoo_id": str(r["yahoo_id"]) if r.get("yahoo_id") == r.get("yahoo_id") and r.get("yahoo_id") else None,
                "fantasypros_id": None,
            })
        if id_map:
            break

    return {"season": used_season, "players": players}, id_map


def fetch_dynasty_values():
    """DynastyProcess publishes current market values as CSV on GitHub."""
    import csv
    import io

    txt = get_text("https://raw.githubusercontent.com/DynastyProcess/data/master/files/values-players.csv")

    def cell(row, key):
        return (row.get(key) or "").strip()

    rows = []
    for row in csv.DictReader(io.StringIO(txt)):
        try:
            rows.append({
                "player": cell(row, "player"),
                "position": cell(row, "pos"),
                "team": cell(row, "team"),
                "age": float(cell(row, "age") or 0) or None,
                "ecr_1qb": float(cell(row, "ecr_1qb") or 0) or None,
                "value_1qb": int(float(cell(row, "value_1qb") or 0)),
                "value_2qb": int(float(cell(row, "value_2qb") or 0)),
                "fp_id": cell(row, "fp_id") or None,
                "scrape_date": cell(row, "scrape_date") or None,
            })
        except Exception:  # noqa: BLE001 - skip malformed rows rather than dying
            continue
    return rows


def link_fantasypros_ids(id_map, dyn):
    """
    The dynasty file keys on FantasyPros ids; the crosswalk doesn't carry them.
    Join on normalised name + position, which is what the original export did.
    """
    def norm(s):
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    by_name = {}
    for d in dyn:
        by_name.setdefault((norm(d["player"]), d["position"]), d)
    hits = 0
    for r in id_map:
        d = by_name.get((norm(r.get("full_name")), r.get("position")))
        if d and d.get("fp_id"):
            r["fantasypros_id"] = d["fp_id"]
            hits += 1
    return hits


# ------------------------------------------------------------------- ESPN
def fetch_espn():
    from espn_api.football import League as ESPNLeague

    lg = ESPNLeague(
        league_id=ESPN_LEAGUE_ID,
        year=ESPN_SEASON,
        espn_s2=os.environ["ESPN_S2"],
        swid=os.environ["ESPN_SWID"],
    )
    out = {
        "league_name": lg.settings.name,
        "scoring_type": lg.settings.scoring_type,
        "current_week": getattr(lg, "current_week", None),
        "teams": [{
            "team_id": t.team_id,
            "team_name": t.team_name,
            "wins": t.wins,
            "losses": t.losses,
            "points_for": getattr(t, "points_for", None),
            "standing": getattr(t, "standing", None),
            "playoff_pct": getattr(t, "playoff_pct", None),
            "waiver_rank": getattr(t, "waiver_rank", None),
            "acquisitions": getattr(t, "acquisitions", None),
            "acquisition_budget_spent": getattr(t, "acquisition_budget_spent", None),
            "trades": getattr(t, "trades", None),
            "streak_type": getattr(t, "streak_type", None),
            "streak_length": getattr(t, "streak_length", None),
            "roster": [{
                "name": p.name,
                "espn_id": getattr(p, "playerId", None),
                "position": p.position,
                "pro_team": p.proTeam,
                "total_points": p.total_points,
                "avg_points": p.avg_points,
                "projected_avg": getattr(p, "projected_avg_points", None),
                "injury_status": p.injuryStatus,
                "injured": getattr(p, "injured", None),
            } for p in t.roster],
        } for t in lg.teams],
    }

    # current-week matchups -- who's playing whom, and the live score if the
    # week is underway. Best-effort: a missing scoreboard shouldn't sink the
    # roster pull above, which is the part everything else depends on.
    try:
        out["matchups"] = [{
            "home_team_id": m._home_team_id,
            "home_score": m.home_score,
            "away_team_id": m._away_team_id,
            "away_score": m.away_score,
            "is_playoff": m.is_playoff,
        } for m in lg.scoreboard()]
    except Exception as e:  # noqa: BLE001
        STATUS["warnings"].append(f"espn.matchups: {e}")
        out["matchups"] = []

    # recent adds/drops/trades/waiver claims league-wide, newest first.
    try:
        rows = []
        for act in lg.recent_activity(size=25):
            for team, action, player, bid in act.actions:
                rows.append({
                    "date": act.date,
                    "team_name": getattr(team, "team_name", None) if team else None,
                    "action": action,
                    "player_name": getattr(player, "name", None) if hasattr(player, "name") else str(player),
                    "bid_amount": bid,
                })
        out["transactions"] = rows
    except Exception as e:  # noqa: BLE001
        STATUS["warnings"].append(f"espn.transactions: {e}")
        out["transactions"] = []

    # free agent pool, top names by roster ownership so the list favors
    # widely-added/droppable players over deep-bench noise.
    try:
        fas = lg.free_agents(size=100)
        fas.sort(key=lambda p: getattr(p, "percent_owned", 0) or 0, reverse=True)
        out["free_agents"] = [{
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "percent_owned": getattr(p, "percent_owned", None),
            "projected_avg": getattr(p, "projected_avg_points", None),
            "injury_status": p.injuryStatus,
        } for p in fas[:60]]
    except Exception as e:  # noqa: BLE001
        STATUS["warnings"].append(f"espn.free_agents: {e}")
        out["free_agents"] = []

    return out


# ------------------------------------------------------------------- main
def main():
    STATUS["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()

    # 1. state
    try:
        state = fetch_state()
        note("sleeper.state", True, f"{state['seasonType']} wk{state['week']} {state['season']}")
    except Exception as e:  # noqa: BLE001
        note("sleeper.state", False, e)
        state = {"week": 1, "season": str(ESPN_SEASON), "seasonType": "regular", "displayWeek": 1}

    season = int(state.get("season") or ESPN_SEASON)
    week = int(state.get("displayWeek") or state.get("week") or 1)

    # 2. byes
    try:
        byes, max_week = fetch_byes(season)
        note("sleeper.schedule", True, f"{len(byes)} teams")
    except Exception as e:  # noqa: BLE001
        note("sleeper.schedule", False, e)
        byes, max_week = {}, 18

    # 3. leagues
    live_leagues, keep_ids = {}, set()
    for key, lid in LEAGUES.items():
        try:
            lg = fetch_league(lid)
            lg["transactions"] = fetch_transactions(lid, week)
            lg["matchups"] = fetch_matchups(lid, week) if state.get("seasonType") == "regular" else []
            lg["myRosterId"] = MY_ROSTER_ID[key]
            live_leagues[key] = lg
            for r in lg["rosters"]:
                keep_ids.update(str(p) for p in r["players"])
                keep_ids.update(str(p) for p in r["starters"] if p and p != "0")
            for t in lg["transactions"]:
                keep_ids.update(str(p) for p in (t["adds"] or {}))
                keep_ids.update(str(p) for p in (t["drops"] or {}))
            note(f"sleeper.league.{key}", True, f"{len(lg['rosters'])} rosters, {len(lg['transactions'])} tx")
        except Exception as e:  # noqa: BLE001
            note(f"sleeper.league.{key}", False, e)

    # 4. trending
    trending = {"add": [], "drop": []}
    for kind in ("add", "drop"):
        try:
            trending[kind] = fetch_trending(kind)
            keep_ids.update(str(r["playerId"]) for r in trending[kind])
            note(f"sleeper.trending.{kind}", True, f"{len(trending[kind])} rows")
        except Exception as e:  # noqa: BLE001
            note(f"sleeper.trending.{kind}", False, e)

    # 5. players + injuries
    try:
        players = fetch_players_filtered(keep_ids)
        injured = sum(1 for p in players.values() if p.get("injuryStatus") or (p.get("status") and p["status"] != "Active"))
        note("sleeper.players", True, f"{len(players)} kept, {injured} carrying a designation")
    except Exception as e:  # noqa: BLE001
        note("sleeper.players", False, e)
        players = {}

    write("live.json", {
        "generatedAt": STATUS["generatedAt"],
        "state": state,
        "byes": byes,
        "maxWeek": max_week,
        "leagues": live_leagues,
        "players": players,
        "trending": trending,
    })

    # 6. projections for the upcoming week
    try:
        proj_week = week if state.get("seasonType") == "regular" else 1
        proj = fetch_projections(season, proj_week)
        write("projections.json", {"generatedAt": STATUS["generatedAt"], "season": season,
                                   "week": proj_week, "projections": proj})
        note("sleeper.projections", True, f"season {season} wk{proj_week}, {len(proj)} players")
    except Exception as e:  # noqa: BLE001
        note("sleeper.projections", False, e)

    # 7. production + crosswalk + dynasty values
    try:
        stats, id_map = build_stats_and_crosswalk(season)
        note("nflverse.stats", True, f"season {stats['season']}, {len(stats['players'])} players, {len(id_map)} crosswalk rows")
    except Exception as e:  # noqa: BLE001
        note("nflverse.stats", False, e)
        stats, id_map = None, None

    try:
        dyn = fetch_dynasty_values()
        write("dynastyprocess_values.json", dyn)
        note("dynastyprocess.values", True, f"{len(dyn)} rows, scraped {dyn[0]['scrape_date'] if dyn else '?'}")
    except Exception as e:  # noqa: BLE001
        note("dynastyprocess.values", False, e)
        dyn = None

    if stats and id_map is not None:
        if dyn:
            hits = link_fantasypros_ids(id_map, dyn)
            note("crosswalk.fantasypros", True, f"{hits}/{len(id_map)} linked")
        write("player_season_stats.json", stats)
        write("player_id_map.json", id_map)
        STATUS["statsSeason"] = stats["season"]
        STATUS["statsIsCurrentSeason"] = stats["season"] == season

    # 8. ESPN
    try:
        espn = fetch_espn()
        write("leagues.json", espn)
        drafted = sum(len(t["roster"]) for t in espn["teams"])
        note("espn.league", True, f"{len(espn['teams'])} teams, {drafted} rostered players")
        STATUS["espnDrafted"] = drafted > 0
    except Exception as e:  # noqa: BLE001
        note("espn.league", False, e)

    write("meta.json", STATUS)

    failed = [k for k, v in STATUS["sources"].items() if not v["ok"]]
    if failed:
        print("\nFAILED SOURCES: " + ", ".join(failed), file=sys.stderr)
    # A partial refresh is still worth committing; only a total wipeout is a hard failure.
    if len(failed) == len(STATUS["sources"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
