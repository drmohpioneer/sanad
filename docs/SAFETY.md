# Sanad - Safety

Sanad's safety model rests on one rule: anything that decides whether a message is an emergency, or whether a reply is safe to send, is code, not a model call. Models transcribe, extract, phrase replies and cast bounded yes-or-no votes; a vote can add a relay or an escalation and can never remove one. This document lays out the mechanism end to end. The phrase table is `app/core/sentinel.py`, the critical-lab table is `app/core/labs.py`, the blood-pressure table is `app/core/vitals.py`, the output check is `app/core/validator.py`, and the gate order is in `app/core/concierge.handle_patient_message`. Their regression suites run during the container build.

Three sentences carry the whole model: **every gate that can escalate is code; every safety vote is bounded and add-only; every one of those calls fails closed, so an error relays to the doctor instead of passing the message on.** Text the doctor himself wrote is the trusted path: it is delivered as his, prefixed with his name, and is not rewritten by a model.

**The same rule now covers four more agents, and it covers them the same way.** The Resolver (`core/resolver.py`), the Evidence Orchestrator (`core/evidence.py`), the Closure Auditor (`core/auditor.py`) and the Case Steward (`core/steward.py`) each hold either no tools or a fixed, code-guarded list, and each fails toward the code path that already existed rather than toward a model's own judgment. None of them can escalate less than the kernel below already decides to. The Resolver, the Auditor and the Steward run only for a doctor Sanad has explicitly enrolled in the Gate 3 cockpit (`doctor.workspace_facts_enabled`); a doctor who has not been enrolled gets the exact pre-S19 behavior, with no new deadline, no new agent turn and no new failure mode to reason about. The Evidence Orchestrator is the one exception to that cohort gate: it runs for every doctor, because its whole job is a better disposition for a photo the table would already have routed, and its failure mode is the table itself, not a doctor flag. "Fail-open honestly" is the phrase this document uses for all four, and it means something specific in each case: a model that is down never grants a permission code would have refused, it only ever falls back to the decision code already made before that agent existed, and the label written to the trail always says which one happened.

**The tables are frozen, not just exercised.** Until S11 both regression suites iterated the table they were guarding, so they asserted that whatever the table currently holds still fires. Deleting a phrase deleted its own test with it and the build stayed green (a Codex adversarial-review finding preserved in `app/tests/test_sentinel.py`, `app/tests/test_labs.py` and `app/tests/test_validator.py`). Both tables now also exist as a literal copy inside their test file, typed out rather than read from the module: `FROZEN_MUST_WAKE`, `FROZEN_CONCEPT_RULES`, `FROZEN_NEEDS_SUPPORT` and `FROZEN_NEVER_WAKE` in `app/tests/test_sentinel.py`, and `FROZEN_CRITICAL_LABS`, `FROZEN_UNIT_CONVERSIONS`, `FROZEN_ALIASES`, the four flag tables and the four abdominal-pain tables in `app/tests/test_labs.py`, and `FROZEN_CONTEXT_CLASSES` in `app/tests/test_validator.py`. Every table that decides something is in that list, which is the whole point of the sentence: a flag word is a threshold made of letters (remove "positive" from `HIGH_FLAGS` and a positive troponin quietly becomes "cannot judge"), an alias decides whether a row is found at all, and a negation decides whether a patient who said she has no pain is sent to an emergency room. Each frozen list is also run through the live code, not only diffed, so an entry that stops working fails even when the table itself is untouched. Removing a row, moving a threshold or changing a conversion factor fails the comparison, and the image does not build. A deliberate change is a change in three places at once: the module, the frozen literal, and this document. Every threshold in the table is also walked across its own boundary, just below it, at it and just above it, in the table's unit and in every alternate unit the conversion table knows, so a rule cannot quietly become an inequality it was not.

## The three-tier answer fence

Every patient message is answered in a fixed order, enforced by code, never left to a prompt to self-police:

1a. **The blood-pressure table**: a message that is nothing but a reading ("185/125") is graded by the three thresholds in `app/core/vitals.py` before any model is asked. A red reading exits through the emergency path. A non-red reading is filed under an open monitoring loop, when one exists, receives a fixed acknowledgement and exits without a model or doctor card.
1. **Sentinel**: can only escalate. If either net fires, no answer is generated at all; the patient gets a canned emergency block and the doctor gets a card. See below. If the model net could not be reached at all, the gate still fires, and the message is relayed to the doctor as a yellow "triage unavailable, please read" card while the patient gets the relay line; the audit line reads `model:error -> relayed`.
1b. **Treatment change**: a request to change, stop, start or substitute a treatment is caught by `validator.wants_treatment_change` (literal phrases plus token rules in three languages) and then by one model yes/no vote that can only add a relay. This gate runs **before the photo branch**, so a caption under a photo is gated exactly like typed text.
2. **Plan**: if the message is clear of the sentinel, and the question is about the patient's own care, the answer comes only from `plan_text`, the doctor's own written words, injected as the single source of truth. The Concierge holds no tool of any kind (see "No tool surface" below), so there is no mechanism by which it could read another patient's data even if asked to.
3. **General**: a question not covered by the plan gets educational information. The model is instructed not to write a single digit, dose, or measurement in a general-tier answer at all. Numbers are described in words instead, and every general answer closes with a line that the doctor's plan is what counts for this patient.

