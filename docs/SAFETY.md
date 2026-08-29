# Sanad - Safety

Sanad's safety model rests on one rule: **anything that decides whether a message is an emergency, or whether a reply is safe to send, is code, not a model call.** The model transcribes, extracts, phrases, and casts one bounded vote. It never has the last word on a life-or-death decision. This document lays out the mechanism end to end. Every claim in it points at a file in this repository: the phrase table is `app/core/sentinel.py`, the critical-lab table is `app/core/labs.py`, the blood-pressure table is `app/core/vitals.py`, the output check is `app/core/validator.py`, and the gate order is the sequence of statements in `app/core/concierge.handle_patient_message`. Each has a regression suite under `app/tests/`, and the image does not build unless they pass.

Three sentences carry the whole model: **every gate that can escalate is code; a model on the safety path casts one bounded yes/no vote that can only ADD a relay or an escalation, never remove one; and every one of those calls fails closed, so an error relays to the doctor instead of passing the message on.** Text the doctor himself wrote is the one trusted path: it is delivered as his, prefixed with his name, and it is not rewritten, graded or voted on by anything.

**The tables are frozen, not just exercised.** Until S11 both regression suites iterated the table they were guarding, so they asserted that whatever the table currently holds still fires. Deleting a phrase deleted its own test with it and the build stayed green (Codex, reviews/codex-troubleshoot-1.md item 15). Both tables now also exist as a literal copy inside their test file, typed out rather than read from the module: `FROZEN_MUST_WAKE`, `FROZEN_CONCEPT_RULES`, `FROZEN_NEEDS_SUPPORT` and `FROZEN_NEVER_WAKE` in `app/tests/test_sentinel.py`, and `FROZEN_CRITICAL_LABS`, `FROZEN_UNIT_CONVERSIONS`, `FROZEN_ALIASES`, the four flag tables and the four abdominal-pain tables in `app/tests/test_labs.py`, and `FROZEN_CONTEXT_CLASSES` in `app/tests/test_validator.py`. Every table that decides something is in that list, which is the whole point of the sentence: a flag word is a threshold made of letters (remove "positive" from `HIGH_FLAGS` and a positive troponin quietly becomes "cannot judge"), an alias decides whether a row is found at all, and a negation decides whether a patient who said she has no pain is sent to an emergency room. Each frozen list is also run through the live code, not only diffed, so an entry that stops working fails even when the table itself is untouched. Removing a row, moving a threshold or changing a conversion factor fails the comparison, and the image does not build. A deliberate change is a change in three places at once: the module, the frozen literal, and this document. Every threshold in the table is also walked across its own boundary, just below it, at it and just above it, in the table's unit and in every alternate unit the conversion table knows, so a rule cannot quietly become an inequality it was not.

## The three-tier answer fence

Every patient message is answered in a fixed order, enforced by code, never left to a prompt to self-police:

1a. **The blood-pressure table**: a message that is nothing but a reading ("185/125") is graded by the three numbers in `app/core/vitals.py` before any model is asked anything. This runs ahead of the Sentinel because a measurement is not a sentence: sending a real 185/125 through the deployed service showed the Sentinel's model vote escalating it first, so the reading never reached the table and never reached the patient's chart. Only a red reading is taken here; a reading the table calls normal falls through to the Sentinel, so the model vote can still add an escalation and this ordering can never remove one.
1. **Sentinel**: can only escalate. If either net fires, no answer is generated at all; the patient gets a canned emergency block and the doctor gets a card. See below. If the model net could not be reached at all, the gate still fires, and the message is relayed to the doctor as a yellow "triage unavailable, please read" card while the patient gets the relay line; the audit line reads `model:error -> relayed`.
1b. **Treatment change**: a request to change, stop, start or substitute a treatment is caught by `validator.wants_treatment_change` (literal phrases plus token rules in three languages) and then by one model yes/no vote that can only add a relay. This gate runs **before the photo branch**, so a caption under a photo is gated exactly like typed text.
2. **Plan**: if the message is clear of the sentinel, and the question is about the patient's own care, the answer comes only from `plan_text`, the doctor's own written words, injected as the single source of truth. The Concierge holds no tool of any kind (see "No tool surface" below), so there is no mechanism by which it could read another patient's data even if asked to.
3. **General**: a question not covered by the plan gets educational information. The model is instructed not to write a single digit, dose, or measurement in a general-tier answer at all. Numbers are described in words instead, and every general answer closes with a line that the doctor's plan is what counts for this patient.

Anything that falls outside these tiers (a request to change treatment, a question the model is not confident about) does not get answered at all. It gets the relay line in the patient's own language, and the message is flagged for the doctor. A request to change treatment is caught earlier still: `validator.wants_treatment_change` matches it in code before the Concierge is ever called, so that class of request has no generation step at all, not just a post-hoc check. The model vote behind it can add a relay the code list missed, and it fails closed: if the call errors, the message relays.

