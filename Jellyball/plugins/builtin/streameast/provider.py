"""Built-in provider plugin: StreamEastScraper."""

import os

from plugins.sdk import HtmlAggregatorProvider


class StreamEastScraper(HtmlAggregatorProvider):
    def __init__(self):
        super().__init__(
            "StreamEast",
            os.getenv("AGGREGATOR_4_URL", "https://thestreameast.top"),
            [],
            ["/stream/", "/match/", "/live/"],
            # The F1 section is on a sister domain linked from the home page.
            series_sections={"f1": ["https://streameast.cool/f1streams4"]},
        )
