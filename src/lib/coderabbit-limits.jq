# SPDX-License-Identifier: GPL-3.0-only

# CodeRabbit has worded its rate-limit replies in several ways: "rate limited
# by coderabbit.ai", "More reviews will be available in N minutes", "Next review
# available in: **N minutes**", and under its Fair Usage Limits Policy "You're
# currently rate limited ... Your next review will be available in N minutes".
def rate_limit_reply:
  test("rate limited by coderabbit\\.ai|currently rate limited|(more reviews|next review) will be available in"; "i");

# {amount, unit} of the wait a rate-limit reply names, or null.
def rate_limit_timer:
  [capture("(?:refills? in|next review available in:?\\*{0,2}|(?:more reviews|next review) will be available in)\\s*\\*{0,2}(?<amount>[0-9]+)\\s+(?<unit>seconds?|minutes?|hours?)"; "i")]
  | first // null;