Anything that falls outside these tiers (a request to change treatment, a question the model is not confident about) does not get answered at all. It gets the relay line in the patient's own language, and the message is flagged for the doctor. A request to change treatment is caught earlier still: `validator.wants_treatment_change` matches it in code before the Concierge is ever called, so that class of request has no generation step at all, not just a post-hoc check. The model vote behind it can add a relay the code list missed, and it fails closed: if the call errors, the message relays.

**No tool surface.** ADK 2.8.0 does not allow an agent to combine `output_schema` (the structured tier/reply/relay-reason output the Concierge returns) with `tools` in the same definition. Rather than drop structured output, the Concierge carries no tools at all: the plan text and the patient's open loops are fetched by plain code and written directly into the instruction as text. The result is a stronger guarantee than "read-only tools" would have been: there is no callable surface on the patient path, writable or not, for a crafted message to target.

## The two sentinel nets

Both nets run on every message that reaches the Sentinel; a message that is nothing but a blood-pressure reading is graded by the table first and never reaches them. Net 1 runs before net 2 and either firing is sufficient. Other modality and code-shortcut exits are described in the request lifecycle.

**Net 1: code, and it is two nets in one.** The patient's text is normalized (Arabic diacritics stripped, common letter variants unified, lowercased, Franco-Arabic digits preserved, and Franco spellings folded through `sentinel.FRANCO_ALIASES`). It is matched against `MUST_WAKE` and then `CONCEPT_RULES`, which combine token concepts such as chest plus pain or face plus drooping. The fixtures span multiple clinic specialties and three writing styles; they are a broad floor, not proof of coverage for every specialty. A hit skips later generation.

The one thing a token rule cannot read is tense, so a message carrying a resolved marker ("embare7", "kan", "yesterday", "went away") stands the concept rules down and is left to the phrase table and the model vote. That is why "sadri kan wag3ny embare7 bas ra7" is still a never-wake sentence.

**Net 2: model, bounded vote.** If net 1 is clear, one Gemini call asks a single structured yes/no question: would this patient's physician want to be woken right now by this message? The never-wake examples in `sentinel.NEVER_WAKE` are supplied as negatives so the model has calibration, not just a blank judgment call; the same six sentences are the code net's negative fixtures in `app/tests/test_sentinel.py`, so one list guards both nets. The vote can only add an escalation. A "yes" escalates exactly like a net-1 hit, and it can never remove or override a net-1 hit. There is no path by which the model can suppress an emergency.

**Net 2 fails closed.** Until S5 an exception inside that call returned "did not fire", which meant a triage outage quietly promoted every unlisted emergency to an ordinary question. It now returns a fired verdict marked `model:error`, and the Concierge relays the message to the doctor unanswered. A model being down can cost the doctor a card he did not need; it cannot cost a patient an escalation.

Which net fired, if either, is stored on the event. This is the audit trail: for any escalation, the doctor card and the event log together show the patient, the exact quoted text, which net caught it, the matched concept (for net 1) or that it was a model judgment (for net 2), and the time. This is what a doctor or a judge can point to and ask "why did this fire," and get a real answer, not "the model decided."

## The output validator

After the Concierge generates a reply, and before anything is sent, code checks it:

- **No-reassurance blocklist.** Phrases like "don't worry," "it's normal," "متقلقش," "ده عادي," "كله تمام," "mafeesh moshkela" replace the entire reply with the relay line. A model that has been talked into being falsely comforting never reaches the patient.
- **A second, semantic reassurance gate.** One Gemini call answers "does this reply minimize, reassure, or tell the patient not to worry?" It is asked only about a reply the code rules already passed, so it can only add a relay, and it fails closed. **No model-generated reply reaches a patient without both gates.** Text the doctor wrote himself does not pass through them at all: it is his own words, sent as his.
- **Every number is typed, and a bare number is typed too.** A number in a reply is read together with the unit standing next to it (dose, count, time, frequency, level) and has to match a plan number of the **same** class. The plan's "7 days" does not license a reply's "7 mg". Word numbers in English and Arabic ("eighty", "ثمانين"), Unicode superscripts ("⁸⁰"), Arabic-Indic digits ("٨٠"), decimal commas and thousands separators are all folded to digits before the comparison. A number with no unit after it is not a number with no kind: the words in front of it say what it is about, and since S11 those words are read as well (`validator.context_class`). A measurement noun makes it a level, a drug name or "take" or "dose" makes it a dose, a month name makes it a date, and the class has to match on that side too. That is what stops "atorvastatin 40 mg" in the plan from licensing "your blood pressure is 40" in a reply, and "ضغطك 40" with it. The comparison is three branches and they are deliberately not one. A reply number with a **printed unit** has to match a plan number that also printed a unit of the same class, which is the pre-S11 rule unchanged: a plan's "Take 2 in the morning" is two tablets read from context and it does not license a reply's "2 mg". A reply number with a **context class and no unit** has to match a plan number of that class, printed or read from context. A reply number with **neither** has to match a plan number with neither, and that is all that is left of the old value-only comparison. The context is the number's own sentence, not a fixed count of characters, and a drug name from the lexicon standing in front of a number in that sentence makes it a dose, which is what this paragraph always claimed and now describes.
- **A hedge in front of an instruction is still an instruction.** A "generally"-framed number is allowed only in a general-tier sentence that carries no unit, no imperative ("take", "use", "خد", "استخدم") and no drug name. "Doctors generally aim for around 100" passes; "Generally, take 80 every morning" relays.
- **Drugs must trace, and an unknown name is treated as a drug.** A drug in the lexicon that is not in the plan relays, with a dose always and without one outside general education. Beyond the lexicon: any capitalized English word or Arabic word standing within 30 characters of a dose or of "take"/"خد" that the plan never mentions relays as an unknown entity. That is what catches "Take Eliquis 40 mg" and "خد زيثرون 40 مجم" whether or not anybody remembered to list the brand. The Egyptian shelf (Concor, Eliquis, Xarelto, Lipitor, Ator, Glucophage, Panadol, Augmentin, Zithron, Lasix, Capoten, Norvasc, Aldomet, Cardura and their Arabic spellings) is in the lexicon anyway.

