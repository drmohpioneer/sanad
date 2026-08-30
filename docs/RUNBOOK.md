# Sanad - runbook

Everything you do before a rehearsal, before the final take, and if a beat fails
while the camera is running. Nothing here needs a redeploy.

## 0. The two lines you paste first

```bash
PROJECT="$(gcloud config get-value project)"
test -n "$PROJECT" && test "$PROJECT" != "(unset)"
U=https://<SERVICE_URL>
S=$(gcloud secrets versions access latest --secret=sanad-admin-secret --project="$PROJECT")
```

`<SERVICE_URL>` is what `deploy.sh` prints at the end, and what `gcloud run
services describe sanad --region europe-west1 --format='value(status.url)'`
answers. It is not written down here: the repository is public, and a public
Gemini-backed endpoint written in a runbook is the first thing a scraper finds
(security audit L2).

`S` never gets echoed. Everything below uses `$S`, never the value.

**The admin secret goes in a header, never in a URL.** Every `/admin` call below
carries `-H "X-Sanad-Admin: $S"`, and a request with `?secret=` in the query
string is refused with 401 (security audit H1). The reason is Cloud Logging:
Cloud Run's request log records the full path of every request and keeps it for
thirty days, so a secret in a query string is a secret in a log that anyone with
`roles/logging.viewer` can read.

The console URL is `$U/c/<web token>`, and
`curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed"` hands it back at any
time (it does not create a second doctor: the seed is idempotent).

### Never test on the doctor whose phone is bound

Every card Sanad produces fans out to whatever Telegram chat the doctor has
bound, so a test run against the demo doctor puts its cards on his real phone.
Seed a second doctor instead and use that console token for anything automated:

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed?name=Test%20Doctor"
```

That doctor has no Telegram chat bound, so every send to him is a silent no-op
and everything he does is visible only in his own console.
`curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/reset?name=Test%20Doctor"`
clears that board and leaves the demo board untouched.

That reset is also safe to run while a phone is being bound. It clears the
pending `/start` rows only for the chat that is already this doctor's, and none
at all for a doctor with no chat bound, so resetting a test board can no longer
wipe the `/start` you are in the middle of claiming on the demo board.

### Never tap a patient's own deep link on the doctor's phone

Mohamed did this on 2026-08-29: he forwarded a patient link and then opened it
himself to see what the patient would see. The bot bound his chat as that
patient, spent the one-time token, sent him the patient welcome, and from then
on every message he typed reached Sanad as that patient's words. The link he
had forwarded was already used, so the real patient could never bind at all.

`core/tg_router._start` refuses that now. A chat that belongs to a doctor
record gets one line back, the token is not consumed, nothing is bound, and the
tap is written to that doctor's own board.

**If a board already carries one** (a binding made before this shipped):

1. Look for it. The check runs by itself at container start and after every
   reset, and it says so on the doctor's board and in the log. To ask for it
   directly, reset the board and read the answer:

   ```bash
   curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/reset?name=Test%20Doctor" \
     | jq .doctor_chats_bound_as_patients
   ```

   An empty list is a healthy board. A row names the patient record and the
   chat id that is wrongly on it.

2. Clear it. There is no separate repair command and there does not need to be:
   the wrong binding lives on a patient record, and `POST /admin/reset` deletes
   that board's patients. Reset the board and re-seed it, which is the step
   section 1 already runs before every take.

Run both against the Test Doctor board unless the demo board is the one that is
wrong. Read the note above about which board a reset touches.

---

## 1. Before every take, in this order

The order matters. Bumping the run id is the step that actually guarantees
silence, because it does not depend on the purge having propagated.

