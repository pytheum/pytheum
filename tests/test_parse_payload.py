"""parse_payload — parse a market payload to a dict ONCE (perf: avoid N re-parses per row)."""
from __future__ import annotations

import json

from pytheum.api.params import (
    book_from_payload,
    parse_payload,
    resolution_status_from_payload,
)


def test_parse_payload_str_dict_and_bad() -> None:
    assert parse_payload('{"a": 1}') == {"a": 1}            # JSON string -> dict
    assert parse_payload({"a": 1}) == {"a": 1}              # dict passes through (no re-parse)
    assert parse_payload("not json") is None               # malformed -> None
    assert parse_payload(None) is None                     # absent -> None
    assert parse_payload("[1, 2]") is None                 # valid JSON but not a dict -> None
    assert parse_payload(123) is None                      # non-str/dict -> None


def test_helpers_accept_parsed_dict_same_as_string() -> None:
    # The whole point: passing the pre-parsed dict yields identical results to passing the string,
    # so parse-once is behavior-preserving.
    payload = {"bestBid": 0.41, "bestAsk": 0.43, "umaResolutionStatus": "resolved"}
    s = json.dumps(payload)
    pl = parse_payload(s)
    assert book_from_payload(pl) == book_from_payload(s)
    assert resolution_status_from_payload(pl) == resolution_status_from_payload(s)