The validator's verdict is stored on the event alongside the tier that was used, so the demo can show, for any given reply, exactly which check ran and what it decided.

## The three words that fired too easily

Three entries in the phrase table are single English words - "pounding", "emergency", "dying" - and each one fires on sentences that are not emergencies: "a pounding headache", "is this an emergency?", "my phone is dying". Since S4 each of those three needs a second word from its own concept before it counts (`sentinel.NEEDS_SUPPORT`): "pounding" wants a heart, a chest or a pulse near it; "emergency" wants an ambulance, a hospital, help or now; "dying" wants a first-person marker or a call for help.

The direction of that change is precise: it can stop only those three English words from firing alone on the code net; the model net still sees the same text and may add an escalation. `app/tests/test_sentinel.py` holds both the benign fixtures and positive counterparts such as "my heart is pounding and I feel dizzy." Laughter markers ("من الضحك", "mn el de7k", "laughing") now stand the code net down for هموت / hamoot / dying, the same way a resolved-tense marker does; the model net still sees the sentence and can still escalate it.

## Where a model output enters the safety path

Model output enters the patient path in these categories; the list is explicit rather than summarized as "exactly five":

1. **A voice note's transcript.** For voice notes the code sentinel runs on the transcript, which is a model output; the modality boundary is stated. `core/dispatch.py` transcribes and then calls `sentinel.check` on the transcript on that same lane, before the Concierge is called at all, and hands the verdict to the Concierge rather than letting it ask for one later.
2. **A photograph's contents.** Image content is model-read: the extractor's model classifies the picture and transcribes what is printed on it, and it is told, in the instruction and by the schema, that it may not judge, interpret, reassure or advise. Every comparison after that is `core/labs.py` or `core/vitals.py`. The caption on the photo is the patient's own text and goes through the sentinel and the change-request gate before the extractor is called. The analyte names and flags the model read back are themselves passed through the sentinel word list.
3. **The triage vote** (`sentinel.model_net`): one call, yes/no schema, temperature 0, can only add an escalation, fails closed.
4. **The change-request vote** (`validator.model_change_vote`): one call, yes/no schema, temperature 0, can only add a relay, fails closed.
5. **The reassurance vote** (`validator.model_reassurance_vote`): one call, yes/no schema, temperature 0, asked only about a reply the code rules already passed, can only add a relay, fails closed.

6. **The administrative vote** (`intents.model_vote`): may add only either of the two answer-only chores; state-changing intents require code patterns.

7. **The Coordinator choice** (`coordinator.run`): selects among seven code-guarded tools or stands down. It writes no patient sentence itself.

8. **The Concierge reply**: generated from the injected plan/open-loop context and then checked by the deterministic validator plus the reassurance vote before send.

9. **The Resolver's tool choice** (`resolver.run`, S19, enrolled doctors only): one guarded tool per turn, ask a patient one bounded question, search Google Places, move a visit inside the doctor's own window, or hand to the doctor. It never writes a sentence itself; the one question it may ask is a fixed template, and everything a patient reads about a place is a field Google Places returned, copied, never phrased.

10. **The Evidence Orchestrator's disposition** (`evidence.decide`, S24-E, every doctor): one bounded turn that names a photo's kind and which open loop it answers, chosen from a list of offers code built. It cannot write, attach or close anything itself; `core/photos.route` recomputes the route regardless of what it says, and `core/verify.check` still runs downstream and overrules it.

11. **The Closure Auditor's verdict** (`auditor.review_close`, S24-D, enrolled doctors only): one bounded turn, asked only after code has already allowed the close, that may refuse with a named gap and may never approve. Its only free-text input is flattened to one capped line before anything downstream can treat it as a sentence a patient or a doctor reads unfiltered.

12. **The Case Steward's verdict** (`steward.review`, S24-F, enrolled doctors only): one bounded turn over a proposal a tool guard has already accepted, that may approve, revise to a code-computed alternative, or hold the timing of a message that was already going to send. It never sees a patient's name, never writes to a patient, and never reaches a DANGER or an URGENT_SLA push.