```bash
# 1. Purge the queue. Asynchronous: `gcloud tasks list` can lag a minute behind
#    it, which is why it is not the kill switch, only tidying.
gcloud tasks queues purge sanad-chase --location europe-west1 \
  --project "$PROJECT" --quiet

# 2. Bump the run id. Every task already on the queue carries the OLD id and is
#    dropped, unsent, the moment it fires. Use a new value every take: demo2,
#    demo3, take7 - anything, as long as it changes.
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/settings?run_id=take1"

# 3. Reset the board. Patients, loops, events, pending confirms, link tokens,
#    relays and the send ledger go. Reset clears policy: the doctor's stored
#    Coordinator policy is emptied too, so the board comes back on the defaults
#    in core/policy.py and a max_contacts you set for one take cannot survive
#    into the next one. The doctor record and the console URL survive, so the
#    link in your browser keeps working. Add &name=Test%20Doctor to clear a test
#    board instead of the demo one.
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/reset"

# 4. Set the clock. 86400 = real time (one day is one day). A rehearsal that
#    has to show the three-nudge ladder inside a minute uses 3.
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/settings?time_scale=86400"

# 5. Seed the demo patient: paste beat 1 from the Demo panel in the console and
#    tap Confirm. Nothing about this step is scripted on purpose - the judge
#    watches you dictate.

# 6. Check what the service believes about itself.
curl -s "$U/health"
```

`/health` must show, before you start recording:

```json
{"ok": true, "service": "sanad", "region": "europe-west1",
 "chaser": "cloudtasks", "telegram": true, "labs_bucket": true,
 "run_id": "take1", "time_scale": 86400, "revision": "sanad-000NN-xxx"}
```

- `chaser: cloudtasks` - the product engine, not the fallback.
- `telegram: true` - the bot token is mounted; the phones will get their cards.
- `labs_bucket: true` - lab photos have somewhere private to go.
- `run_id` - the value you just set in step 2.

The same line is printed across the top of the console, so the GCP proof is in
frame for the whole recording without switching tabs.

What is still queued, if you want to see it:

```bash
gcloud tasks list --queue sanad-chase --location europe-west1 \
  --project "$PROJECT" \
  --format='table(name.basename(),scheduleTime,dispatchCount)'
```

### The twenty background patients

The board behind the demo patient should look like a clinic, not like a demo.
One command puts twenty more patients on it, with thirty one care obligations
between them, in every state the system has:

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed-background?name=Test%20Doctor"
```

Drop `&name=Test%20Doctor` to seed your own board instead. Run it after step 3
(the reset) and before step 5 (the dictation), so the board is the twenty plus
whoever you dictate on camera.

All twenty are invented: made up names, conspicuously formatted demo phone
values (`0100 000 00NN`), textbook diagnoses, and no photographs. The number
pattern is not an officially reserved test range, so never dial it. The rows are a
table in `app/core/background.py` and nothing about them came from a real
person. The seeder writes patients, loops, events and relays and nothing else:
it creates no Cloud Task and sends no message, so it can never reach a phone.
The document ids are derived from the doctor, so running it twice replaces the
same twenty rather than making forty.

On a board that has been reset and carries nobody else, `GET
$U/c/<token>/summary` then reads, word for word:

```
Today Sanad carried 31 care obligations · 3 completed with evidence ·
17 progressing normally · 6 patients needed logistical help ·
1 patients could not be reached · 1 treatment questions need you ·
2 critical results escalated · Doctor attention required: 11 cases
```

and the counts behind it:

```json
{"carried": 31, "completed_with_evidence": 3, "progressing": 17,
 "needed_help": 6, "unreachable": 1, "questions": 1, "criticals": 2,
 "attention": 11, "closed_without_evidence": 1, "lost": 0, "duplicates": 0}
