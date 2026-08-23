import json, os
from espn_api.football import League

league = League(
    league_id=267341,
    year=2026,
    espn_s2=os.environ["ESPN_S2"],
    swid=os.environ["ESPN_SWID"],
)

data = {
    "league_name": league.settings.name,
    "scoring_type": league.settings.scoring_type,
    "teams": [
        {
            "team_name": t.team_name,
            "wins": t.wins,
            "losses": t.losses,
            "roster": [
                {"name": p.name, "position": p.position, "pro_team": p.proTeam,
                 "total_points": p.total_points, "avg_points": p.avg_points,
                 "injury_status": p.injuryStatus}
                for p in t.roster
            ],
        }
        for t in league.teams
    ],
}

os.makedirs("data", exist_ok=True)
with open("data/leagues.json", "w") as f:
    json.dump(data, f, indent=2, default=str)
print("done")
