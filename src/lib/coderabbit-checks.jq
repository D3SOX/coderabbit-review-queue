# SPDX-License-Identifier: GPL-3.0-only

# A skip that another request for the same head cannot change. A new head
# can be queued again.
def final_skip_pattern:
  "Review skipped: (?:[0-9]+ files exceed the limit of [0-9]+|bot user not eligible for review)";

def final_skip: test("^" + final_skip_pattern);

# Convert CodeRabbit's GitHub Checks API entries to the status-context shape
# used by the queue. Leave other apps and legacy statuses untouched.
def coderabbit_check_context:
  if .name == "CodeRabbit" and (.app.slug // .checkSuite.app.slug) == "coderabbitai" then
    (.status | ascii_upcase) as $status
    | (.conclusion // "" | ascii_upcase) as $conclusion
    | ((.output.title // .title // "") + "\n" + (.output.summary // .summary // "")) as $detail
    # CodeRabbit reviews incrementally; a head with no new files is done.
    | ($status == "COMPLETED" and ($detail | test("No files to review"; "i"))) as $nothing_new
    | . + {
        context: "CodeRabbit",
        creator: {login: "coderabbitai"},
        created_at: (.started_at // .startedAt),
        createdAt: (.started_at // .startedAt),
        updatedAt: (.completed_at // .completedAt // .started_at // .startedAt),
        state: (if $status != "COMPLETED" then "PENDING"
                elif $conclusion == "SUCCESS" or $nothing_new then "SUCCESS" else "ERROR" end),
        description: (
          if $status != "COMPLETED" then "Review in progress"
          elif $detail | test("Review rate limited|rate limit exceeded|currently rate limited|(more reviews|next review) will be available|next included review available"; "i") then "Review rate limited"
          elif $detail | test(final_skip_pattern) then
            ($detail | capture("(?<reason>" + final_skip_pattern + ")").reason)
          elif $conclusion == "SUCCESS" or $nothing_new then "Review completed"
          elif $conclusion | IN("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE") then
            "Review failed: " + $conclusion
          else "Review skipped" end
        )
      }
  else . end;
