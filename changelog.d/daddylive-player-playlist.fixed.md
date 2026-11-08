DaddyLive finds streams again: it captured the page's decoy playlist request
instead of the player's own, and replayed it without the `Origin` header the
stream host requires. Its playlist token lasts about a minute, so playing
DaddyLive streams now renew the token in place instead of stalling.