**No tool surface.** ADK 2.8.0 does not allow an agent to combine `output_schema` (the structured tier/reply/relay-reason output the Concierge returns) with `tools` in the same definition. Rather than drop structured output, the Concierge carries no tools at all: the plan text and the patient's open loops are fetched by plain code and written directly into the instruction as text. The result is a stronger guarantee than "read-only tools" would have been: there is no callable surface on the patient path, writable or not, for a crafted message to target.

## The two sentinel nets

Both nets run on every patient message, in order, before any reply is generated. Either one firing is sufficient to escalate; neither can be skipped or talked out of it by the patient's own words, because neither is a prompt instruction: both are code paths that run before generation.

**Net 1: code, and it is two nets in one.** The patient's text is normalized (Arabic diacritics stripped, common letter variants unified, lowercased, Franco-Arabic digits preserved, and Franco spellings of one word folded onto one form through `sentinel.FRANCO_ALIASES`, so "nafasy", "nfsy" and "nafsi" are the same word). It is then matched against the phrase table `MUST_WAKE`, which covers all clinic specialties in Egyptian Arabic, Franco-Arabic and English, and after that against `sentinel.CONCEPT_RULES`, which match a **set of tokens** instead of a sentence: a chest word with a pain word, a breath word with an inability word, a face word with a drooping word, a limb word with a weakness word, a lips or face word with a blue word. That is what catches "وجع فظيع بمنتصف الصدر ونازل لدراعي الشمال" and "my face suddenly went crooked and my left hand has no strength", which no phrase list held. A hit on either skips every later step, including any model call. This is the deterministic floor: it does not depend on the model being available, working correctly, or resisting a clever prompt.

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

The direction of the change is worth being precise about, because it is the only change in this build that can make the Sentinel fire *less*. It can only ever stop those three specific words from firing, only when the sentence carries nothing else from their own concept, and only on the code net - the model net still runs on exactly the same sentences and can still escalate them. "I have a pounding headache since this morning" run against the deployed service produces no escalation from either net and a yellow relay card for the doctor, which is the right answer: a new symptom the doctor should see, not an ER instruction. `app/tests/test_sentinel.py` holds both halves - the benign sentences that must not fire, and "my heart is pounding and I feel dizzy", which must.

## Where a model output enters the safety path

There are exactly five places, and each one is named here so the claim can be checked rather than believed:

1. **A voice note's transcript.** For voice notes the code sentinel runs on the transcript, which is a model output; the modality boundary is stated. `core/dispatch.py` transcribes and then calls `sentinel.check` on the transcript on that same lane, before the Concierge is called at all, and hands the verdict to the Concierge rather than letting it ask for one later.
2. **A photograph's contents.** Image content is model-read: the extractor's model classifies the picture and transcribes what is printed on it, and it is told, in the instruction and by the schema, that it may not judge, interpret, reassure or advise. Every comparison after that is `core/labs.py` or `core/vitals.py`. The caption on the photo is the patient's own text and goes through the sentinel and the change-request gate before the extractor is called. The analyte names and flags the model read back are themselves passed through the sentinel word list.
3. **The triage vote** (`sentinel.model_net`): one call, yes/no schema, temperature 0, can only add an escalation, fails closed.
4. **The change-request vote** (`validator.model_change_vote`): one call, yes/no schema, temperature 0, can only add a relay, fails closed.
5. **The reassurance vote** (`validator.model_reassurance_vote`): one call, yes/no schema, temperature 0, asked only about a reply the code rules already passed, can only add a relay, fails closed.

Everything else on the patient path is code. And one path carries no model at all: **text the doctor wrote himself is the trusted path.** His answer to a card is delivered as his, prefixed with his name, appended to the plan as a dated addendum, and neither validator gate nor either vote is applied to it.

## Gendered wording, because getting it wrong is a safety-adjacent failure

Arabic conjugates the second person. A reminder that reads "فاكر" to a man reads wrong to a woman, and Mohamed's first real phone test found exactly that: a female patient addressed throughout as a man. A patient who can tell the message was not written for her is a patient who trusts the next one less, including the one that tells her to go to an emergency room.

The fix is code, not a prompt. `app/core/gender.py` turns the record's `sex` field into one of three answers - masculine, feminine, or unknown - and every template that addresses or mentions a patient picks its wording from that: the three nudge rungs, the monitoring reminder, the emergency block, the critical-lab block, the doctor's cards ("his"/"her"/"their"), the Telegram lines. Unknown is a real third answer with its own wording, not a fallback to masculine: a doctor who did not say the sex gets text that commits to neither, rather than a guess with a good chance of reading wrong. `app/tests/test_gender.py` asserts that a woman never receives a masculine form, that a man keeps his, and that the unknown form contains no gendered verb at all.