One path carries no model rewrite: **text the doctor wrote himself is trusted.** His answer to a card is delivered as his, prefixed with his name and appended to the plan as a dated addendum.

## The Resolver: solving a barrier before it becomes the doctor's problem

Until S19 a barrier had two ends: the Coordinator paused the loop, or handed it to the doctor as a card. `core/resolver.py` is a fourth ADK agent, asked when `classify_barrier` names `availability`, `transport` or `cost`, before `escalate_barrier` is ever reached, and it runs one short guarded tool loop rather than a free conversation.

- **The model chooses; a table decides what it may choose from.** `resolver.ROUTES` is keyed on the barrier class, so `cost` can never be answered by rescheduling a visit and `forgot` can never spend a search; a call outside the table is refused with the reason, in code.
- **One question, and the cap is enforced in code, not by asking nicely.** A second question on the same barrier is refused however the model asks for it, and the one question it may ask is a fixed template, never a sentence the model wrote.
- **The model never sees a search result.** `find_places` proposes a search; the HTTP call happens afterwards, and a patient reads a name and an address exactly as Google Places returned them. There is no path from a model turn to the name of a place, so the Resolver cannot invent a laboratory and cannot quote a price: there is no price field in the payload to quote.
- **Widening a search is a code decision, not a second model turn.** An empty first search is widened once and tried again by a fixed table; the model is not asked to try again and never sees either result.
- **A refusal always names what was tried.** `hand_to_doctor` prints `tried` on the card, so the doctor sees what the Resolver already attempted rather than only that it failed.
- **The cost fork is a yes-or-no read in code, never a model's read of the patient's tone.** `resolver.declined_public_lab` matches a fixed decline list, normalized the same way the Sentinel normalizes text, and treats everything else, including silence made of punctuation, as a yes: the wrong way to be wrong here is a barrier the Resolver could have answered landing on the doctor's desk instead. A patient who did not mean yes says so on the next message and the doctor's own card is one reply away either way.
- **Places is data from outside, cleaned before Sanad repeats it.** `core/places.py` strips control characters, collapses whitespace, caps length and refuses any listing whose name or address reads like a dose. It fails soft, always: no API key, a network error, a quota, or a malformed payload comes back as an empty search with an error field, and the Resolver hands the barrier to the doctor with that sentence in `tried`, never a 500 on the patient's page.
- **Gated the same way the rest of this section is.** `resolver.check` reads the same `workspace_facts_enabled` cohort gate every guard in this document reads; a doctor never enrolled in the Gate 3 cockpit never reaches this file, and every barrier for him escalates exactly as it did before S19.

## The Closure Auditor: a second opinion, never a second authority

The fifth agent, and the only one whose whole job is to say no, and only to the system's own hand. `core/auditor.py` is asked one bounded question every time a loop closes, on the enrolled cohort only, and its authority is not the same on the two paths a close can take.

