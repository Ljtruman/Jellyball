"""League registry: the one place that describes each sports league.

Catalog order, guide group titles, ESPN API and logo paths, game lengths,
season windows and college handling are read from here instead of being
repeated as literals across the catalog, the ESPN lookups and the dashboard.
test_leagues.py freezes every structure derived from it.

A league key is stored in the database (teams.category) and is part of channel
ids and catalog keys ("team:nfl:buf"), so an existing key must never change.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Optional, Tuple

# (start_month, start_day, end_month, end_day): preseason through the championship.
SeasonWindow = Tuple[int, int, int, int]

# sports_catalog.TeamSlug.category of the college team records, which football
# and men's basketball share (ESPN uses one team id for both).
COLLEGE_RECORD_CATEGORY = "college"
# ESPN logo folder of every college team (teamlogos/ncaa/500/<id>.png).
COLLEGE_LOGO_SPORT = "ncaa"


@dataclass(frozen=True)
class League:
    """key: category key (teams.category; catalog key "team:<key>:<id>").
    espn_sport/espn_league: ESPN site API path, sports/<espn_sport>/<espn_league>/...
    game_duration: assumed length of one game (guide block, stream window).
    group_title: M3U group-title and dashboard catalog section.
    logo_sport: ESPN logo folder of its catalog channels, teamlogos/<logo_sport>/500/<id>.png.
    season_window: in-season dates; None means always in season.
    is_college: teams come from the shared college records and the directory is refreshed from ESPN.
    sport_label: appended to college channel names, "Michigan Wolverines (Football)"."""

    key: str
    espn_sport: str
    espn_league: str
    game_duration: timedelta
    group_title: str = ""
    logo_sport: str = ""
    season_window: Optional[SeasonWindow] = None
    is_college: bool = False
    sport_label: str = ""


NCAAF = League(
    key="ncaaf", espn_sport="football", espn_league="college-football", game_duration=timedelta(hours=4),
    group_title="College Football", logo_sport=COLLEGE_LOGO_SPORT,
    season_window=(8, 1, 1, 22),  # fall camp/preseason through the CFP national championship
    is_college=True, sport_label="Football",
)
NCAAM = League(
    key="ncaam", espn_sport="basketball", espn_league="mens-college-basketball", game_duration=timedelta(hours=3),
    group_title="College Basketball", logo_sport=COLLEGE_LOGO_SPORT,
    season_window=(11, 1, 4, 10),  # season tip-off through the men's national championship
    is_college=True, sport_label="Men's Basketball",
)
NFL = League(
    key="nfl", espn_sport="football", espn_league="nfl", game_duration=timedelta(hours=4),
    group_title="NFL", logo_sport="nfl",
    season_window=(8, 1, 2, 15),  # Hall of Fame Game/preseason through the Super Bowl
)
MLB = League(
    key="mlb", espn_sport="baseball", espn_league="mlb", game_duration=timedelta(hours=4),
    group_title="MLB", logo_sport="mlb",
    season_window=(2, 15, 11, 10),  # spring training through the World Series
)
NHL = League(
    key="nhl", espn_sport="hockey", espn_league="nhl", game_duration=timedelta(hours=3),
    group_title="NHL", logo_sport="nhl",
    season_window=(9, 15, 6, 30),  # preseason through the Stanley Cup Final
)
NBA = League(
    key="nba", espn_sport="basketball", espn_league="nba", game_duration=timedelta(hours=3),
    group_title="NBA", logo_sport="nba",
    season_window=(10, 1, 6, 30),  # preseason through the NBA Finals
)

# The catalog leagues, in catalog order (dashboard sections, catalog entries).
LEAGUES: Tuple[League, ...] = (NCAAF, NCAAM, NFL, MLB, NHL, NBA)
LEAGUES_BY_KEY: Dict[str, League] = {league.key: league for league in LEAGUES}
CATALOG_CATEGORIES: Tuple[str, ...] = tuple(league.key for league in LEAGUES)
COLLEGE_CATEGORIES: Tuple[str, ...] = tuple(league.key for league in LEAGUES if league.is_college)

# Not a catalog league: only the hand-mapped Inter Miami entry of
# catalog._SPORT_SLUG_MAP uses it, for its ESPN schedule (usa.1 is MLS).
SOCCER = League(key="soccer", espn_sport="soccer", espn_league="usa.1", game_duration=timedelta(hours=2.5))

# Every key espn_schedule.fetch_espn_team_schedule accepts: the catalog
# leagues, SOCCER, and "ncaa", the sport catalog._SPORT_SLUG_MAP gives college
# teams, which looks up their college football schedule.
SCHEDULE_LEAGUES: Dict[str, League] = {**LEAGUES_BY_KEY, SOCCER.key: SOCCER, "ncaa": NCAAF}


# (ESPN session code, guide label, assumed length). ESPN lists every session of
# a race weekend as one "competition" whose type.abbreviation is the code.
SessionKind = Tuple[str, str, timedelta]


@dataclass(frozen=True)
class Series:
    """A motorsport series: one channel follows every session of every round,
    instead of one team's games.

    key: category key (teams.category; catalog key "series:<key>"). Must not
        collide with a League key and must never change once stored.
    espn_sport/espn_league: ESPN site API path, sports/<espn_sport>/<espn_league>/scoreboard.
    name: display name of the channel.
    short_name: prefix of guide titles ("F1 Qualifying"): one title per
        session kind all season, so a Jellyfin "Record Series" on it records
        every qualifying; the round goes in the episode title.
    group_title: M3U group-title and dashboard catalog section.
    session_kinds: known session codes, in weekend order.
    logo_url: channel logo (Jellyfin fetches it, so a public URL).
    default_session_duration: length of a session whose code is not listed."""

    key: str
    espn_sport: str
    espn_league: str
    name: str
    short_name: str
    group_title: str
    session_kinds: Tuple[SessionKind, ...]
    logo_url: str = ""
    default_session_duration: timedelta = timedelta(hours=1)

    def session_label(self, code: str) -> str:
        for kind_code, label, _ in self.session_kinds:
            if kind_code == code:
                return label
        return code

    def session_duration(self, code: str) -> timedelta:
        for kind_code, _, duration in self.session_kinds:
            if kind_code == code:
                return duration
        return self.default_session_duration


F1 = Series(
    key="f1", espn_sport="racing", espn_league="f1",
    name="Formula 1", short_name="F1", group_title="Formula 1",
    session_kinds=(
        ("FP1", "Practice 1", timedelta(hours=1)),
        ("FP2", "Practice 2", timedelta(hours=1)),
        ("FP3", "Practice 3", timedelta(hours=1)),
        ("SS", "Sprint Qualifying", timedelta(hours=1)),  # ESPN: "sprint shootout"
        ("SR", "Sprint", timedelta(hours=1)),
        ("Qual", "Qualifying", timedelta(hours=1)),
        ("Race", "Race", timedelta(hours=2)),
    ),
    logo_url="https://a.espncdn.com/i/teamlogos/leagues/500/f1.png",
)

SERIES: Tuple[Series, ...] = (F1,)
SERIES_BY_KEY: Dict[str, Series] = {series.key: series for series in SERIES}
