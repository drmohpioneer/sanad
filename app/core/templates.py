"""Owns the words the Care Coordinator is allowed to send a patient.

Twelve sentences, that is the whole vocabulary: the five the Coordinator's
tools needed at S6, three more the administrative tier needed at S6++ item G,
and four added at rev 17, which sit together at the bottom of this file under a
marked block so a reviewer reading the Arabic has one place to read. The
Coordinator is an agent with tools and it never writes a line: it picks a tool,
and the tool sends one of these, gendered by core/gender.py and in the patient's
own language. The only variable parts are a date, the doctor's name, the
patient's first name and the name of a missing analyte, and `render` refuses
anything else, so a template can never grow a number or a dose the doctor did
not write.

The Chaser's ladder templates stay where they are (core/chaser.py): those are
the reminders S3 owns. These are the sentences the Coordinator needs and S3 had
no reason to have.

Arabic conjugates the second person, so each line exists in three forms: to a
man, to a woman, and, when the record does not say, in wording that commits to
neither. That is the same rule and the same three keys the Chaser uses.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import string
from typing import Any

# The only variables a template may carry. A field outside this set is a
# refusal, not a substitution: this is what keeps a date and a doctor's name
# from becoming a place a dose could appear.
ALLOWED_FIELDS: frozenset[str] = frozenset({"patient", "doctor", "date", "analyte"})

# "we will check again on <date>"
CHECK_AGAIN = {
    "ar": {
        "m": "تمام {patient}. هسأل عليك تاني يوم {date}.",
        "f": "تمام {patient}. هسأل عليكي تاني يوم {date}.",
        "u": "تمام {patient}. هيتم السؤال تاني يوم {date}.",
    },
    "en": {
        "m": "Alright {patient}. I will check again on {date}.",
        "f": "Alright {patient}. I will check again on {date}.",
        "u": "Alright {patient}. I will check again on {date}.",
    },
}

# "I told Dr <name> about the cost". Nothing else is said about money: what to
# do about the price of a test is the doctor's decision (core/policy.py).
COST_TOLD = {
    "ar": {
        "m": "بلّغت {doctor} بموضوع التكلفة. مش هبعت تذكيرات تانية لحد ما ييجي الرد.",
        "f": "بلّغت {doctor} بموضوع التكلفة. مش هبعت تذكيرات تانية لحد ما ييجي الرد.",
        "u": "بلّغت {doctor} بموضوع التكلفة. مش هبعت تذكيرات تانية لحد ما ييجي الرد.",
    },
    "en": {
        "m": "I have told {doctor} about the cost. I will not send any more "
             "reminders until there is an answer.",
        "f": "I have told {doctor} about the cost. I will not send any more "
             "reminders until there is an answer.",
        "u": "I have told {doctor} about the cost. I will not send any more "
             "reminders until there is an answer.",
    },
}

# "please send the result when you have it"
SEND_WHEN_READY = {
    "ar": {
        "m": "تمام. أول ما النتيجة تجهز ابعتهالي هنا.",
        "f": "تمام. أول ما النتيجة تجهز ابعتيهالي هنا.",
        "u": "تمام. برجاء إرسال النتيجة هنا أول ما تجهز.",
    },
    "en": {
        "m": "Alright. Send me the result here as soon as you have it.",
        "f": "Alright. Send me the result here as soon as you have it.",
        "u": "Alright. Please send the result here as soon as it is ready.",
    },
}

# "please send the missing part: <analyte>"
MISSING_PART = {
    "ar": {
        "m": "وصلتني النتيجة بس ناقصها {analyte}. ابعتلي الجزء الناقص لما يجهز.",
        "f": "وصلتني النتيجة بس ناقصها {analyte}. ابعتيلي الجزء الناقص لما يجهز.",
        "u": "وصلتني النتيجة بس ناقصها {analyte}. برجاء إرسال الجزء الناقص لما يجهز.",
    },
    "en": {
        "m": "I have your result but {analyte} is missing. Send me the missing "
             "part when you have it.",
        "f": "I have your result but {analyte} is missing. Send me the missing "
             "part when you have it.",
        "u": "I have your result but {analyte} is missing. Please send the "
             "missing part when it is ready.",
    },
}

# The doctor's pre-approved reason for a follow-up, item I. It is used only when
# the doctor's policy record carries no line of his own; when it does, his own
# words are sent as his.
FOLLOWUP_REASON = {
    "ar": {
        "m": "{doctor} طالب ده عشان يتطمن إن العلاج شغال، حتى وانت حاسس إنك كويس.",
        "f": "{doctor} طالب ده عشان يتطمن إن العلاج شغال، حتى وانتي حاسة إنك كويسة.",
        "u": "{doctor} طالب ده عشان يتطمن إن العلاج شغال، حتى من غير أي أعراض.",
    },
    "en": {
        "m": "{doctor} asked for this to check the treatment is working, even "
             "when you feel fine.",
        "f": "{doctor} asked for this to check the treatment is working, even "
             "when you feel fine.",
        "u": "{doctor} asked for this to check the treatment is working, even "
             "when you feel fine.",
    },
}

# "I told Dr <name> and I am not going to suggest anything myself". The
# administrative tier's answer to "the medicine is not available" (S6++ item G):
# a substitute is a treatment decision, so Sanad records the barrier, tells the
# doctor, and says exactly that to the patient. First person throughout, so
# there is no second-person verb to conjugate.
TOLD_DOCTOR = {
    "ar": {
        "m": "بلّغت {doctor} إن الدوا مش متوفر. مش هقترح بديل، ده قرار الدكتور.",
        "f": "بلّغت {doctor} إن الدوا مش متوفر. مش هقترح بديل، ده قرار الدكتور.",
        "u": "بلّغت {doctor} إن الدوا مش متوفر. مش هقترح بديل، ده قرار الدكتور.",
    },
    "en": {
        "m": "I have told {doctor} that it is not available. I will not suggest "
             "an alternative: that is the doctor's decision.",
        "f": "I have told {doctor} that it is not available. I will not suggest "
             "an alternative: that is the doctor's decision.",
        "u": "I have told {doctor} that it is not available. I will not suggest "
             "an alternative: that is the doctor's decision.",
    },
}

# The line in front of the doctor's own plan text when the patient has lost his
# copy of it. The plan itself is not a template and is not generated: it is what
# the doctor confirmed at intake, sent again, word for word.
PLAN_AGAIN = {
    "ar": {
        "m": "دي خطة {doctor} زي ما هي:",
        "f": "دي خطة {doctor} زي ما هي:",
        "u": "دي خطة {doctor} زي ما هي:",
    },
    "en": {
        "m": "Here is {doctor}'s plan again:",
        "f": "Here is {doctor}'s plan again:",
        "u": "Here is {doctor}'s plan again:",
    },
}

# "where do I send it": here, as a photo, and nothing else.
SEND_IT_HERE = {
    "ar": {
        "m": "ابعتلي صورة النتيجة هنا في الشات ده.",
        "f": "ابعتيلي صورة النتيجة هنا في الشات ده.",
        "u": "برجاء إرسال صورة النتيجة هنا في الشات ده.",
    },
    "en": {
        "m": "Send me a photo of the result here, in this chat.",
        "f": "Send me a photo of the result here, in this chat.",
        "u": "Please send a photo of the result here, in this chat.",
    },
}

# =========================================================================== #
# REV 17: every patient-facing sentence added after the S6++ blocks lives here,
# in one block, so Mohamed's Arabic pass has one place to read. Same rules as
# every line above it: three genders, two languages, no field outside
# ALLOWED_FIELDS, and nothing a model wrote.
# =========================================================================== #

# "I told Dr <name>, he will answer you here." The one line a patient gets when
# the Coordinator escalates on something he actually said (rev 17 item 6). Until
# rev 17 an escalation on a reply left the patient with no answer from Sanad at
# all, so the Concierge generated one on top of the escalation and the screen
# showed Sanad arguing with a patient it had just decided not to argue with.
# This is the whole of what Sanad says: the doctor's later answer, through the
# relay, is the real reply.
TOLD_DOCTOR_WILL_ANSWER = {
    "ar": {
        "m": "بلّغت {doctor} بالموضوع، والرد هيوصلك هنا.",
        "f": "بلّغت {doctor} بالموضوع، والرد هيوصلك هنا.",
        "u": "بلّغت {doctor} بالموضوع، والرد هييجي هنا.",
    },
    "en": {
        "m": "I have told {doctor} about this. The answer will come to you here.",
        "f": "I have told {doctor} about this. The answer will come to you here.",
        "u": "I have told {doctor} about this. The answer will come here.",
    },
}

# The two bubbles a patient sees before he has typed anything (rev 17 item 9).
# The first says who this is and what it is not; the second introduces the
# doctor's own confirmed plan text, which follows as its own message, word for
# word, and is not a template and not generated.
WELCOME = {
    "ar": {
        "m": "أهلاً {patient} 👋 أنا سند، المساعد الذكي بتاع {doctor}. "
             "أنا مش دكتور: بتابع معاك الخطة وأوصّل أي حاجة لدكتورك.",
        "f": "أهلاً {patient} 👋 أنا سند، المساعد الذكي بتاع {doctor}. "
             "أنا مش دكتور: بتابع معاكي الخطة وأوصّل أي حاجة لدكتورك.",
        "u": "أهلاً {patient} 👋 أنا سند، المساعد الذكي بتاع {doctor}. "
             "أنا مش دكتور: بتابع الخطة وأوصّل أي حاجة للدكتور.",
    },
    "en": {
        "m": "Hello {patient} 👋 I am Sanad, {doctor}'s assistant. I am not a "
             "doctor: I follow the plan with you and pass anything you say to "
             "your doctor.",
        "f": "Hello {patient} 👋 I am Sanad, {doctor}'s assistant. I am not a "
             "doctor: I follow the plan with you and pass anything you say to "
             "your doctor.",
        "u": "Hello {patient} 👋 I am Sanad, {doctor}'s assistant. I am not a "
             "doctor: I follow the plan and pass anything you say to the doctor.",
    },
}

# The line under the plan on that first open: what happens next, and the one
# invitation to talk back.
WELCOME_NEXT = {
    "ar": {
        "m": "هفكرك بمواعيدك هنا. لو حصلت أي حاجة، قوللي هنا وأنا أبلّغ {doctor}.",
        "f": "هفكرك بمواعيدك هنا. لو حصلت أي حاجة، قوليلي هنا وأنا أبلّغ {doctor}.",
        "u": "التذكير بالمواعيد هييجي هنا. لو حصلت أي حاجة، الكلام هنا يوصل {doctor}.",
    },
    "en": {
        "m": "I will remind you here when something is due. If anything comes "
             "up, tell me here and I will pass it to {doctor}.",
        "f": "I will remind you here when something is due. If anything comes "
             "up, tell me here and I will pass it to {doctor}.",
        "u": "Reminders will come here when something is due. Anything said "
             "here reaches {doctor}.",
    },
}

# The line in front of the doctor's own answer when it is relayed to a patient
# (rev 17 item 11). Until then an Arabic-speaking patient got his own doctor's
# words with an English label on them: "Test Doctor says: ...". The answer
# itself is the doctor's free text, which is the trusted path in SAFETY.md, so
# it follows this line rather than sitting inside it, exactly as the plan does
# behind plan_again.
DOCTOR_SAYS = {
    "ar": {
        "m": "{doctor} بيقولك:",
        "f": "{doctor} بيقولك:",
        "u": "{doctor} بيقول:",
    },
    "en": {
        "m": "{doctor} says:",
        "f": "{doctor} says:",
        "u": "{doctor} says:",
    },
}


TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    "check_again": CHECK_AGAIN,
    "cost_told": COST_TOLD,
    "send_when_ready": SEND_WHEN_READY,
    "missing_part": MISSING_PART,
    "followup_reason": FOLLOWUP_REASON,
    "told_doctor": TOLD_DOCTOR,
    "plan_again": PLAN_AGAIN,
    "send_it_here": SEND_IT_HERE,
    # rev 17
    "told_doctor_will_answer": TOLD_DOCTOR_WILL_ANSWER,
    "welcome": WELCOME,
    "welcome_next": WELCOME_NEXT,
    "doctor_says": DOCTOR_SAYS,
}


def fields_of(template: str) -> set[str]:
    """The named variables in one template string."""
    return {
        name for _, name, _, _ in string.Formatter().parse(template) if name
    }


def render(key: str, speak: str, who: str, **fields: Any) -> str:
    """One template, in one language, in one grammatical gender.

    `speak` is "ar" or "en" (core/lang.py decides which); `who` is "m", "f" or
    "u" (core/gender.py decides which). A key that does not exist, or a field
    the template does not declare, raises rather than producing half a sentence.
    """
    table = TEMPLATES[key]
    text = table["ar" if speak == "ar" else "en"].get(who) or table["en"]["u"]
    wanted = fields_of(text)
    unknown = wanted - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"{key} carries fields that are not allowed: {sorted(unknown)}")
    missing = wanted - set(fields)
    if missing:
        raise ValueError(f"{key} needs {sorted(missing)}")
    return tidy(text.format(**{name: fields[name] for name in wanted}))


# Punctuation a dropped field must not be left sitting in front of.
_HANGING = ".,!?:؛،"


def tidy(text: str) -> str:
    """Close the gap a field rendered as an empty string leaves behind.

    Rev 17 item 11: an Arabic sentence greets the patient with "يا أحمد" or,
    when no Arabic form of his name is known, with nothing at all, because a
    Latin name inside an Arabic sentence is the tell of a machine. The vocative
    lives inside the `patient` field for that reason, and dropping it would
    otherwise leave "تمام . هسأل" with a double space and a floating full stop.
    Whitespace and punctuation only: this can never add a word.
    """
    for mark in _HANGING:
        text = text.replace(f" {mark}", mark)
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()
