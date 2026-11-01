# Keeps the newest attempt of each check, as GitHub does after a re-run.
# Attempts are ordered by check run id: GitHub can report a cancelled
# attempt's startedAt as later than the re-run that replaced it. Without an
# id, a check that has not started yet counts as the newest attempt.
def latest_checks:
  map(select(length > 0))
  | group_by([(.__typename // ""),
      (.workflowName // .checkSuite.workflowRun.workflow.name // ""),
      (.name // .context // "")])
  | map(max_by([
      (.databaseId
        // ((.detailsUrl // "") | [match("/job/([0-9]+)").captures[0].string | tonumber] | first)),
      (.startedAt // .createdAt // "~")]));
