# A comment body without the footer the queue adds to its commands, trimmed.
def without_queue_footer:
  sub("\\s*<sub>🤖 Posted automatically by \\[coderabbit-review-queue\\]\\([^)]*\\)</sub>\\s*$"; "")
  | gsub("^\\s+|\\s+$"; "");

# CodeRabbit review-body findings (nitpicks, outside-diff comments) on the PR
# head cannot be resolved like threads. They count as handled once someone
# answers with a PR comment after the review. Comments from bots and bare
# `@coderabbitai <command>` requests, including the queue's own, are not answers.
def unanswered_body_feedback:
  .headRefOid as $head
  | [.comments.nodes[]?
      | select(.author.__typename != "Bot"
          and ((.body // "") | without_queue_footer | test("^@coderabbitai\\s+[a-z ]+$"; "i") | not))
      | .createdAt] as $answers
  | .reviews.nodes[]?
  | select(.author.login == "coderabbitai"
      and .commit.oid == $head
      and .state != "DISMISSED"
      and ((.body // "") | test("(Nitpick comments|Outside diff range comments)\\s*\\([1-9][0-9]*\\)"; "i")))
  | (.submittedAt // "") as $at
  | select(all($answers[]; . <= $at));