```

Those numbers are computed from the same table the seeder writes, and
`app/tests/test_background.py` reads this file and fails if the two ever
disagree. Two things move them: dictating a patient on camera adds his own
obligations to every count, and the two critical results are counted for the day
they were seeded, so a board seeded yesterday and read today shows
`2 critical results escalated` as `0`. Seed and film on the same day.

### Reset from the console instead

The "Reset board" button in the Demo panel does step 3 alone, behind a prompt
for the same admin secret. It is there for the take where you notice a stray
patient thirty seconds before you start. It does not purge the queue and it does
not bump the run id: for a clean take, run all six steps.

---

## 1b. Rehearsal order for spine v3

`specs/video-spine-v3.md` has the full beat timing and narration. This section
is the order to run the sections above in for that spine, plus the exact
patient phrases and slips each beat needs, so nothing is re-typed from memory
on the day. It does not repeat what the sections below already say; it points
at them.

1. Section 0: paste the two lines.
2. Section 1, steps 1 to 4: purge the queue, bump the run id to a value you
   have not used yet, reset the board (Test Doctor for a rehearsal, the demo
   doctor for the real take), and set the clock per item 5 (3 before beat 1's
   Confirm, back to 86400 right after beat 2).
3. Section 1, "The twenty background patients": seed them, so the board
   carries real load and Amany Roushdy's Glucose tolerance test loop sits at
   six contacts for beat 7.
4. Section 1, item 16 check: if you set a policy value for testing, confirm
   `POST /admin/reset` cleared it back to `core/policy.py`'s defaults before
   you record, from `GET /c/<token>/settings`.
5. Beat 2: set `time_scale=3` before Confirm. Measured live on revision 25:
   the first blood-pressure reminder is on screen about 6 seconds after
   Confirm and the six of them run to about 22 seconds; the lipid ladder's
   first rung lands about 39 seconds after Confirm and is the one to point at;
   its unreachable card follows at about 55 seconds and the visit reminders
   run from about 61 to about 76 seconds.
   Let it go unanswered and show the feed line for it, the real ladder rung.
   Never `/force_due` this beat on camera. Set `time_scale=86400` again right
   after beat 2, before beat 3.

**Beat 1, the exact dictation:**

> Ahmed Ali, 58, male, heart failure and high LDL. Start atorvastatin 40 at
> night. Lipid panel in 2 weeks. Blood pressure twice a day for 7 days. Come
> back in 3 weeks.

"3 weeks," not the canonical seed's "a month": the 720-hour hop is proven live,
but 3 weeks is the rehearsed path and there is no reason to spend the buffer.
Confirm, then scan the QR with the patient phone. Before the patient has written,
the welcome defaults to English. Once the patient writes, later proactive text
uses the language of the latest patient message.

**Beat 1 is a new patient, and the card now says so.** Since S9 the Registrar
matches the dictated name against the board before it builds the confirm card,
so the title reads "New patient: Ahmed Ali" on a board that has no Ahmed on it.
Run the reset in section 2 before the take: an Ahmed left over from a rehearsal
turns beat 1 into "Which patient is this?", which is the right behaviour and the
wrong shot. If you want the existing-patient card on camera, dictate beat 1,
confirm it, and then say "follow up with Ahmed about his potassium in a week":
that one attaches to the record beat 1 made and adds a dated addendum to his
plan.

**Beat 3, the cost barrier, patient phrase and the doctor's typed reply:**
patient sends "I'm not doing the test, it's too expensive." (voice note or
typed). When the barrier card lands on the doctor's phone, answer it with:
"The hospital lab is free, go there." Rehearse that exact sentence, since it
is what the narration in `specs/video-spine-v3.md` describes the doctor
saying.

**Beat 4, the incomplete-evidence slip:** use the pair dated the day of the
take. `docs/seed/lab-slip-7-lipid-partial-0830.png` is collected 30/08/2026,
for the Sunday rehearsal; `lab-slip-7-lipid-partial-0831.png` is collected
31/08/2026, for the Monday take, and the unsuffixed
`lab-slip-7-lipid-partial.png` is the same file as the 31/08 pair. Using a
slip dated earlier than the order date is what makes the verifier correctly
report `date before_order`, so match the file to the day you are filming on.
Every version prints LDL 160 H and Total Cholesterol 240 H, with HDL and
Triglycerides absent. On a date-valid take, the card carries:

```
verified: identity match, date ok, 2 of 4 requested analytes present
missing: Triglycerides, HDL
```

`satisfies` is false, the values still attach and the loop stays open. The card
and the patient request name both missing analytes. LDL 160 follows the printed
`H` flag and `<100` reference, so it is not labelled in range.

**Beat 5, the complete slip:** same rule as beat 4, use the pair dated the day
of the take. `docs/seed/lab-slip-8-lipid-complete-0830.png` is collected
30/08/2026, for the Sunday rehearsal; `lab-slip-8-lipid-complete-0831.png` is
collected 31/08/2026, for the Monday take, and the unsuffixed
`lab-slip-8-lipid-complete.png` is the same file as the 31/08 pair. It prints
all four lipid analytes: LDL 92, HDL 48, Total Cholesterol 178 and
Triglycerides 130. On a date-valid take its card line is:

```
verified: identity match, date ok, 4 of 4 requested analytes present
```

`satisfies` is true and the loop moves to `pending_review`. Confirm that off
camera, then cut to the card already rendered, per the spine's own instruction
not to show a second spinner on camera.

**Beat 6, the critical value:** `docs/seed/lab-slip-2.png` prints potassium 6.4,
flagged H. It escalates whether or not a matching loop is open. After beat 5 the
lipid loop is pending review, so this appears as an unordered critical result
with Attach/Open-a-loop actions. Narrate that; do not say it attached to the
lipid contract.

**Beat 7, the refusal:** from Amany Roushdy's patient box, send exactly:

```
I did the glucose test
```

The code administrative pattern attempts to schedule the next evidence contact.
Her glucose loop already has six contacts, so the feed's audit line is:

```
refused by code (core/policy.py): 6 contacts already on this loop and the
policy limit is 6
```

This is a natural patient interaction and does not depend on the filming hour.

**Beat 9, the end-of-day sentence:** read `GET /c/<token>/summary` on screen.
If the board carries only the background twenty plus Ahmed Ali, the sentence
is the one already recorded above under "The twenty background patients," with
whatever Ahmed Ali did in beats 1 through 8 added to each count.

Sections 2 (the two phones), 3 (if a beat fails), and 5 (after the final take)
apply. Section 3b is an off-camera diagnostic only; section 4 is an optional
separate ladder rehearsal, not part of the filmed spine.

---

## 2. The two phones

- Doctor's phone: `@SanadHealthBot`, already bound. If it ever stops receiving
  cards, bind it again by NAMING the chat:

  ```bash
  curl -s -H "X-Sanad-Admin: $S" "$U/admin/pending-starts"          # read the id
  curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/bind-doctor?chat_id=<id>"
  ```

  `chat_id` is required (security audit M3). It used to default to "the newest
  chat that sent /start", and the bot's username is public: anyone who sent
  `/start` in the seconds between his `/start` and this call became the doctor's
  phone, and every card, every patient message and every lab result went there.
  `?chat_id=0` unbinds a wrong one.
- Patient's phone: after Confirm, the commit card carries a `t.me/...?start=...`
  link and the console shows its QR inline, next to the board. Scan the QR off
  the laptop screen with the patient phone. One tap binds that chat to that
  patient for good, and the link cannot bind a second phone.
- Both phones: Do Not Disturb on, notifications for Telegram only, brightness up.
- A reset burns the patient's binding along with the patient. Re-scan the new QR
  after every reset. The doctor's binding survives a reset, and so does a
  pending `/start` from any chat that is not already this doctor's.

---

## 3. If a beat fails while recording

Take them in this order. Every one of these is a step down, not a rebuild.

1. **A beat is slow, not broken.** Say what is happening and keep going. A lab
   photo takes 15 to 25 seconds end to end: upload, EXIF pass, one Gemini read,
   the comparison in code, two cards. That is worth narrating, not cutting.
2. **The same slip lands twice.** The exact same image sent again on the same
   board on the same day is refused as a duplicate: "I already received this
   image today." No second card, result or model call. Recover with the other
   dated pair (beats 4 and 5, section 1b) or a board reset.
3. **A nudge does not arrive on the patient phone.** Use the console: the same
   card is in the feed, because every reply fans out to both. If the phone is
   the problem, the demo is not.
4. **Telegram is down or the phone will not bind.** Run the whole demo in the
   browser. The console's Patient box and the patient page (`/p/<link token>`,
   linked under the QR) are the same brain through the same gates. Say so on
   camera: the adapter is the only thing that changed.
5. **Cloud Tasks refuses to enqueue.** Redeploy with the in-process engine:
   `CHASER_ENGINE=inprocess bash deploy.sh`. Same `enqueue()`, same handler,
   same nudges; it forgets pending nudges on a restart, which is why it is the
   fallback and not the default. `/health` will then say `chaser: inprocess`,
   so do not claim Cloud Tasks on camera while it says that.
6. **Vertex or the model is failing.** The Sentinel's code net, the critical-lab
   table, the Chaser and the whole board still work with no model at all. Run
   beats 1, 4 and 6 (dictate is the only one that needs the model). Say what is
   degraded.
7. **Anything else.** Stop and use the previous unedited rehearsal recording as
   the submitted video, then write down what failed. A calm recorded run beats a
   live stumble, and that was decided before the first take, not in the moment.

---

## 3b. Showing a guard refuse, on camera (legacy diagnostic only)

Do **not** use this procedure in the submitted video. Beat 7 above is the
natural patient interaction and is the only refusal path in the filmed spine.
This section remains as an off-camera operator diagnostic for the same handler.

The load-bearing claim is "the model chooses and code decides". On rev 15 no
guard had been observed refusing anything outside a unit test. The number this
diagnostic refuses on is on the record before you start.

One of the twenty background patients, **Amany Roushdy**, carries a Glucose
tolerance test that has already cost six contacts, which is the default policy
ceiling. So:

```bash
# seed the twenty first (section 1), then, in the doctor's box or on Telegram:
/force_due Amany glucose strict
```

`strict` is the whole trick and it is one word. `/force_due` on its own marks
the task `force`, which is what lets a doctor push a nudge past quiet hours and
past his own contact window: the doctor asking for it now is the doctor's call.
`strict` drops that mark, so the task arrives as an ordinary scheduled wake-up
and every guard applies to it. `core/policy.check` then refuses it and the feed
writes:

```
refused by code (core/policy.py): 6 contacts already on this loop and the
policy limit is 6
```

At real scale inside Cairo quiet hours, policy is evaluated before an allowed
task is deferred to 09:00, and the deferral is written to the feed. The natural
Beat 7 patient pattern remains the submitted-video path.

Two things worth knowing before you run it:

- No message is sent, and none should be. The refusal is the whole event.
- The same red chip appears in front of any Coordinator turn where a guard
  refused a call before the one it accepted, so a live turn that asks for today
  and is told "not before tomorrow" shows REFUSED and then the accepted retry,
  in that order, which is the two-line version of the whole architecture.

---

## 4. The compressed clock, for beat 2 and for the full ladder

Beat 2 in the filmed spine runs on this clock, and the scale has to be set
**before Confirm**, because a task's delay is fixed at the moment it is
created:

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/settings?time_scale=3"   # a day is 3 seconds
# dictate Ahmed Ali, tap Confirm: measured live on revision 25, the six
# blood-pressure reminders land about 6, 9, 12, 16, 19 and 22 seconds after
# Confirm; the lipid ladder's first rung lands about 39 seconds after Confirm,
# the one to point at. Let it go unanswered, show the feed line for it. Its
# unreachable card follows at about 55 seconds, and the visit reminders run
# from about 61 to about 76 seconds.
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/settings?time_scale=86400"  # back to real time, before beat 3
```

