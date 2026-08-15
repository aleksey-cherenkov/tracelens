#!/usr/bin/env bash
# Live check. Needs ANTHROPIC_API_KEY. ~$0.50 total.
#   ./scripts/live_check.sh
# Output lands in examples/ and is echoed to the terminal.

set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p examples

ask() {
  local slug="$1"; shift
  echo; echo "═══ $slug ═══"
  tracelens --plain ask "$@" 2>&1 | tee "examples/$slug.txt"
}

# telemetry quality — the question the provided symptoms never ask
ask q1-telemetry-correct  "are the email pipeline logs and telemetry correct? can I trust what I'm seeing"
ask q2-would-we-notice    "if something broke right now, would our monitoring catch it?"
ask q3-alerting           "we're about to build alerting on this pipeline. what should we not build it on?"

# incidents
ask s1-push        --symptom 1
ask s2-duplicate   --symptom 2
ask s3-slow        --symptom 3
ask s4-trace       --symptom 4
ask s5-log-noise   --symptom 5   # not an incident — a request for a log viewer.
                                 # the test is whether it says so or invents a threshold.

# should decline
ask x1-csv         "the CSV export job is failing"
ask x2-salesforce  "our Salesforce sync stopped last night"
ask x3-webhooks    "our webhooks stopped firing"

# is PLATFORM.md doing anything
ask x3-webhooks-nocontext "our webhooks stopped firing" --no-platform-context

echo
echo "═══ summary ═══"
grep -h "^source:" examples/*.txt | sort | uniq -c
echo "verdicts:"
grep -l "insufficient evidence" examples/*.txt 2>/dev/null | sed 's/^/  declined: /'
echo "dropped citations:"
grep -h "validator dropped" examples/*.txt || echo "  none"