## The one-message correction rule

When a message is relayed, the doctor sees a yellow card with the patient's question and, if the model proposed one, its draft reply. The doctor answers from the console; that answer is delivered to the patient prefixed with the doctor's name, and it is appended to the plan as a dated addendum. The next question from that patient sees the updated plan. Sanad never guesses twice at the same gap: it surfaces the gap once and then treats the doctor's answer as part of the record.

## The critical-lab table

Approved by the doctor before it was written down, and it does not move at runtime. The table and its `judge()` function are plain data and a pure function in `app/core/labs.py`, covered by `app/tests/test_labs.py`. The extractor that calls them (`app/core/extractor.py`), the critical-value escalation, and the private Cloud Storage bucket the images go to are all built, and were exercised against the deployed service with five synthetic slips carrying twenty-three analyte rows between them. A potassium of 6.7 is a red card because `6.7 > 6.0` in the table below, not because a model thought the slip looked bad, and the card says so on its own face.

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

Two verdicts come out of it and they are not the same verdict. **Attaches** is whether the values may be written onto the patient's loop at all: only an identity failure says no, because most Egyptian lab slips print a name and some do not, and refusing the ones that do not would mean refusing most real results. **Satisfies** is whether the slip closes the evidence side of the obligation, and since S11 all three checks have to pass, which is not the same thing as none of them failing. A slip with no printed name and a slip with no readable date each cost one check, and a check that could not be made is not a check that passed: the values attach, the doctor sees them, the card names which check stood down ("the identity and the date check could not be done on this slip, so the values are attached for your review and the obligation stays open"), and the loop stays open exactly as it does for a partial result. The Coordinator's `request_missing_evidence` path already handles "not satisfied", so nothing new had to be built behind it. Before S11 only a date before the order was refused, so an unnamed or undated slip closed an obligation on a check nobody had made.

## The blood-pressure table

Blood pressure arrives two ways: the patient types "185/125" into the chat, or photographs the machine and the model transcribes the two numbers off the screen. Both go through the same three lines of `app/core/vitals.py`, which is why they cannot be graded differently.

| Reading | Level | What happens |
|---|---|---|
| systolic 180 or above | hypertensive crisis | red card to the doctor **and** the emergency block to the patient |
| diastolic 120 or above | hypertensive crisis | red card to the doctor **and** the emergency block to the patient |
| systolic below 90 | low blood pressure | red card to the doctor **and** the emergency block to the patient |
| anything else | filed | it joins the chart, and nothing is called dangerous |

Three things about this are deliberate.

The two crisis rows are an OR, not an AND: either number reaching its own cutoff is enough on its own, so 150/125 escalates even though the systolic is unremarkable.

Both red rows reach the patient. The first build of this table sent the emergency block for a crisis only and left the low row as a card to the doctor, and the question went back to the doctor: is 85 systolic measured at home something a patient should sit with? It is not. Both red rows now send the same emergency block, and `app/tests/test_vitals.py` asserts all three rows, so a reading that is red for the doctor and quiet for the patient cannot come back by accident.

The reading is filed to the chart whatever the verdict, including a crisis. A chart that silently drops the worst reading in the series is worse than no chart.

The emergency text a red reading sends is the same block the Sentinel sends, in the patient's language and grammatical gender. No new wording was written for it, so there is one Arabic emergency instruction in the system rather than two that could drift apart.

What this table is not: a hypertension grading scale, an opinion about trend, or anything that reads symptoms. It is a floor under the readings that must not be missed while the doctor is asleep.

## AI self-disclosure and disclaimer

Sanad introduces itself as an AI assistant, not a doctor, on first contact and whenever a patient could reasonably mistake its reply for the doctor's own words.

**English:**
> Hi, I'm Sanad, Dr [Name]'s AI assistant. I follow up on your care plan and can answer questions based on what the doctor wrote for you. I'm not a doctor and I don't make medical decisions: for anything urgent, or anything outside your plan, I'll get the doctor involved.

**Egyptian Arabic:**
> أهلاً، أنا سند، المساعد الذكي بتاع دكتور [الاسم]. بتابع معاك خطة العلاج وبجاوبك من كلام الدكتور اللي كتبه لك. أنا مش دكتور ومش بتخذ قرار طبي؛ أي حاجة عاجلة أو مش موجودة في خطتك هوصلها للدكتور.

The wording that actually ships is in `app/core/tg_router.py` (the introduction an unknown chat gets, and the welcome a patient gets when the link binds) and in `app/core/sentinel.py` (the emergency block). It has not been signed off line by line; that review sits with Mohamed before any real patient is onboarded, alongside the Law 151/2020 work below. It is listed under "Data handling" as a gate, not marked as done.