Never `/force_due` either reminder on camera: the tasks waking on their own,
unforced, is the whole point of beat 2. Changing the scale after Confirm does
not touch already-created Cloud Tasks, which is why the order above is fixed.
The full Beat 1 dictation creates twelve tasks total across its four loops
(three lipid, six blood pressure, three visit); the blood-pressure reminders
arrive first, then the lipid rung at about 39 seconds, so this window is not a
clean lipid-only ladder.

To rehearse all three nudges plus the unreachable card separately from the
video, off camera, run the same two commands around a dictation with a dated
TEST loop and watch the feed to the end of the ladder.

### A loop dated more than a month out

Cloud Tasks holds no schedule more than 720 hours (30 days) ahead, so a contact
further away than that is carried in hops: the task is scheduled for 28 days,
it fires early, sends nothing, and puts itself back on the queue for what is
left, writing "re-armed for 2026-09-30" in the feed. Nothing to do about it and
nothing to say on camera; it is here so that an extra wake-up in the feed is not
a surprise. "Come back in a month" is safe to dictate again: before this it made
Confirm return 500 with no patient link at all.

Quiet hours (22:00-09:00 Cairo) are enforced only at real time scale: a
compressed day has no wall clock to be quiet in, and `core/timing.py` says so.
That means a compressed rehearsal will never be blocked by the hour, and a real
run at midnight will be.

