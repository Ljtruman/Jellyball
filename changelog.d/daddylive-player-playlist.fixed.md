DaddyLive plays again: it captured the page's decoy playlist request instead
of the player's own, and replayed it without the `Origin` header the stream
host requires. Its playlist token lasts about two minutes, so playing DaddyLive
streams now renew the token in place, and its segments, which arrive as PNG
images with the video packed inside, are unpacked to MPEG-TS.
