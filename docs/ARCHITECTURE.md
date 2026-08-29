# Sanad - Architecture

This document describes the system as it is deployed. It was built in slices - S1 (core + web console), S2 (Concierge, Sentinel, validator, Telegram), S3 (Lab-Extractor, Chaser on Cloud Tasks, completion report), S4 (console, patient page, photo classification, gender), S5 (the blood-pressure table, slip-to-loop matching, ambiguous names), S6 (the Care Coordinator, the Care Contract, the slip verifier, the end-of-day summary) - and every one of them was exercised against the deployed Cloud Run service with curl, never only locally. Each slice was also read line by line by an independent reviewer before it was accepted. Where a claim below is about behaviour, the file named beside it is the code that behaves that way, and `app/tests/` holds the suite that keeps it honest: the container image does not build unless the suite passes.

## Positioning

A hybrid autonomous care-loop system: Gemini agents understand people, evidence and barriers; a deterministic safety kernel controls clinical boundaries; durable cloud infrastructure carries the objective across time. The product line is the same sentence from the other end: **the doctor gives the plan once, and Sanad carries it until reality matches.**

There are **three agents**, never six, and their toolsets are disjoint:

| Agent | What it decides | Tools | Output |
|---|---|---|---|
| **Registrar** (`core/registrar.py`) | what the doctor just dictated | none | `ProposedRecord`, a pydantic schema code then validates |
| **Care Coordinator** (`core/coordinator.py`) | what to do next about one open obligation | seven, listed in `core/policy.TOOLS`, each one guarded in code before it runs | one tool call, or nothing |
| **Concierge** (`core/concierge.py`) | how to answer one patient message | none at all, by design | `ConciergeAnswer`, checked by the validator before it is sent |

No agent holds another's tools. The Concierge, which is the only one a patient talks to, has no tool surface at all: ADK 2.8.0 does not allow `output_schema` and `tools` on the same agent, so the plan and the open loops are fetched by plain code and injected as text. The Coordinator has tools but no words: every sentence it can send a patient is a template in `core/templates.py`. The Registrar writes nothing until the doctor taps Confirm.