---

## 5. After the final take

```bash
gcloud tasks queues purge sanad-chase --location europe-west1 \
  --project "$PROJECT" --quiet
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/settings?run_id=post-demo"
```

### Rotate the console token BEFORE you upload the video

Do this while the file is still exporting, not after it is public.

The console URL is a bearer credential: whoever reads it off the screen can
dictate, confirm records and answer cards on that board. The rules require a
live `.run` URL visible in the address bar and the demo is the console, so the
token is legible in the recording, and the recording stays public for the weeks
between the deadline and the result.

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/rotate-token"
```

It answers with a new `console_url`. Open that one; the one in the video is now
a 404. Nothing else changes: same doctor, same patients, same feed, new key. If
you re-record anything afterwards, rotate again afterwards, because the new URL
is now the one on camera.

Two mistakes to avoid here:

- Rotating before the last take. The URL on screen has to be a working one, so
  rotation is the step after the final export and before the upload.
- Assuming a reset does it. `POST /admin/reset` deliberately keeps the token so
  the link in your browser survives a rehearsal. Only `rotate-token` changes it.

### Kill the patient links too, if a patient page or a QR was on camera

A patient link opens one person's whole record with no second factor at all, so
it is worth more than the console URL, not less. `revoke_links=true` kills every
one of them on the board:

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" \
  "$U/admin/rotate-token?revoke_links=true"
```

