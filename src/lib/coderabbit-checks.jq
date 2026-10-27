# SPDX-License-Identifier: GPL-3.0-only

# Convert CodeRabbit's GitHub Checks API entries to the status-context shape
# used by the queue. Leave other apps and legacy statuses untouched.
def coderabbit_check_context:
  if .name == "CodeRabbit" and (.app.slug // .checkSuite.app.slug) == "coderabbitai" then
    (.status | ascii_upcase) as $status
    | (.conclusion // "" | ascii_upcase) as $conclusion
    | ((.output.title // .title // "") + "\n" + (.output.summary // .summary // "")) as $detail
    | . + {
        context: "CodeRabbit",
        creator: {login: "coderabbitai"},
        created_at: (.started_at // .startedAt),
        createdAt: (.started_at // .startedAt),
        updatedAt: (.completed_at // .completedAt // .started_at // .startedAt),
        state: (if $status != "COMPLETED" then "PENDING"
                elif $conclusion == "SUCCESS" then "SUCCESS" else "ERROR" end),
        description: (
          if $status != "COMPLETED" then "Review in progress"
          elif $detail | test("Review rate limited|rate limit exceeded|More reviews will be available"; "i") then "Review rate limited"
          elif $detail | test("Review skipped: [0-9]+ files exceed the limit of [0-9]+") then
            ($detail | capture("(?<reason>Review skipped: [0-9]+ files exceed the limit of [0-9]+)").reason)
          elif $conclusion == "SUCCESS" then "Review completed"
          elif $conclusion | IN("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE") then
            "Review failed: " + $conclusion
          else "Review skipped" end
        )
      }
  else . end;