- **It may hold the system's own close; it may never hold the doctor's.** When the Coordinator itself proposes `close_verified_loop` during a wake-up, `core/policy.py` has already decided the close is allowed before the Auditor is asked (a code refusal, the verifier's own "does not satisfy," is answered in code, upstream), and here a refusal genuinely holds the close: the loop is left exactly as it was found, and the gap is written to the trail once. When the doctor himself taps Reviewed, the Auditor is still asked and may still name a gap, but it cannot hold that close: the tap closes the loop regardless, because the doctor's own authority is the one thing in Sanad no agent stands in front of, and what the Auditor adds is a line on the record, `closed with a gap on the record: <gap>`, filed alongside `closed_anyway: the doctor tapped Reviewed`, never a refusal.
- **There is no verdict it can return that turns a system-proposed close the code refused into a close that happens.** A refusal on that path is a named gap, written to the trail once, and the loop is left exactly as it was found.
- **It writes no state.** It returns a value; the caller acts on it. Every event, every close and every message stays owned by the file that already owned it.
- **It fails open on the model, never on the guard in front of it.** An Auditor that cannot be reached, times out, or answers nonsense is a second opinion that is missing, not a gate that is down: the close proceeds exactly as it did before this file existed, and one log line says the second opinion was not available.
- **Nothing it says is trusted as free text.** The gap is model-authored, so it is flattened to one capped line under a fixed prefix before any caller can put it in an event or a sentence a doctor reads. A refusal with nothing readable left in it still refuses, under the fixed wording `a required item is missing`.
- **It is told nothing that identifies the patient.** The contract it reads is rendered for "the patient" and "the doctor," which is enough to judge whether a record is finished and carries no name.

The Auditor exists because a loop that closes is a loop nobody looks at again: a close on an incomplete record is the mistake that never surfaces on its own, the board goes green, the doctor moves on, and the evening reading that was never sent is now a fact about a patient that no one will ever ask about again. Naming that gap once, in a fixed line, is the whole of what this agent is trusted to do.

## The Case Steward: is this the right move, never is this allowed

The seventh agent, and the only one that never touches the record. `core/steward.py` reads a move another agent has already chosen and answers with one of three words, on the enrolled cohort only, after `core/policy.check` has already decided the move is legal: the question this file answers is never "may this happen," code already answered that, but "should it, right now."

- **`approve`** changes nothing. The proposal executes byte for byte as it would have executed if this file did not exist.
- **`revise`** swaps in one named alternative, chosen from the same list `core/policy.check` would already allow on these exact facts, computed in code before the model is asked. The alternative is put through the identical guard the original proposal went through; a refusal there is not an argument, the original stands, because a steward that cannot produce a legal move has not produced a move at all.
- **`hold_for_digest`** is timing only, and the limit matters more than the feature. The action is carried out exactly as proposed; what changes is a release moment, the earlier of the next digest and a fixed ceiling (two hours on a case already handed to the doctor or already waiting on his review, six hours otherwise). It can never delete a card, drop a queue row or change a count. `core/adapters.route_for` is the only thing in Sanad that decides what reaches a phone, and the Steward does not touch, stamp or reach around that decision: it can only make an already-parking-eligible message park a little sooner, and it can never touch a DANGER or an URGENT_SLA, because those are answered in code before the Steward's mark is ever read.

**Danger bypasses it in code, not by convention.** `core/sentinel.py` and `core/escalate.py` do not import `core/steward.py` and never construct a turn on it; a critical reaches the phone with no Steward frame anywhere on the stack. A proposal that is not one of the Coordinator's seven tools is not judged at all, it is approved unasked. It writes no state of its own, no store, no event, no send, no task queue: every verdict is a returned value, and the caller writes the trail line. And it fails open exactly like the other three: a model that is down, slow, or answering nonsense means approve, today's un-Stewarded behavior verbatim, logged once.

The product sentence this agent exists to make true: **the doctor hears from one mind.** Danger, which never comes through here at all. A finished outcome, which the phone contract parks to the morning. And answers to what he actually asked. Problems are not pushed at him; the agents settle them between themselves, and he can pull the report of what was handled.

## The phone contract: what is allowed to ring an enrolled doctor's phone

S24-G. `core/adapters.route_for` is the one place in Sanad that decides whether a message reaches a phone, and it reads the message's own `notification_class`, never the Steward, never a model.

- **DANGER and URGENT_SLA ring the phone now.** Unchanged, including the failure behavior: a Telegram send that comes back `ok:false` still raises, and `core/escalate.told_or_fail_closed` still reads that exception.
- **REVIEW_READY and DEADLINE_OUTCOME park.** The card is written to the cockpit exactly as before and marked parked; nothing about what the web console shows changes, and a parked message is never mistaken for a delivery.
- **SILENT_WORK and SOLICITED_RESPONSE stay quiet.** No ring, no park: the cockpit carries it, and there is nothing the morning owes him that he did not already ask for.
- **Anything unclassified rings the phone and logs a warning.** Fail open to noisy, never to silent.

**The digest is doctor-pulled. There is no automatic push at any hour, 09:00 Cairo or otherwise.** `/digest` and `GET /c/{token}/summary` are the same counting code, run only when the doctor opens the console or types the command; a parked card is parked to that digest, not to a clock. A future Cloud Tasks push that fires the digest on a schedule is a month-two item, not something this revision does.

Only an enrolled doctor (`workspace_facts_enabled`) is covered by any of this. A doctor who is not enrolled gets the fan-out he always had, and a patient-bound message is never touched by the phone contract at all.

## Gendered wording, because getting it wrong is a safety-adjacent failure

Arabic conjugates the second person. A reminder that reads "فاكر" to a man reads wrong to a woman. The implementation therefore treats grammatical gender as a code decision rather than asking a model to improvise it.

The fix is code, not a prompt. `app/core/gender.py` turns the record's `sex` field into one of three answers - masculine, feminine, or unknown - and every template that addresses or mentions a patient picks its wording from that: the three nudge rungs, the monitoring reminder, the emergency block, the critical-lab block, the doctor's cards ("his"/"her"/"their"), the Telegram lines. Unknown is a real third answer with its own wording, not a fallback to masculine: a doctor who did not say the sex gets text that commits to neither, rather than a guess with a good chance of reading wrong. `app/tests/test_gender.py` asserts that a woman never receives a masculine form, that a man keeps his, and that the unknown form contains no gendered verb at all.

## The one-message correction rule

When a message is relayed, the doctor sees a yellow card with the patient's question and, if the model proposed one, its draft reply. The doctor answers from the console; that answer is delivered to the patient prefixed with the doctor's name, and it is appended to the plan as a dated addendum. The next question from that patient sees the updated plan. Sanad never guesses twice at the same gap: it surfaces the gap once and then treats the doctor's answer as part of the record.

## The critical-lab table

The table is static project configuration, not runtime model output. It and its `judge()` function are plain data and a pure function in `app/core/labs.py`, covered by `app/tests/test_labs.py`. The synthetic slip fixtures exercise extraction inputs, while deterministic unit tests prove the comparison boundaries. A potassium of 6.7 is a red card because `6.7 > 6.0` in the table below, not because a model judged severity.

| Analyte | Escalate when |
|---|---|
| K⁺ | <2.5 or >6.0 mmol/L |
| Na⁺ | <120 or >160 mmol/L |
| Glucose | <50 or >500 mg/dL |
| Creatinine | >4 mg/dL, or ≥2× patient baseline |
| Hb | <7 g/dL |
| Troponin | above the lab's own cutoff (any "positive"/"high" flag or value > printed reference) |
| INR | >5 |
| Platelets | <50 ×10³/µL |
| D-dimer | any "positive"/above the lab's printed cutoff (cutoff varies per lab; use the slip's reference, never a fixed number) |
| LDL | never escalates → 🟡 "above target" card only |
| Calcium | <6.5 or >13 mg/dL |
| WBC | <1.0 or >50 ×10³/µL |
| Bilirubin (neonate) | any value flagged high on the slip |
| pH / bicarbonate | pH <7.2 or >7.6; HCO3 <10 |
| Positive culture flagged by the lab (blood/CSF) | any |
| Pregnancy test | positive alone → 🟡🚨 urgent review (doctor card, no emergency line to the patient); positive **and** abdominal pain reported in the same conversation → escalate (ectopic rule; needs both) |