**One caveat, stated here rather than left to be found.** There are three ADK agents, and there is a fourth code path that produces one of the same outputs without ADK. A photographed prescription is read by `registrar.propose_from_image`, which calls Gemini through `google.genai` directly with `response_schema=ProposedRecord`, because the input is image bytes plus a prompt and an ADK agent carrying an `output_schema` takes a text turn. It is the same model, the same schema and the same `ProposedRecord` the ADK Registrar returns, and both of them then go through the identical code validation in `core/registrar.py`, which is what actually decides whether a proposal may become a patient. So "three agents" is a statement about the tool surfaces, not a claim that every model call in the system is wrapped in an `Agent`: it is not, and the honest count below says how many are. Ten direct `google.genai` calls live in `core/` (the sentinel's model net, the change vote, the reassurance vote, the administrative vote, the slip extractor, transcription, the report line, the prescription read, and the S9 identification of which patient a dictation is about) against three ADK agent turns, and each of those nine is a single structured yes/no or one read with a schema on it, never an actor with tools.

A rendered image version of the diagram below is at `docs/architecture.svg`, for use on Devpost where an embedded mermaid block will not render. The two are kept node for node: `app/tests/test_architecture_diagram.py` fails the image build if a node exists in one and not the other, which is how the S4 diagram was allowed to sit two slices behind the code until rev 17.

## System diagram

```mermaid
flowchart TB
    subgraph channels["Channels: one interface, interchangeable"]
        WEB["Web console (/c/token)<br/>doctor box + patient box + board + feed"]
        TG["Telegram adapter<br/>webhook, secret_token verified"]
    end

    ADAPT["ChannelAdapter interface<br/>inbound(message) / send(target, text|card)"]

    subgraph core["Cloud Run service sanad, stateless per request"]
        DISPATCH["dispatch.py<br/>plain-Python routing by sender identity<br/>NO model in this decision"]

        subgraph doctorpath["Doctor path"]
            REG["Registrar (ADK agent)<br/>dictation/text -> ProposedRecord (pydantic)<br/>code validates loop types & required fields"]
            CONFIRM["Confirm card -> pending_confirms<br/>doctor taps Confirm/Cancel"]
        end

        subgraph patientpath["Patient path: the safety kernel, in the order code runs it"]
            VITALS["1. Blood-pressure table (code)<br/>core/vitals.py, three numbers, no model"]
            S1NET["2. Sentinel net 1 (code)<br/>phrase table + concept rules<br/>core/sentinel.py MUST_WAKE, CONCEPT_RULES"]
            S2NET["3. Sentinel net 2 (model)<br/>would the doctor want to be woken now?<br/>yes/no, structured, add-only<br/>fails closed to a relay"]
            CHANGE["4. Treatment-change gate (code, then one vote)<br/>core/validator.wants_treatment_change<br/>add-only, fails closed to a relay"]
            ADMIN["5. Administrative tier (code)<br/>core/intents.py, six chores<br/>acts only through the Coordinator's tools"]
            CONC["7. Concierge (ADK agent)<br/>no tools at all (output_schema + tools<br/>is disallowed in ADK 2.8.0); plan and<br/>open loops injected as text by code"]
            VALID["8. Output validator (code)<br/>no-reassurance list, then every number<br/>traced to a plan number of the same class"]
            REASSURE["9. Reassurance vote (model, add-only)<br/>fails closed to a relay"]
        end

        subgraph labpath["Lab path"]
            LAB["Lab-Extractor<br/>photo -> structured analytes (Gemini)<br/>compare vs target/baseline/critical table (code)"]
            VERIFY["Slip verifier (code)<br/>core/verify.py: printed name, collection date,<br/>every analyte the doctor asked for"]
        end

        subgraph chasepath["Chase path"]
            CHASER["Chaser<br/>Cloud Tasks handler<br/>run id, quiet hours, one a day<br/>ledger claimed before the agent thinks"]
            COORD["6. Care Coordinator (ADK agent)<br/>seven guarded tools, one action per wake-up<br/>templates only, never free text<br/>fails closed to the ladder"]
            POLICY["core/policy.py (code)<br/>registered as ADK's before_tool_callback<br/>every tool call checked before it runs<br/>a refusal goes back with its reason"]
        end
    end

    GEMINI[["Gemini 3.5 Flash: Vertex AI, global<br/>service-account auth, no API key"]]
    FS[("Firestore Native, europe-west1<br/>doctors · patients · loops · events · sends")]
    TASKS[["Cloud Tasks queue, europe-west1<br/>OIDC-signed wake calls"]]
    GCS[("Cloud Storage<br/>lab slip images, private bucket")]
    SM[["Secret Manager<br/>admin secret · bot token"]]

    WEB --> ADAPT
    TG --> ADAPT
    ADAPT --> DISPATCH
    DISPATCH -->|sender = doctor| REG
    DISPATCH -->|sender = patient| VITALS
    REG --> GEMINI
    REG --> CONFIRM --> FS
    CONFIRM -.due-date loop.-> TASKS

    VITALS -->|red reading| ESCALATE["Escalation: canned ER block to patient<br/>+ red card to doctor<br/>NO model call happens"]
    VITALS -->|normal, or not a reading| S1NET
    S1NET -->|hit| ESCALATE
    S1NET -->|clear| S2NET
    S2NET -->|yes| ESCALATE
    S2NET -->|no| CHANGE
    CHANGE -->|asks to change treatment| VALID
    CHANGE -->|clear| ADMIN
    ADMIN -->|a chore, or a reply about an open loop| COORD
    COORD -.stands down, or it was a question.-> CONC
    CONC --> GEMINI
    CONC --> VALID
    VALID --> REASSURE --> ADAPT
    ESCALATE --> ADAPT
    ESCALATE --> FS

    LAB --> GEMINI
    LAB --> GCS
    LAB --> VERIFY --> FS
    LAB -->|critical value| ESCALATE
    VERIFY -.evidence that does not satisfy the contract.-> COORD

    TASKS -->|fires on schedule or /force_due| CHASER
    CHASER --> COORD
    COORD --> POLICY
    POLICY -.refused, with the reason.-> COORD
    COORD --> GEMINI
    COORD --> FS
    COORD --> ADAPT
    COORD -.no choice, or model down.-> CHASER
    CHASER --> FS
    CHASER --> ADAPT

    core -. reads at boot/per-request .-> SM
```

## Request lifecycle 1: a patient message

1. **Load.** The handler resolves the sender to a patient record, then loads that patient's plan text, targets, open loops, and last ten events from Firestore. A voice note is transcribed to text first (ffmpeg to 16kHz mono WAV, then Gemini). A photo on a message with no open TEST loop is logged and relayed to the doctor as a yellow card rather than entering the lab path.
2. **The blood-pressure table (code, before either net).** A message that is nothing but a reading is graded by `core/vitals.py` first: three numbers, no model. A red reading is filed to the patient's monitoring chart, the patient gets the emergency block, and the doctor gets a red card naming the table in `decided_by`. A reading the table calls normal continues to the Sentinel like any other message.
3. **Sentinel net 1 (code, always runs first on anything that is not a bare reading).** The text is normalized (Arabic diacritics stripped, common letter variants unified, lowercased, Franco-Arabic digits kept, Franco spelling variants folded onto one form) and matched against the phrase table `MUST_WAKE` in `core/sentinel.py`, then against `CONCEPT_RULES`, which match token sets rather than sentences (chest with pain, breath with an inability, face with drooping, a limb with weakness, lips with blue). A hit on either skips every later step: no model call happens at all. The patient gets the canned emergency block in his own language, an escalation event is written, and the doctor gets a red card naming the patient, the quoted text, the matched concept, and the time.
4. **Sentinel net 2 (model, always runs if net 1 was clear).** One Gemini call asks a strict yes/no question: would this patient's physician want to be woken right now by this message? The never-wake examples from `sentinel.NEVER_WAKE` are supplied as negatives. A "yes" takes the identical emergency path, marked on the event as caught by the model net rather than the code net. Both nets always run in sequence; either firing is sufficient, and which one fired is stored on the event, which is the audit trail shown in the demo. The call fails closed: an error or a timeout fires the gate as `model:error`, the patient gets the relay line, and the doctor gets a yellow "triage unavailable, please read" card carrying the message verbatim.
   **The modality boundary.** For voice notes the code sentinel runs on the transcript, which is a model output; the modality boundary is stated. `core/dispatch.py` transcribes on the patient lane and calls `sentinel.check` on the transcript there, before the Concierge is called, and passes the verdict in. Image content is model-read, and the caption on an image is patient text that goes through the sentinel and the change gate before the extractor.
5. **A code gate before the model ever runs, and before the photo branch.** A request to change treatment (`validator.wants_treatment_change`, literal phrases plus token rules in Arabic, Franco-Arabic and English) is matched in code and never reaches the Concierge at all: the relay line is produced directly, with no generation in that path. If the code list is clear, one yes/no model vote (`validator.model_change_vote`) is asked, and it can only add a relay; it fails closed. Both run before the photo branch, so a caption asking to double a dose is gated exactly like the same sentence typed on its own.
5b. **The administrative tier, in code, before any model turn** (`core/intents.py`, S6++ item G). Six things a patient says are chores rather than questions: "I did the test", "I lost the prescription", "can I come Thursday instead", "where do I send it", "the medicine is not available", "I forgot to measure". Each is matched against a short pattern list in Egyptian Arabic, English and Franco-Arabic, folded by the same normalizer the Sentinel uses; if the list misses, one yes/no model vote may name one of the two that only answer, and it can only add a match, never remove one, and it fails closed to no intent at all. The four that change the plan of work need a code pattern match and nothing else will do: a vote naming one of those is discarded and the message falls through to the Coordinator, because "a model may add a relay, never a change" is the rule this whole system is built on and moving an appointment is a change. Two of the six only answer: the doctor's own plan text is sent again, or the patient is told to send a photo here. The other four change the plan of work, and every one of them is carried out through the Coordinator's own guarded tools, so the schedule window, the one-a-day rule, the quiet hours and the six-contact cap bind an intent exactly as they bind the agent. A guard that refuses is a stand-down, not a workaround: the message falls to the tiers below unchanged. Only one of the six ever reaches the doctor, and it is the one that should: the medicine being unavailable records an availability barrier and cards him, and no substitute is ever suggested, because there is no tool for one and no sentence for one.

**Where the guard is wired.** `core/policy.check` runs at two points, on purpose. The first is ADK's own `before_tool_callback`, registered on the Coordinator agent (`core/coordinator.before_tool`): the framework calls it with the tool and its arguments before any tool body runs, and a dict returned from it becomes the tool's answer, so a refused call never enters the function at all. The second is the `propose` at the end of each of the seven tool bodies, which is the line that still holds if the callback is dropped by an SDK upgrade, and the one the administrative tier uses, because that caller is code and never goes through an agent turn. `Turn.precheck` hands the accepted answer from the first through to the second, so the doubled call costs nothing and changes nothing. Two enforcement points, one rule, one file.

5c. **The Care Coordinator, before any generation and after every gate.** If the message is not a bare reading, is not a treatment-change request, and the patient has an obligation Sanad is still carrying, the Coordinator is woken for that obligation (`core/coordinator.py`). It reads the objective, the loop's own numbers, the doctor's policy and the last ten events, and it calls exactly one of seven tools. Every call is put to `core/policy.check` first, in code, and a refusal comes back to the model as a reason it can choose again inside. Whatever it decides, the patient hears a template and never a generated sentence: "we will check again on 2026-09-01", "I told Dr Mohamed about the cost", "please send the missing part: Potassium". If it errors, times out, calls nothing, or has every call refused, it stands down and the Concierge answers exactly as it did before S6. That is the fail-closed default and it is the same one the Chaser uses.

6. **Concierge, tier order enforced by code.** For everything else, an ADK agent answers with `output_schema=ConciergeAnswer` and **no tools at all**: ADK 2.8.0 does not allow an agent to carry both `output_schema` and `tools` in the same definition, so the plan and the patient's open loops are fetched by plain code and injected directly into the instruction text rather than exposed as callable tools (`app/core/concierge.py`: "on the patient path there is no tool surface at all, writable or not"). The instruction fixes the tier order: plan questions are answered only from the injected plan text; general questions get education framed as general information: the model is told not to write a single digit, dose, or measurement in a general-tier answer at all, describing things in words instead: always closing with a line that the doctor's plan is what counts; anything the model is not confident about gets the relay line in the patient's language instead of an answer, flagged for the doctor. The patient's own text sits inside a clearly delimited block the instruction marks as untrusted, so nothing inside it can rewrite the agent's rules: this is what makes the jailbreak-refusal demo beat honest rather than a scripted denial.
7. **Output validator (code, always runs on whatever came back, model-generated or code-shortcut, before send).** A no-reassurance list (Arabic, English and Franco-Arabic phrases such as "don't worry," "it's normal," "متقلقش," "كله تمام," "mafeesh moshkela") replaces the whole reply with the relay line if matched. Every number is read with the unit beside it and has to match a plan number of the same class, so "7 days" in the plan never licenses "7 mg" in a reply; word numbers, Unicode superscripts and Arabic-Indic digits are folded to digits first. A general-tier number framed as a range is allowed only in a sentence with no unit, no imperative and no drug name. A drug not in the plan mentioned with a dose forces a relay, and so does any capitalized or Arabic word within thirty characters of a dose or of "take"/"خد" that the plan never mentions. The verdict is stored on the event.
8. **The reassurance vote (model, add-only, fails closed).** A reply the code rules passed is put to one yes/no call: does this minimize, reassure, or tell the patient not to worry? A "yes" relays. No model-generated reply reaches a patient without both gates. A reply the doctor wrote himself is the trusted path and goes through neither.
9. **Send.** The reply (or the relay line) goes out through the same `ChannelAdapter` the message arrived on. A relayed message also produces a yellow doctor card; the doctor's answer to that card is delivered to the patient prefixed with his name, and is appended to the plan as a dated addendum: the one-message correction rule, so the next patient question already sees the update.

## Request lifecycle 2: a doctor dictation

1. **Load.** Text, or audio transcoded and transcribed the same way as the patient path.
2. **Registrar (ADK agent, `output_schema=ProposedRecord`).** One turn, through a throwaway `InMemorySessionService` created and discarded within the request: no session is ever reused across requests, which is visible in `core/registrar.py` and was checked against the deployed service. The model extracts a structured patient record, a rewritten plan in plain language, and a list of proposed loops (type, title, details, `due_in_days`). Relative dates ("in two weeks") are resolved to real timestamps in Python at commit time, never left to the model, so "two weeks" always means the same thing regardless of phrasing.
3. **Code validation, before anything is shown to the doctor.** Every proposed loop must carry a type from the fixed enum; a MEDICATION loop must have a drug and an action; a TEST loop must have a test name. A dictation that fails this comes back as a request to restate, not a half-formed record. A placeholder name ("Unknown," "N/A," "غير معروف") is treated as a missing name and triggers the same "please restate" response rather than silently creating a patient with a placeholder identity: a defect the S1 build surfaced and the S2 carry-over list fixes.
4. **Identification: which patient is this about (S9).** Before the card is built, two answers are taken and code decides between them. The first is `core/names.resolve`, the same name matcher `/report` and `/force_due` use, run over the doctor's own board: nobody, exactly one, or more than one. The second is a Gemini read (`core/identify.py`), given a compact list of up to fifty of his patients, the most recent by last event, with id, name, age, sex, diagnosis, last seen date, and the free-text notes he has made about who each of them is. It returns a structured verdict: an intent (`new_patient`, `existing_patient`, `lookup`, `unclear`), candidate patient ids with a confidence and a one-line reason quoting the words that matched, and any relationship worth remembering ("father of Dr Tarek", "lives in Zagazig"). It exists because a doctor does not always say a name: "the father of my friend Tarek" and "the old lady I saw last week with the swollen legs" are what a name matcher has nothing to say about.

   The rules over that verdict are code, and they are the whole safety argument. An explicit "this is a new patient", in Arabic or English, matched by a phrase list, always wins as new whatever the model returned. The auto-selected "Existing patient" card needs both halves to agree and each of them to name exactly one patient: one name match in code, one model candidate at or above the confidence threshold, and the same id. Anything else asks, with one button per candidate labelled "Ahmed Ali, 58, heart failure" and the model's quoted reason printed beside it: a description-only match asks, a confidence below the threshold asks, two candidates ask. "unclear" asks for the name. "lookup" lists the matching records with a button that opens one and creates nothing at all, not even a pending proposal. A model error or a malformed verdict falls back to the code name matcher and to the ask card, never to a silent guess, and no path attaches a dictation to a record without the doctor tapping the record's own name.

5. **Confirm card.** One event, one card, with the plan summary and each loop as a line. On a new patient the title says "New patient: Mariam Fouad" and the buttons are Confirm and Cancel. On an existing patient the title says "Existing patient: Ahmed Ali, 58, heart failure", the lines separate what is being added (the new loops, each as its contract) from what is being changed (only the demographic fields the dictation actually mentioned, printed as "old becomes new"), the plan is shown as the dated addendum it will become, and a third button says "This is a new patient" and switches the proposal back with one tap.
6. **Commit.** On confirm of a new record, the patient and loop documents are written to Firestore, any due-dated loop gets its nudge schedule created as Cloud Tasks, and a deep-link token for patient onboarding is generated (Telegram) or the patient is marked active directly (web demo). On confirm of an addition, the new loops are created on the existing record and only those are scheduled, the plan grows a dated addendum through the same function the doctor's own relay answers use (`core/concierge.with_addendum`) and is never replaced, the fields the dictation did not mention are not touched, baseline and target metrics are merged by name, any relationship note is appended to `patient.notes` with its date, and no second link is minted because the patient already has one. On cancel, the proposal is discarded and nothing is written.

## Request lifecycle 3: a scheduled chase

1. **Wake.** Cloud Tasks fires the Chaser handler at the scheduled time (or immediately, if a doctor sent `/force_due`, which enqueues the identical task type with a near-zero delay rather than running a separate demo-only path). The request must carry a valid Google-signed identity token for the runtime service account; the task-creating identity is separately granted permission to act as that service account, which is documented as the known trap in Cloud Tasks + Cloud Run setups.
2. **Guard checks, in order.** The handler drops the task if its stamped demo-run id does not match the current one (so a purged rehearsal can never fire into the next take), re-arms it if it is only a hop (see below), drops it if the loop has been paused on a recorded barrier, checks quiet hours (22:00 to 09:00 Cairo) and the one-message-per-patient-per-day rule, then puts the same schedule guard the Coordinator's tools have to pass to `core/policy.check`: the doctor's window, one contact a day on this loop, and never more than six contacts on one loop, whoever asked for the message. `/force_due` is exempt from the last three for the same reason it is exempt from quiet hours: the doctor asking for it now is the doctor's call. Finally the idempotency pair (loop id, attempt number) is claimed in Firestore before anything is sent, so a retried task can never double-message a patient.
2b. **The Coordinator decides what the wake-up is for.** It is asked before anything is sent. On a wake-up with nothing new from the patient the answer is `schedule_next_contact` with zero days, which means "the reminder that is due now", and the ladder step below is what carries it out. Anything else it chooses (a barrier to escalate, a loop to pause, a contact to move) happens instead of the nudge, and the audit line says which tool and why. When it stands down the ladder runs unchanged, and the audit line says `fallback: ladder (model unavailable)`.
3. **Nudge.** If all guards pass, the patient is messaged in his own language through his adapter, naming the doctor, offering help if the underlying issue is the blocker, and the attempt is recorded. `attempts` is the ladder counter and any patient reply resets it; `contacts` is every message the loop has ever cost the patient and it never resets, which is what makes the six-contact promise true.
4. **Patient response.** Any reply on an open loop resets the attempt counter. If the reply is the expected kind of evidence (a lab photo on a TEST loop, a value on a MONITOR loop), it routes into the Lab-Extractor or the monitoring table; otherwise it is logged and, if unclear, relayed.
4b. **The 720 hour ceiling, and the hop that gets past it** (`core/tasks.py`). Cloud Tasks refuses a schedule time more than 720 hours out, which is a hard API limit. "Come back in a month" opens a visit due in 30 days whose last ladder rung lands at 33, and before this was fixed that made the create call throw and the whole Confirm return 500 with no patient link ever minted. No task now carries a delay longer than 28 days: a contact further away is scheduled for 28 days with its real moment written into its own body, and when it fires the handler sees the moment has not come, puts it back on the queue for what is left, sends nothing, and writes "re-armed for 2026-09-30" so the extra wake-up explains itself. A re-arm is not a contact: nothing is counted, nothing is claimed in the ledger and the patient hears nothing. The link is also minted before the queue is touched now, and a queue that refuses is a yellow card on the doctor's board rather than a failed Confirm: the record outlives the queue.

5. **Give up gracefully, not silently.** After three nudges with no reply, the loop moves to "unreachable," the doctor gets a plain white card naming the patient and how overdue the loop is, and the loop stays open rather than closing itself: Sanad reports that it lost contact; it does not decide the loop is finished. Every step of this lifecycle has been run against the deployed service, including a task from a purged run firing after the run id moved on and being dropped without sending anything.

## Request lifecycle 4: a photograph

Every photo is read. What happens next is a two-column table and nothing else.

1. **Upright, then store.** The image is turned upright from its own EXIF tag (a phone photo taken in portrait arrives rotated), written to `gs://sanad-506914-labs/<run id>/<patient id>/<uuid>` in the private bucket, and from that point on every record carries only the path. Image bytes never enter Firestore.
2. **One model call: what is it, and what does it say.** Gemini returns a structured `PhotoReading`: a class (`lab_slip`, `bp_monitor`, `prescription`, `other`), the orientation of the printed text, and the transcription - analyte, value, unit, the slip's own printed reference range, the slip's own printed flag, or the systolic/diastolic/pulse on a monitor screen. The prompt and the schema both forbid interpretation, and nothing downstream reads one. If the model reports the text as sideways, code rotates the image and asks once more. Once, never in a loop.
3. **Routing, in code (`app/core/photos.py`).** The class the model returned, plus which loops the patient has open, decide the route. Nothing else does:

   | class | open TEST loop | open MONITOR loop | route |
   |---|---|---|---|
   | `lab_slip` | yes | - | attach to that loop, move it to pending review |
   | `lab_slip` | no | - | yellow "unexpected result" card: same values, plus two buttons (keep it on the record, or open a loop for it) |
   | `bp_monitor` | - | yes | the reading joins that loop's chart |
   | `bp_monitor` | - | no | the reading is shown to the doctor, unfiled |
   | `prescription`, `other` | - | - | stored and relayed unread |

   The TEST states that still welcome a slip include `unreachable`: a patient who stopped answering and then sends the result a week later has still sent the result.

   **Which open test takes the slip.** With one open TEST loop there is nothing to decide. With two, the slip's own analytes decide: `core/labs.panel_analytes` expands the doctor's own words for the test ("Kidney function tests") into the analytes that panel is made of, and `core/labs.panel_overlap` counts how many of the slip's rows are among them. The highest count wins, with the oldest loop taking a tie. Until S6 that count was over shared title *words*, which meant a lab that printed nothing but "K" and "Na" overlapped a kidney loop by zero; it is analyte level now. Nothing about this touches whether a value is critical; it only decides which loop the result is filed against.

   **The three verifier checks, before a slip may satisfy a contract** (`core/verify.py`, S6+ item B). The name printed on the slip has to be the name on the record, compared fuzzily in both scripts through `core/names.same_person`, so a title, a missing middle name or an Arabic spelling variant is still the same person. The collection date has to be on or after the day the doctor ordered the test. Every analyte the contract asked for has to be present. A partial result and a result collected before the order both attach and both keep the contract open: the evidence is real, it is just not yet the evidence that was asked for, and the Coordinator's `request_missing_evidence` names the missing part to the patient. An identity mismatch, or two names written in two different alphabets, never attaches at all: the values still reach the doctor, on a card that says the identity check failed, and a critical value on such a slip still escalates, because the direction the errors point is towards the doctor and never towards silence.

   **The two numbers on a monitor screen are graded too.** A photographed reading is judged by `core/vitals.py`, the same table a typed reading meets, before the doctor's card is built. That is what makes the card red rather than green when the machine reads 190/125.
4. **Comparison, in code (`app/core/labs.py`).** Every lab row is converted into the table's own unit and then judged against the critical-value table, the doctor's target for this patient, and the doctor's recorded baseline, in that order. A unit with no conversion, a number the parser could not read on a row the lab flagged, or an analyte with no table row that the lab flagged HH/LL/critical produces an amber-red "URGENT REVIEW" card instead of an ordinary yellow one. The analyte names and flags the model read back are also passed through the sentinel word list, because they are model output too. A critical value takes the identical escalation path as a Sentinel hit - the canned emergency block to the patient, a red card to the doctor - on every route, including the one with no order behind it. `decided_by` on the event names the table.
5. **The same photo from the doctor is a dictation.** A prescription photographed by the doctor goes to the Registrar, into the same `ProposedRecord` schema a voice note produces, through the same code validation, to the same confirm card. Voice, text and photo are one path with one gate at the end of it.

## Request lifecycle 5: one care obligation, over time

This is the S6 slice, and it is the difference between a reminder and an agent that owns something.

1. **The Care Contract** (`core/contract.py`). Every open loop is rendered as six things: the objective (what, for whom, by when), the evidence required (which analytes, and the three checks a slip has to pass), the permitted actions (the seven tools and nothing else), one fixed safety sentence, the deadline (the due date and the end of the doctor's window), and the escalation conditions. Nothing new is stored: a contract is the loop plus the doctor's policy, rendered. The doctor sees it on the confirm card he taps and on the patient's page afterwards, from the same function, so the two cannot drift apart.
2. **The doctor's policy** (`core/policy.py`). A small record on the doctor: how early a contact may be scheduled (not before tomorrow), how far past the due date the window runs (seven days), how many contacts one loop may ever cost (six), how many a day (one), the quiet window (22:00 to 08:00 Cairo), how many times the missing part may be asked for (twice), whether a cost barrier may be discussed with the patient at all (no), and his own pre-approved one-line reason for a follow-up. Absent means these defaults, which is what the demo runs on. `POST /admin/settings` stores one; there is no settings screen in this build and there does not need to be one.
3. **The wake-ups.** A Cloud Task firing, a patient reply the sentinel and the change gate have already passed, evidence arriving that did not satisfy the contract, and silence past a deadline (which is the same Cloud Task, with the deadline in its facts). Which obligation a text reply is about is decided in code, from the words in it: a message carrying the words a lab answers to goes to the lab loop and not to the oldest one, and a message that fits two of them equally well falls back to the oldest rather than guessing (`core/coordinator.carrying`).
4. **The choice.** One ADK agent turn, `gemini-3.5-flash` on Vertex, the same client setup the Registrar uses, with seven tools and a timeout. The tools do not act: each one calls `core/policy.check` and returns "accepted" or "refused, because", and the accepted one is carried out by code after the turn ends. Only one action per wake-up; a second call is refused with that reason.
5. **The guards, each with a test** (`app/tests/test_policy.py`). The schedule window, the seventh contact, the two evidence requests, the eight barrier classes, escalation always allowed, evidence only when there is some, closing only after the doctor's own review flag, pausing only with a barrier recorded. There is no tool for cancelling an escalation, changing a dose or editing the plan: those are not refusals, they are absences.
6. **What the patient hears.** Eight templates, in Arabic and English, in three grammatical genders, with a date, a name and an analyte as the only variable parts (`core/templates.py`, `app/tests/test_templates.py` asserts that no template carries any other field and that none of them contains a digit). Five are the tools' own; three were added by the administrative tier (the doctor was told and no substitute will be suggested, the plan sent again, and where to send a photo). The plan text a lost prescription re-sends is not a template and not generated either: it is the doctor's own confirmed words, sent again.
7. **The barrier card is a two-way door.** An escalated barrier opens a relay carrying the loop's id and the card carries the same Answer button a Concierge relay card carries. The doctor's answer goes to the patient down the one path that already existed, lands in the plan as a dated addendum, and, because the relay names the loop, takes that obligation off its barrier and puts the next contact back on the queue through the same schedule guard (`core/coordinator.resume_after_answer`). Answering the same card twice schedules one contact, because the first answer already unpaused the loop.
8. **Doing nothing is a choice too.** The model may call no tool and answer NONE, which is the right answer to a question, a greeting or anything that is not about whether this patient is doing this obligation: those are the Concierge's, and a card the doctor did not need is a real cost. A voluntary stand-down is written as `handed to concierge` (or `handed to the ladder` on a wake-up); the `fallback: ladder (model unavailable)` wording is reserved for a model that could not be used at all.
9. **The audit line.** Every choice, its stated reason, the guard that allowed or refused it, and `decided_by: model choice, guards in code`, written to the event log and printed on the card.

## The end-of-day summary

`GET /c/{token}/summary` and the `/digest` command are counted from the records in code; no model is asked and none ever will be. Every obligation the doctor has falls into exactly one of six buckets (`core/summary.classify` is a total function whose last branch is an else), so the buckets always add up to the number carried and **"lost" is zero by construction rather than by hope**. The suite drives every combination of state, barrier, pause and review flag through it and asserts that sum.

## The monitoring summary

A monitoring loop is a schedule, so "eleven readings arrived" is not the answer to what happened; which eleven is. `core/monitoring.py` counts one loop into one sentence, in the shape S6++ item H fixes:

```
Requested: BP twice daily for seven days · Expected readings: 14 · Received: 11 ·
Missing: evenings on days 3, 5 and 6 · Trend: 159/93 to 138/88 · Threshold alerts: 0 ·
Patient-reported barrier: none
```

Every number is a count over that loop's own readings. The days are counted from the day the doctor asked, in Cairo; "twice a day" is a morning and an evening split at noon; the trend is the average of the first three readings against the average of the last three, and a blood pressure is a pair, so both halves are averaged and rounded and it reads "144/89 to 138/83" (a metric with only one number, a daily weight, keeps its single number); a threshold alert is a reading `core/vitals.py` calls red, which is the same table that decides a red card, so the number here and the cards the doctor already saw come from one place. It is on `GET /c/{token}/patient/{id}` for every monitoring loop and in the completion report in place of the bare first-against-last line. No model is asked.

## The twenty background patients

`POST /admin/seed-background` puts twenty invented patients on a doctor's board, across specialties, both genders, both languages, one to three obligations each, in every state the summary can count, so the board and the end-of-day numbers carry real load while the video follows one patient (S6++ item J). They are a table in `core/background.py`: made up names, one impossible phone block, textbook diagnoses, no photographs. The seeder writes patients, loops, events and relays and nothing else, so it creates no Cloud Task and sends no message, and the document ids are derived from the doctor, so running it twice replaces the same twenty. The exact summary they produce is in `docs/RUNBOOK.md`, and a test reads that file and fails if the document and the fixture ever disagree.

## The honest model-call count

One "call" below is one request to Gemini. Everything else in Sanad is code.

| What happens | Calls | Which ones |
|---|---|---|
| A message that is nothing but a blood pressure | **0** | the table grades it in code |
| A patient message the code sentinel catches | **0** | no generation happens at all |
| A patient message the code change-gate catches | **1** | the sentinel's model net |
| A patient message an administrative pattern matches | **2** | sentinel net, change vote; the tier itself is code, and it answers instead of the Coordinator and instead of the Concierge |
| A patient reply the Coordinator acts on | **4** | sentinel net, change vote, administrative vote (which names nothing), Coordinator |
| An ordinary patient question | **up to 6** | sentinel net, change vote, administrative vote, Coordinator (which stands down), Concierge, reassurance vote |
| The same as a voice note | **+1** | the transcription |
| A photographed lab slip, no caption | **1 or 2** | the read, plus one more read only if the text came back sideways |
| The same slip when it does not satisfy the contract | **+1** | the Coordinator, to ask for the missing part |
| A scheduled wake-up | **1** | the Coordinator, and 0 when it is switched off |
| A doctor dictation, typed | **2** | the Registrar extraction, as an ADK agent turn, then the identification read of which patient it is about |
| The same on a board with no patients on it | **1** | there is nobody to identify against, so no second call is made |
| A photographed prescription | **2** | the same model and the same schema, called directly (see the caveat under Positioning), then the identification |
| The same dictated by voice | **3** | transcription, then the Registrar, then the identification |
| A completion report | **1** | the one "what I would flag" line |

A dictation cost one before S9 and costs two now: the second is the identification, and it fails closed to the code name matcher and a card that asks, so a model that is wrong or unreachable costs the doctor one tap and can never attach a dictation to the wrong record. Before S6 the ordinary question cost up to four and a wake-up cost none. The Coordinator is one call added to each and the administrative vote is the other, and every one of them fails closed: an error or a timeout costs the doctor a card he did not strictly need, never a gate a patient should have met. The administrative tier is the only one that can also take a call away: a message its pattern list matches is answered from a template, so it costs two calls where the same message used to cost five.

## The demo run id and the compressed clock

Two settings live in Firestore and can be changed at run time through `POST /admin/settings`, so a rehearsal never needs a redeploy between takes (`app/core/settings.py`).

- **`run_id`.** Every task created is stamped with the run id current at creation. The handler drops a task whose stamp is not the current one, without sending anything, and logs why. Bumping the run id is therefore an instant kill switch for everything already queued, and unlike purging the queue it does not depend on eventual consistency.
- **`time_scale`.** How many real seconds make one Sanad "day". At 86400 a day is a day and the rules are the real ones; at 3, the whole three-nudge ladder plus the unreachable card plays out inside a minute, with no change to the logic - only to the length of a day (`app/core/timing.py`). Quiet hours are enforced only at real scale, because a compressed day has no wall clock to be quiet in, and the code says so rather than pretending otherwise.

## The console, and the patient's page

Both are plain HTML served by the same service. No build step, no framework, nothing to install.

- **`/c/<web token>`** is the doctor's console and the judge's whole demo surface: a header naming the service, region, project, revision, run id, time scale and chaser engine, all read from the container's own environment; four counts over the board (`app/core/board.py`) and the one line they add up to, "3 need you, 17 handled"; the board, where a patient's name opens the per-patient view (record, loops with their results and readings, and that patient's whole timeline); the append-only event feed, where every card carries its audit line - tier, which nets ran, the validator's verdict, which tool the Coordinator chose and the guard that allowed or refused it, `decided_by`; a Demo panel whose six buttons fill the doctor or patient box with the exact demo text and send nothing; a Reset board button behind the admin secret; and the QR of the latest committed patient, inline, so a phone can scan it off the screen.
- **`/c/<web token>/app`** is the same board, drawn to the design system in `design-system/MASTER.md`: board, patients, inbox, reports and settings, behind the same `current_doctor` dependency as the console above, so a wrong token is the same 404. It reads four routes the plain console does not. `/c/<token>/cards` returns only the cards that still need the doctor: pressing a button writes `meta.card.resolved` back onto the card event (`core/cards.py`), so a page reload no longer resurrects a card the doctor already finished, and the feed still returns everything because the feed is the history. One button is exempt and only one, "Send a note": it is a side message to the patient, the card behind it is a result still waiting for the doctor's review, and the Inbox must not hide a result he has not reviewed. `/c/<token>/reports` returns completion reports and digests as records, stored the moment each one is written, rather than by matching the first line of an event against its heading. `/c/<token>/settings` returns the doctor's own record and his Coordinator policy, read only, with whether a Telegram chat is bound but never the chat id itself. And `/board` now carries `next_due`, `last_event_ms`, `last_event_kind`, `channel` and `link` per patient, all computed from the records in `core/views.py`: the page used to derive the last event from the newest two hundred feed events and the channel from whether the bot was configured at all, which is a fact about the deployment and not about the patient. The plain console at `/c/<token>` is unchanged.
- **`/c/<web token>/summary`** is the end of the day as a JSON record and a card: the fixed sentence with the day's numbers in it, counted from the loops, the events and the open questions, with `lost` in the payload so it can be read rather than believed.
- **`/p/<link token>`** is the patient's own page: the same one-time link, opened in a browser instead of Telegram, with text, a voice note and a photo. Same patient record, same brain, same gates. It does not burn the link token, so opening it never costs the patient the Telegram binding.

## Failure-tolerance story

- **Idempotency key.** Every scheduled send is keyed on `(loop_id, attempt_number)` and checked in Firestore before the message goes out, so Cloud Tasks' at-least-once delivery guarantee can never turn into a patient receiving the same nudge twice.
- **Demo run id.** Every task enqueued during a rehearsal is stamped with the current run id. A handler that fires with an older run id is dropped without sending anything. This exists specifically so that purging the queue and starting a fresh take for the recorded video can never have a stray task from an earlier rehearsal land on a phone mid-recording.
- **Stale task drop.** The same mechanism doubles as ordinary operational hygiene outside the demo: if a queue backs up or a task is delayed past its usefulness, the handler's guard checks (run id, idempotency, quiet hours) mean a late-firing task degrades to a no-op rather than an out-of-context message.
- **A deadline on everything a patient waits for.** Every dependency on the patient's lane runs inside `core/bounded.within`: the triage vote, the two output votes, the Concierge reply, the voice transcription, the photo read and the bucket write, with one table of deadlines in `app/core/bounded.py`. A dependency that hangs is indistinguishable from one that is down, and neither may become an HTTP 500 on a patient's phone. Each call site fails closed in its own way: the triage gate and the Concierge relay to the doctor, an unreadable photo takes the "stored and relayed unread" exit that already existed, an unreadable voice note asks the patient to send it again and puts a card in front of the doctor, and a bucket that does not answer costs the photograph and never the values on it.
- **The doctor knows before the patient is told he knows.** "Your doctor has just been alerted" is written last on every escalating path, after the escalation event, the relay and the doctor's card exist. One function, `core/escalate.told_or_fail_closed`, owns that ordering and the fallback that goes with it, so no branch can be given the promise without the sentence to say when the write fails. The fail-closed emergency block keeps the instruction to go to hospital and drops only the claim about the doctor.
- **A message that never left costs nothing.** The Chaser writes the loop state, the counters and the audit event before the send, so a delivery that dies mid-flight can never leave a delivered message uncounted. The contact against the doctor's six-per-loop policy limit is the one thing handed back when the delivery explicitly fails (`store.refund_contact`), and `chaser.resend` counts it again when the retry gets through, so the number of contacts on a loop stays the number of messages that reached the wire.
- **One fix attempt, then stop and document.** This is the project's own build discipline, written into every slice's acceptance criteria before the slice was started. It shows up as real evidence, not just a rule on paper. S1 verification alone found three defects: a feed pagination bug from comparing a truncated millisecond timestamp against Firestore's full-precision one, an `http://` URL returned because Cloud Run terminates TLS before the app sees the request, and a scrambled loop display order from one commit's loops sharing a timestamp. Each was fixed once, verified once, and written down, rather than iterated on silently. The same discipline governs the Cloud Tasks identity setup: if it fights for more than 30 minutes, the documented fallback is an in-process scheduler behind the same handler interface, which is why `core/tasks.py` carries one and `/health` reports which engine is actually running.

## Data model (Firestore)

- `doctors/{doctor_id}`: name, specialty, language, `web_token` (the console URL's only auth in this build), created_at, and since S6 `policy`: the Care Coordinator's window, contact limits, quiet hours, cost rule and pre-approved follow-up line. An absent or unreadable policy is the defaults in `core/policy.py`, never a guard that does not guard.
- `patients/{patient_id}`: doctor_id, name, phone, age, sex, diagnosis, baseline, targets, `plan_text` (the single source Concierge may quote), channel bindings, status.
- `loops/{loop_id}`: patient_id, type (TEST/MONITOR/MEDICATION/VISIT/TASK), title, details, state (open → waiting_patient → received/pending_review → done, or → unreachable), due_at, attempts. Since S6 also: `contacts` and `contact_days` (every message this loop has ever cost the patient, which never resets, unlike `attempts`), `evidence_requests`, `barrier` and `barrier_note`, `reluctance`, `paused`, `doctor_reviewed` (the two-state gate's own flag, set only by the doctor's tap), and `verified` (what the three verifier checks said about the evidence).
- `events/{event_id}` (append-only): patient_id (nullable), doctor_id, loop_id (nullable), kind (doctor_in/patient_in/agent_out/card/system/escalation), channel, text, media references, meta (which sentinel net fired, the validator verdict, the tier used), timestamp. This is the log judges can read as an audit trail. One field is written after the fact and only one: `meta.card.resolved`, set when the doctor presses the button on that card. It is added, never a change to what was already there, so text, kind, timestamp and media stay write-once.
- `pending_confirms/{confirm_id}`: a proposed record awaiting the doctor's tap.
- `sends/{loop_id}:{kind}:{attempt}`: the Chaser's idempotency ledger, one row per nudge actually sent. The document id *is* the key, and the row is created (not set) before anything goes out, so a Cloud Tasks retry finds it and sends nothing.
- `link_tokens/{token}`: a patient's one-time deep link, burnt on first use.
- `relays/{relay_id}`: a patient question the Concierge refused to answer, waiting on the doctor.
- `reports/{report_id}`: one completion report or one digest, with its kind, patient, title, body and time, written at the moment the report is produced (`core/report.record`). It exists so the Reports screen reads records instead of matching generated text.
- `contacts/{patient_id}:{day_index}`: the patient-wide contact ledger, one document per patient per Cairo day, holding the count, the kinds and the loops. It is what makes "one message a day" a promise about the patient rather than about one loop: before it existed, the Chaser counted `sends` rows, the Coordinator counted one loop's own `contact_days`, and the doctor's pre-approved reluctance line was counted nowhere, so a patient with two open loops could hear from Sanad three times in a day with every guard satisfied. **Roadmap note:** it is the one collection that grows with time rather than with records, and nothing sweeps it. At demo volumes that is a document per patient per day and it is cleared by `/admin/reset`; a real deployment wants a TTL policy on it.
- `settings/demo`: the run id and the time scale, read per request, never cached.
- Loops additionally carry `attempts`, `last_attempt_at`, `last_reply_at`, `results` (what the extractor read, already judged) and `readings` (a monitoring chart). Patients additionally carry `results`: lab values that arrived with no order behind them and that the doctor chose to keep on the record.

Two implementation notes carried over from S1, both documented in the code itself: queries are equality-only with ordering done in Python, because the natural feed query needs a Firestore composite index that this build avoids building (correct at demo volume; the first thing to change if a doctor's history grows large); and `baseline`/`targets`/loop `details` are flattened to name/value pairs or optional fields rather than a free-form dict, because Vertex's structured output rejects an object schema with no declared properties.

## Health, and the path that is not there

`GET /health` is the liveness endpoint and reports what the container actually is: service, region, project, revision, model, whether Telegram is configured, which Chaser engine is live, whether the labs bucket is set, the run id and the time scale. **It is `/health`, never `/healthz`.** Cloud Run's Google Frontend swallows the exact literal path `/healthz` before it reaches the container, returning a Google-branded 404 that looks exactly like a broken app; `/healthz` is still registered in `app/main.py` beside `/health` as a permanent note about the trap.

## Region and pinning

Everything lives in `europe-west1`: Cloud Run, Firestore, Cloud Tasks. The model is pinned to `gemini-3.5-flash` over Vertex AI's `global` endpoint. ADK is pinned to `2.8.0`. Deploys go through the hand-written `Dockerfile` and `gcloud run deploy --source .` (Cloud Build), never `adk deploy`.