The answer says how many were revoked. Nothing is lost: the records stay, and
each patient is given a fresh link the next time the doctor confirms anything
about him.

A link also dies on its own. Every patient link expires 30 days after it is
minted, and an expired one is a 404 on `$U/p/<token>` exactly like a revoked one
(codex item 14). A patient whose link ran out gets a new one at the doctor's
next confirm.

### Rotate the admin secret

Do this whenever the value has been seen: pasted into a transcript, read off a
shared screen, or written to a log by an old client that still put it in a query
string. It was surfaced once already, in the 2026-08-29 security audit, so it
has been rotated at least once.

Three steps, in this order. The old version keeps working until the new revision
is serving, so there is no window where `/admin` is unreachable.

```bash
# 1. Add a new version of the secret. Nothing reads it yet.
openssl rand -hex 24 | tr -d '\n' \
  | gcloud secrets versions add sanad-admin-secret \
      --project "$PROJECT" --data-file=-

# 2. Redeploy, so the running revision picks the new version up. The service
#    mounts `latest`, so this is what makes the change live.
bash app/deploy.sh

# 3. Re-read it into the shell for everything below.
S=$(gcloud secrets versions access latest --secret=sanad-admin-secret \
      --project="$PROJECT")
```

Then rotate every console token, because a doctor's token is a bearer credential
in its own right and the same audit surfaced one of those too:

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/rotate-token"
curl -s -X POST -H "X-Sanad-Admin: $S" \
  "$U/admin/rotate-token?name=Test%20Doctor"
```

Disable the old secret version last, once the new one has been used against the
live service at least once:

```bash
gcloud secrets versions list sanad-admin-secret --project "$PROJECT"
gcloud secrets versions disable <OLD_VERSION> --secret=sanad-admin-secret \
  --project "$PROJECT"