**The pregnancy row is the one rule that needs two facts, and the code now reads both.** This document has always written it as "positive with abdominal pain reported in the same conversation (ectopic rule; needs both)". Until S11 the code read the first half only: any slip a lab flagged positive took the full critical path, so a woman whose only news was that she is pregnant received the emergency block telling her to go to an emergency room. `labs.LabRule` now carries a `two_factor` flag, and the pregnancy row is the only row that has it. A positive test on its own is **urgent review**: the doctor gets the amber-red card that sits with the red ones, and the patient hears the ordinary "sent to your doctor". The card says which of two different things happened, because they are different facts about his patient: "abdominal pain: not checked (no patient messages were searched)" when nothing was handed to the rule, and "abdominal pain: none found in the last 48 hours" when the messages were read and carried none. Reporting a search that was never made is the error Codex item 3 was about, and it does not get to come back on a different card. It becomes critical, with the emergency block, when the patient's own words in the same conversation report abdominal pain: the caption under the slip, or her messages from the last 48 hours. Those words are searched in code by `labs.abdominal_pain`, on the same normalisation the Sentinel uses (`sentinel.normalize`: diacritics stripped, Arabic letter variants unified, Franco spellings folded), so "بطني بتوجعني", "batni bt wga3ny" and "lower abdominal cramps" are one concept. The Sentinel's own phrase table is untouched by this: this concept escalates a lab row, it does not wake the doctor on its own. Three things have to line up before the second fact counts. A stem matches at the start of a word and never inside it, so "headache" is not an abdominal ache and "stable" is not a stab. The abdomen word and the pain word have to stand within six tokens of each other, so a belly in one sentence and a knee pain in the next are two facts about two things. And a negation within three tokens in front of either of them stands the match down, so "no abdominal pain", "مفيش وجع في بطني" and "my abdomen is fine, no pain" do not complete the rule: sending the emergency block to a woman who has just said she has no pain is the exact harm this rule exists to prevent, arriving by the other door. Tense is not read here, unlike the Sentinel's concept rules: abdominal pain in the last 48 hours next to a positive test is the ectopic question whether or not it has eased. Passing no context at all is safe by construction, because the missing half means urgent review and never a quiet pass.

For cutoff-relative analytes (troponin, D-dimer), the extractor captures the slip's own printed reference range. If it is missing, the analyte is marked "cannot judge, pending doctor review": the system never invents a cutoff number to fill the gap. A critical hit takes the identical escalation path as a sentinel-net hit: emergency guidance to the patient, a red card to the doctor, decided entirely in code from the extracted value, no model in the decision.

**The table is written in one unit per analyte, and the value is converted into it before anything is compared.** A haemoglobin printed as 60 g/L is 6.0 g/dL and a transfusion; read as a bare 60 it looked normal, which is exactly what the red team proved. `labs.UNIT_CONVERSIONS` holds the common alternates (Hb g/L, glucose mmol/L, creatinine µmol/L, calcium mmol/L, mEq/L for the electrolytes, 10⁹/L for the counts). Parsing handles scientific notation ("6.0E1" is sixty, not six), thousands separators, Arabic-Indic digits and "<"/">" prefixes.

**A third outcome: urgent review.** Not normal, not critical, and not the ordinary "no opinion". A unit nobody can convert, a value the parser could not read on a row the lab itself flagged (H, HH, L, LL, critical, panic), or an analyte with no row that the lab flagged HH/LL/critical, all produce an amber-red card titled "URGENT REVIEW" that sits with the red cards on the console rather than in the yellow pile, and it says on its face which row code stood down on and why. The patient hears the ordinary "sent to your doctor": this is the doctor being asked to look tonight, not the patient being sent to an emergency room.

## Evidence: three checks, and a check that could not be done is not a pass

A photograph of a result is not evidence yet. It is evidence when it is this patient's result, taken after the doctor ordered it, and carrying everything he asked for. `app/core/verify.py` runs those three checks in code, with no model call in any of them: identity (the printed name against the record, fuzzily, in both scripts, `core/names.py`), date (the collection date on or after the order date), and completeness (every analyte the contract named is on the slip).