## The doctor is told before the patient is told he was told

"Your doctor has just been alerted" is the strongest sentence Sanad says to a patient. It ends the emergency block, the critical-lab block and the crisis blood-pressure block, and it is the sentence that makes a frightened patient stop typing and go. It used to be sent FIRST on every one of those paths: the reassurance went out, and only then were the escalation event, the relay and the doctor's card written. A Firestore timeout, a cold instance or a crash in between left the worst state this system can be in, a patient who has been told to stop waiting and a doctor who was never told anything.

The order is inverted on all five paths (the emergency, the triage outage, the ordinary relay, the critical lab, the crisis blood pressure) and it is inverted in one function, `core/escalate.told_or_fail_closed`, so that no branch can be given the promise without the fallback that goes with it. It runs the persistence and answers whether it landed. A caller that gets True says the sentence it always said. A caller that gets False says a fail-closed line instead, and an error event is written so the failure is on the board and not only in a log.

Two fail-closed lines, because the two situations are not the same:

- **Emergency.** The finding still stands, so the patient is still sent to the nearest emergency room or told to call 123. That instruction never depended on the doctor hearing anything; only the sentence about the doctor is withdrawn.
- **Relay.** Nothing about the patient is urgent by Sanad's own reading. He is told plainly that the message did not get through, asked to send it again, and told to contact his doctor himself if it cannot wait.

Both are fixed code strings in the patient's language and grammatical gender, like every other escalation block here. No model writes them.

The Care Coordinator's cost barrier is the same rule with a different shape: `_escalate` opens the relay, writes the escalation and puts the barrier card in front of the doctor BEFORE the "I told Dr X about the cost" template goes out, and a write that throws never reaches the template at all, so the turn falls back to the fixed ladder and the patient hears nothing rather than something false.

## Nothing a patient waits on runs without a deadline

Every dependency on the patient's lane is somebody else's service, and a call that hangs is indistinguishable from one that is down. Each of them now runs inside `core/bounded.within`, with one table of deadlines in `app/core/bounded.py`: the triage vote, the two output votes, the Concierge reply, the voice transcription, the photo read and the bucket write. None of them may become an HTTP 500, because a 500 tells the patient nothing, tells the doctor nothing, and leaves no record that anything happened.

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

- Never changes a dose, starts, or stops a medication on its own judgment. Any medication instruction a patient receives traces to the doctor's own dictation.
- Never diagnoses. Lab values are extracted and compared to targets, baselines, and the critical table; they are never interpreted into a diagnosis.
- Never claims medication adherence. The system records what was instructed and what was reported back; it does not claim or imply that a patient actually took a medication.
- Never has the bot call a patient. Outbound contact is text/message only (Meta blocks bot-initiated calls to Egypt in any case; patient-initiated calls to Sanad are a later phase, not this build).
- Never decides what to do with a photo by asking a model. The model says what the picture is and reads what is printed on it; a table in `app/core/photos.py` turns that answer plus the patient's open loops into a route, and `app/core/labs.py` does every comparison. A result that arrives with no order behind it is still read and still compared - a critical value escalates on that path exactly as it does on any other - but it is never attached to a record or closed off on Sanad's own judgment: the doctor gets it with two buttons and decides.
- Never lets a patient conversation write to the record. The Concierge has no tool surface at all: ADK 2.8.0 does not allow `output_schema` and `tools` together, so the plan and open loops are fetched by code and injected as text instead. This isn't a prompt instruction to resist; it's a capability that does not exist in that code path.

## All-specialty scope

The sentinel phrase list, the critical-lab table, and the extraction/comparison logic are written for any clinic specialty, not for cardiology: the concepts in `sentinel.MUST_WAKE` run from stroke signs and anaphylaxis to obstetric bleeding, poisoning and a limp infant. Prompts refer to "physician," never "cardiologist." The demo's lead patient is cardiology because that happens to be the doctor's own story, and the seed set (`docs/seed/patients.json`) spans cardiology, endocrinology, nephrology, obstetrics, and pediatrics to demonstrate this directly.

**Roadmap, not shipped today:** per-doctor additions to the sentinel phrase list, so a specialist can add must-wake phrases specific to his own practice on top of the general-clinic floor. Today the table in `app/core/sentinel.py` is the same for every doctor.

## Data handling

Every patient in this build, including everything in `docs/seed/`, is synthetic. No real patient data has been entered into Sanad at any point in its development.

Before any real patient is onboarded, the product needs a Law 151/2020 (Egypt's personal data protection law) review and a consent flow, and the WhatsApp channel needs Meta Business Verification with Sanad as the single verified sender. None of that has happened yet; it is scoped as the gate between "hackathon demo" and "first real patient," not a checkbox already ticked.
