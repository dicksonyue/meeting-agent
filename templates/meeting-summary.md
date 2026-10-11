---
name: Meeting Summary
description: Meeting record - speakers, topics, verdicts, next actions, per-topic detail with quotes, suggestions
---
You are writing the official record of a meeting from its transcript. Use exactly the six numbered sections below, in order. Be specific and concise.

## 1. Meeting
- **Date & time:** use the recording date, start time and duration given in the meeting context.
- **Speakers:** the transcript has no speaker labels. If names are stated or used when people address each other, list them (with role if mentioned). Otherwise give the number of distinct voices you can infer from the conversation, e.g. "about 3 speakers (estimated)", and refer to them as Speaker A, B, C only where the dialogue makes the switch clear. Never invent names.

## 2. Discussion Summary
A short paragraph, then a numbered list of the topics covered (one line each). Keep this list consistent with section 5.

## 3. Verdict by Topic
A markdown table: **Topic** | **Verdict** | **Basis**. The verdict is one of: Decided, Agreed in principle, Deferred, Unresolved, or Information only, followed by the decision or outcome in a few words. The basis is the reason it landed there.

## 4. Next Actions
A markdown table: **Action** | **Owner** | **Due** | **Topic #**. Use "Not stated" for a missing owner or date. Include only actions that were actually committed to or clearly assigned.

## 5. Topics in Detail
Cover each topic from section 2 as `### Topic N: <name>`, numbered 1, 2, 3 and so on. At most 5 topics: merge minor items into the closest related topic. For each topic give:
- **Discussion points:** the main arguments, positions and facts raised, as bullets.
- **Original quote:** one or two short verbatim quotes from the transcript that capture the key moment, in quotation marks, each followed by its timestamp, like "..." (3:25). Quote exactly as written in the transcript. Do not paraphrase inside quotation marks.
- **Conclusion:** what was concluded, or "No conclusion reached".

## 6. Suggestions
Your own observations to help the team move forward: gaps, unresolved risks, items that need an owner or a deadline, and follow-up questions worth raising. Mark these clearly as suggestions rather than things said in the meeting. Write "None" if nothing stands out.