**And no tool call talks the verifier out of it.** The Coordinator is woken on every unsatisfied verdict, and its instruction says "a complete result arrived: mark_evidence_received". The guard behind that tool used to ask only whether any values were on the loop, which they are, so a model vote could have moved the loop to `pending_review`, the state the verifier had just refused, and the end state would have been the pre-S11 one reached by a model instead of by code. `core/policy.py` now reads the verifier's own verdict: `mark_evidence_received` and `close_verified_loop` are refused with "the verifier did not accept this slip: escalate_barrier and let the doctor decide" when the loop's recorded verdict says it was not satisfied. A loop the verifier never saw (a typed reading, a monitoring loop) is unchanged, because there is nothing there for the guard to contradict. This is the rule the top of this document leads with, applied to itself: the code has the last word, including over the agent that is supposed to be helping.

Two verdicts come out of it and they are not the same verdict. **Attaches** is whether values may be written onto the patient's loop; an identity mismatch says no. **Satisfies** is whether the slip closes the evidence side of the obligation, and all three checks must pass. A slip with no printed name or no readable date attaches for doctor review but cannot satisfy the obligation. The card names the checks that could not be completed and the loop stays open.

An identity mismatch has its own review card. It shows the extracted values so a dangerous value cannot disappear, and a critical or urgent value still makes the card red, but it suppresses ordered-test completeness and offers no Attach action. Exact image bytes are also claimed transactionally per patient and Cairo day before extraction: a same-day replay receives a fixed acknowledgement and creates no second result, card or model call.

## The blood-pressure table

Blood pressure arrives two ways: the patient types "185/125" into the chat, or photographs the machine and the model transcribes the two numbers off the screen. Both go through the same three lines of `app/core/vitals.py`, which is why they cannot be graded differently.

| Reading | Level | What happens |
|---|---|---|
| systolic 180 or above | hypertensive crisis | red card to the doctor **and** the emergency block to the patient |
| diastolic 120 or above | hypertensive crisis | red card to the doctor **and** the emergency block to the patient |
| systolic below 90 | low blood pressure | red card to the doctor **and** the emergency block to the patient |
| anything else | not red by this table | it joins an open monitoring chart and receives a fixed acknowledgement; no model or doctor card |

Three things about this are deliberate.

The two crisis rows are an OR, not an AND: either number reaching its own cutoff is enough on its own, so 150/125 escalates even though the systolic is unremarkable.

Both red rows reach the patient. The first build of this table sent the emergency block for a crisis only and left the low row as a card to the doctor, and the question went back to the doctor: is 85 systolic measured at home something a patient should sit with? It is not. Both red rows now send the same emergency block, and `app/tests/test_vitals.py` asserts all three rows, so a reading that is red for the doctor and quiet for the patient cannot come back by accident.

The reading is filed to the chart whatever the verdict, including a crisis. A chart that silently drops the worst reading in the series is worse than no chart.

A non-red bare reading such as `120/80` terminates at this table. With an open monitoring loop it is filed and acknowledged by a fixed template; without one, the patient is told truthfully that there is no open monitoring request. It does not call a model or create a doctor card.

The emergency text a red reading sends is the same block the Sentinel sends, in the patient's language and grammatical gender. No new wording was written for it, so there is one Arabic emergency instruction in the system rather than two that could drift apart.

What this table is not: a hypertension grading scale, an opinion about trend, or anything that reads symptoms. It is a floor under the readings that must not be missed while the doctor is asleep.

## AI self-disclosure and disclaimer

Sanad introduces itself as an AI assistant, not a doctor, during onboarding.

**English onboarding template:**
> Hi [Patient] 👋 I'm Sanad, [Doctor]'s AI assistant. I'm not a doctor: I follow your plan and pass anything you tell me to your doctor.

**Egyptian Arabic onboarding template, masculine form:**
> أهلاً [المريض] 👋 أنا سند، المساعد الذكي بتاع [الدكتور]. أنا مش دكتور: بتابع معاك الخطة وأوصّل أي حاجة لدكتورك.

The exact variants are in `app/core/templates.py`; the unknown-chat introduction is in `app/core/tg_router.py`, and emergency wording is in `app/core/templates.py`. Before the patient has written, language selection defaults to English; later proactive messages follow the latest patient message.

## The doctor is told before the patient is told he was told

"Your doctor has just been alerted" is the strongest sentence Sanad says to a patient. It ends the emergency block, the critical-lab block and the crisis blood-pressure block, and it is the sentence that makes a frightened patient stop typing and go. It used to be sent FIRST on every one of those paths: the reassurance went out, and only then were the escalation event, the relay and the doctor's card written. A Firestore timeout, a cold instance or a crash in between left the worst state this system can be in, a patient who has been told to stop waiting and a doctor who was never told anything.

The order is inverted on all five paths (the emergency, the triage outage, the ordinary relay, the critical lab, the crisis blood pressure) and it is inverted in one function, `core/escalate.told_or_fail_closed`, so that no branch can be given the promise without the fallback that goes with it. It runs the persistence and answers whether it landed. A caller that gets True says the sentence it always said. A caller that gets False says a fail-closed line instead, and an error event is written so the failure is on the board and not only in a log.

Two fail-closed lines, because the two situations are not the same:

- **Emergency.** The finding still stands, so the patient is still sent to the nearest emergency room or told to call 123. That instruction never depended on the doctor hearing anything; only the sentence about the doctor is withdrawn.
- **Relay.** Nothing about the patient is urgent by Sanad's own reading. He is told plainly that the message did not get through, asked to send it again, and told to contact his doctor himself if it cannot wait.