```

### The judges get their own board

Seed a doctor for them and point every judge-facing URL at that board, never at
the demo board (security audit M5):

```bash
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed?name=Judge%20Doctor"
curl -s -X POST -H "X-Sanad-Admin: $S" \
  "$U/admin/seed-background?name=Judge%20Doctor"
```

`seed` answers with that board's `console_url`. That URL is the one that goes in
DEVPOST.md and README.md. The Judge Doctor has no Telegram chat bound, so
nothing it does can reach anybody's phone, and the twenty background patients
give the board the shape a judge expects to see. Dictate the demo patient onto
it as well, so beat 1's record is already there.

If the URL scrolls away, `seed` again with the same name: it creates nothing and
hands back the current URL.

### If you lose the console URL after rotating

Nothing is lost and nothing has to be rotated again. `POST /admin/seed` on a
doctor who already exists creates nothing and hands back that doctor's CURRENT
console URL, which after a rotation is the new one:

```bash
# the demo board
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed"
# the Test Doctor board, which is the one every automated run uses
curl -s -X POST -H "X-Sanad-Admin: $S" "$U/admin/seed?name=Test%20Doctor"
```

The answer carries `"created": false` and the live `console_url`. That is the
recovery line for the case that actually happens: the rotate answer scrolled
away, or the take was on the Test Doctor board and the new URL was never written
down anywhere. Rotating a second time to "get a URL back" would only invalidate
the one that is already open in another window.

### If you restarted the service by pinning traffic

The quickest way to restart the service mid rehearsal is to send its traffic to
a named revision:

```bash
gcloud run services update-traffic sanad --to-revisions sanad-000NN-xxx=100 \
  --region europe-west1 --project "$PROJECT"
```

That works, and it has one consequence that is invisible until it costs a
deploy: the service's traffic block now names a revision instead of carrying
`latestRevision: true`, and the pin survives every later deploy. On rev 17 this
made `bash deploy.sh` exit 0, print "is serving 100 percent of traffic", and
serve the OLD revision, because the new one was built, imported and then retired
for having no traffic allocation.

**Traffic is pinned after this. Run `update-traffic --to-latest` before the next
deploy:**

```bash
gcloud run services update-traffic sanad --to-latest \
  --region europe-west1 --project "$PROJECT"
```

`app/deploy.sh` now runs that itself as its step 9 and then reads
`GET /health` back, so a deploy that does not end up serving the revision it
just built exits non-zero and says both revision names. The line above is for
the case where you pinned traffic and are not deploying again yet: until it is
run, the service is on the pinned revision whatever anyone else pushes.

### Set the budget alert BEFORE submission

Not after. The patient path costs up to six Gemini calls per message, the
service is public by necessity, and the whole point of the paragraph above is
that a credential was on camera. A budget alert is the thing that tells you if
that ever mattered.

Console: Billing, then Budgets and alerts, then Create budget. Scope it to
project `$PROJECT`, set a monthly amount you are willing to lose, and tick
the alert thresholds at 50, 90 and 100 percent so the mail arrives before the
money does. Do it in the same sitting as the upload; a budget alert set the week
after is a budget alert that was not there for the week that mattered.

`--max-instances 3` in `app/deploy.sh` is the other half of that guard and it is
already set. It caps how fast a bill can grow; the budget alert is what tells
you it is growing.

### Leave the board as the judges will find it

Run the six steps in section 1 and then seed one patient, so the console is not
empty when someone opens the link two days later. Use the console URL that came
back from `rotate-token`.

---

## 6. The public repo, when that gate opens

The public repository is five paths, and only five:

```
README.md    at the repository root, because GitHub renders that one
app/         the service (no secrets, no .env, test-assets are synthetic)
docs/        ARCHITECTURE, SAFETY, DEVPOST, RUNBOOK, seed/
LICENSE      MIT, Mohamed Mostafa 2026
.gitignore   at the repository root
```

Everything else in the working folder this was built in is planning material,
slice specs, verification results and reviews. None of it ships: the public
repository is the five entries above and nothing else.

Creating and pushing that repository is a separate gate. Nothing in this build
does it, and no commit has been made from this tree.