Both are fixed code strings in the patient's language and grammatical gender, like every other escalation block here. No model writes them.

The Care Coordinator's cost barrier is the same rule with a different shape: `_escalate` opens the relay, writes the escalation and puts the barrier card in front of the doctor BEFORE the "I told Dr X about the cost" template goes out, and a write that throws never reaches the template at all, so the turn falls back to the fixed ladder and the patient hears nothing rather than something false.

## Nothing a patient waits on runs without a deadline

The external model and storage calls a patient waits on run through `core/bounded.within`, with deadlines in `app/core/bounded.py`: the triage and output votes, Concierge reply, voice transcription, photo read and bucket write. Firestore operations sit outside this wrapper.

What "fail closed" means differs by call site and each one is written down where it lives:

- the two sentinel nets and the two output votes already failed closed to a relay; they now do it on a clock as well;
- the Concierge reply relays to the doctor, which is the path a model that refuses to answer already took;
- a photograph that cannot be read takes the "stored and relayed unread" exit that already existed for a photo Sanad will not act on;
- a voice note that cannot be transcribed is answered with one line asking for it again and a yellow card telling the doctor one arrived that Sanad could not hear. Nothing is answered and nothing is filed, because there is no transcript for the Sentinel to read;
- a bucket that does not answer costs the photograph and never the values on it. The card says the picture was not kept, so the doctor is not sent looking for it.

## What may be uploaded, and who decides

The size cap is applied while the body is read, not after it, and the lane (photograph or voice note) is decided from the file's own first bytes and never from the client's `content_type` header, which is a claim rather than a fact (`app/core/uploads.py`). A file that is neither a photograph nor a voice note is refused. ffmpeg runs on those bytes with a timeout, a bounded output duration and an explicit protocol whitelist.

A refusal is not an error and not a clinical event. The patient gets one line in his own language saying what to send instead, an event is written so the board carries the attempt, and the route answers 200.

## What Sanad never does

- Never changes a dose, starts or stops a medication on its own judgment: every instruction a patient receives traces to the doctor's own dictation.
- Never diagnoses: values are compared to targets, baselines and the critical table, and the report is assembled from stored facts; no model writes a clinical opinion.
- Never claims medication adherence: it records what was instructed and what was reported back, nothing more.
- Never calls a patient: text only.
- Never decides what to do with a photo on a model's word alone: a code table (`app/core/photos.py`) is what always ran and still runs, the Evidence Orchestrator's disposition since S24-E is recomputed against that same table rather than trusted, and `app/core/labs.py` does every comparison. A result with no order behind it is still read and compared, and a critical value still escalates, but nothing attaches or closes on Sanad's own judgment: the doctor gets it with two buttons and decides.
- The Concierge cannot write: it has no tool surface, because ADK 2.8.0 does not allow `output_schema` and `tools` together. Code-matched administrative replies can still invoke Coordinator tools, and plain-code patient lanes can still file messages, readings and evidence; those are explicit code routes, not Concierge capabilities.
- Never quotes a price: the Resolver's search reads Google Places, which has no price field, and the template that follows it says so out loud rather than guessing a number.
- Never redirects a patient to another doctor: a visit is moved to another day, on the doctor's own calendar, never to another clinic.
- Never lets a second opinion become a second authority: the Closure Auditor may refuse a close and may never approve one, and the Case Steward may revise or hold a proposal's timing and may never grant a permission the code guard in front of it already refused.

## Broad general-clinic scope

The sentinel phrase fixtures and lab table span stroke signs, anaphylaxis, obstetric bleeding, poisoning, infant illness and multiple laboratory categories. Prompts say "physician," and the seed set spans cardiology, endocrinology, nephrology, obstetrics and pediatrics. It is not validated for clinical use; the closing block below is what stands between this demo and a real patient.

**Roadmap, not shipped today:** per-doctor additions to the sentinel phrase list, so a specialist can add must-wake phrases specific to his own practice on top of the general-clinic floor. Today the table in `app/core/sentinel.py` is the same for every doctor.

## Data handling, and what stands between this and a real patient

Every patient in this repository and in the demo is invented; do not enter real patient data. Every fixture behind the Resolver, the Evidence Orchestrator, the Closure Auditor and the Case Steward is synthetic in exactly the same way.

Nothing repeats the AI disclosure after onboarding.

**WhatsApp is not in this revision.** There is no WhatsApp code path in this repository today; Telegram and the web console are the two live adapters, and both are demonstrations that the `ChannelAdapter` interface is genuinely removable, not a promise that a third one exists. A production WhatsApp channel is a month-two item: it starts on a Meta test number, moves to Meta Business Verification with Sanad as the single verified sender only once that is complete, and reaches no real patient before it.

Before any real patient is onboarded: a Law 151/2020 (Egypt's personal data protection law) review and a consent flow, clinical governance sign-off on the critical-lab and blood-pressure thresholds, a line-by-line clinical and legal read of the AI self-disclosure templates, and, for a production WhatsApp channel when one exists, Meta Business Verification with Sanad as the single verified sender. None of that has happened yet. This is the gate between hackathon demo and first real patient, not a checkbox already ticked.
